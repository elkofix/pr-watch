#!/usr/bin/env bash
# Brings up the real PIB stack on a disposable Linux runner, for live PR checks:
#   Keycloak (contratacion-auth, realm nexo-rrhh + dev users) on :8180
#   contratacion-files (its own Postgres + MinIO) on :8084
#   contratacion-back (SECURITY_MODE=jwt, its Postgres :54321 + MinIO :9100) on :8080/api
#
#   scripts/stack.sh up            clone/update the repos and start everything (idempotent)
#   scripts/stack.sh token <user>  print a fresh access token (admin-con | contratador-con | candidato-con)
#   scripts/stack.sh psql "<sql>"  run SQL on the back database
#   scripts/stack.sh logs          tail the back log
#   scripts/stack.sh down          stop everything (free the ~5 GB before a full ./gradlew clean build)
#   scripts/stack.sh check         service-to-service check: back's client token accepted by files
#
# Env: BITBUCKET_TOKEN (required); BACK_REF / FILES_REF / AUTH_REF (default dev) to run a PR branch
# instead, e.g. BACK_REF=feature/x for a back PR; STACK_DIR (default ~/stack).
set -euo pipefail

STACK_DIR=${STACK_DIR:-$HOME/stack}
SCM=https://pi2tools.icesi.edu.co/bitbucket/scm/pib
REALM=http://localhost:8180/realms/nexo-rrhh
mkdir -p "$STACK_DIR"
cd "$STACK_DIR"

git_bb() { git -c credential.helper= -c "http.extraHeader=Authorization: Bearer $BITBUCKET_TOKEN" "$@"; }

checkout() {  # repo ref
    if [ ! -d "$1" ]; then git_bb clone -q "$SCM/$1.git" "$1"; fi
    git_bb -C "$1" fetch -q origin "$2"
    git -C "$1" checkout -q --force FETCH_HEAD
    echo "$1 @ $2 ($(git -C "$1" rev-parse --short HEAD))"
}

wait_for() {  # description seconds command...
    local what=$1 limit=$2; shift 2
    for _ in $(seq "$limit"); do
        if "$@" >/dev/null 2>&1; then echo "ready: $what"; return 0; fi
        sleep 1
    done
    echo "TIMEOUT waiting for $what" >&2
    return 1
}

secret() { openssl rand -hex 16; }

AUTH_COMPOSE="docker compose -f docker-compose.yml -f stack.override.yml"
export AUTH_COMPOSE

up_auth() {
    cd "$STACK_DIR/contratacion-auth"
    if [ ! -f .env ]; then
        sed -e "s/^KC_BOOTSTRAP_ADMIN_PASSWORD=.*/KC_BOOTSTRAP_ADMIN_PASSWORD=$(secret)/" \
            -e "s/^KC_DB_PASSWORD=.*/KC_DB_PASSWORD=$(secret)/" \
            -e "s/^DEV_USER_PASSWORD=.*/DEV_USER_PASSWORD=Dev-$(secret)/" \
            -e "s/^CLIENT_SECRET_SELECCION_BACKEND=.*/CLIENT_SECRET_SELECCION_BACKEND=$(secret)/" \
            -e "s/^CLIENT_SECRET_CONTRATACION_BACK=.*/CLIENT_SECRET_CONTRATACION_BACK=$(secret)/" \
            -e "s/^ADMIN_API_CLIENT_SECRET=.*/ADMIN_API_CLIENT_SECRET=$(secret)/" .env.example >.env
    fi
    # The repo publishes Keycloak on 127.0.0.1 only. On Linux a container cannot reach that through
    # host.docker.internal, so contratacion-files could not fetch the JWKS and answered 401 to every
    # service token (the back then returned 502). Publish it on all interfaces for the runner.
    cat >stack.override.yml <<'YML'
services:
  keycloak:
    ports: !override
      - "8180:8180"
YML
    # Only Keycloak and its realm/dev-user import; admin-api & co. are not needed for PR checks.
    $AUTH_COMPOSE up -d keycloak keycloak-config keycloak-config-dev
    wait_for "realm import" 300 sh -c '[ "$($AUTH_COMPOSE ps -a --format "{{.Service}} {{.State}} {{.ExitCode}}" | grep "^keycloak-config-dev ")" = "keycloak-config-dev exited 0" ]'
}

env_auth() { grep "^$1=" "$STACK_DIR/contratacion-auth/.env" | cut -d= -f2-; }

up_files() {
    cd "$STACK_DIR/contratacion-files"
    [ -f .env ] || cp .env.example .env
    docker compose up -d --build app
    # no actuator: any HTTP answer (401 included) means it is listening
    wait_for "contratacion-files" 600 curl -s -o /dev/null http://localhost:8084/api/documents
}

up_back() {
    cd "$STACK_DIR/contratacion-back"
    DB_PORT=54321 MINIO_API_PORT=9100 MINIO_CONSOLE_PORT=9101 docker compose up -d db minio
    wait_for "back postgres" 120 docker exec nexo_rrhh_db pg_isready -q
    local secret_back
    secret_back=$(env_auth CLIENT_SECRET_CONTRATACION_BACK)
    pkill -f 'GradleWrapperMain bootRun' 2>/dev/null || true
    env PROFILE=dev SECURITY_MODE=jwt DB_HOST=localhost DB_PORT=54321 \
        MINIO_URL=http://localhost:9100 MINIO_API_PORT=9100 \
        CLIENT_SECRET_CONTRATACION_BACK="$secret_back" DOCUMENT_FILE_SERVICE_CLIENT_SECRET="$secret_back" \
        DOCUMENT_FILE_SERVICE_TOKEN_URI=$REALM/protocol/openid-connect/token FILES_TOKEN_URI=$REALM/protocol/openid-connect/token \
        DOCUMENT_FILE_SERVICE_URL=http://localhost:8084 FILES_BASE_URL=http://localhost:8084 \
        DOCUMENT_FILES_BASE_URL=http://localhost:8084/api CORS_ALLOWED_ORIGINS=http://localhost:5173 \
        nohup ./gradlew bootRun --console=plain -x spotlessJavaCheck >"$STACK_DIR/back.log" 2>&1 &
    wait_for "contratacion-back" 600 grep -qE "Started [A-Za-z]+ in|APPLICATION FAILED" "$STACK_DIR/back.log"
    if grep -q "APPLICATION FAILED" "$STACK_DIR/back.log"; then
        grep -A15 "APPLICATION FAILED" "$STACK_DIR/back.log" >&2
        return 1
    fi
}

token() {
    curl -sf "$REALM/protocol/openid-connect/token" -d grant_type=password -d client_id=contratacion-front \
        -d scope=openid -d "username=$1" --data-urlencode "password=$(env_auth DEV_USER_PASSWORD)" |
        python3 -c 'import json,sys; print(json.load(sys.stdin)["access_token"])'
}

check_s2s() {
    # The token the back uses for contratacion-files (client credentials) must be accepted by files.
    local tok
    tok=$(curl -sf "$REALM/protocol/openid-connect/token" -d grant_type=client_credentials         -d client_id=contratacion-back-service --data-urlencode "client_secret=$(env_auth CLIENT_SECRET_CONTRATACION_BACK)" |
        python3 -c 'import json,sys; print(json.load(sys.stdin)["access_token"])')
    local code
    code=$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $tok"         "http://localhost:8084/api/documents?subjectId=00000000-0000-4000-8000-000000000001")
    echo "files with the back's service token -> $code"
    [ "$code" = 200 ]
}

down() {
    pkill -f 'GradleWrapperMain bootRun' 2>/dev/null || true
    (cd "$STACK_DIR/contratacion-back" && docker compose stop) || true
    (cd "$STACK_DIR/contratacion-files" && docker compose stop) || true
    (cd "$STACK_DIR/contratacion-auth" && $AUTH_COMPOSE stop) || true
    echo "stack stopped (data kept; 'up' starts it again)"
}

psql_back() { docker exec -i nexo_rrhh_db psql -U nexo_user -d rrhh_nexo_db -qAt -c "$1"; }

seed_users() {
    # app_user rows for the Keycloak dev users (model B links by email + cedula on first request).
    for user in admin-con contratador-con candidato-con; do
        read -r email cedula < <(token "$user" | python3 -c '
import base64, json, sys
p = sys.stdin.read().split(".")[1]; c = json.loads(base64.urlsafe_b64decode(p + "=" * (-len(p) % 4)))
print(c["email"], c.get("cedula", ""))')
        psql_back "INSERT INTO app_user (id, email, name, lastname, identifier_type, personal_identifier, phone, active)
                   SELECT gen_random_uuid(), '$email', '$user', 'Dev', 'CC', '$cedula', '3000000000', true
                   WHERE NOT EXISTS (SELECT 1 FROM app_user WHERE email = '$email');"
        echo "app_user: $user <$email> cedula $cedula"
    done
}

case "${1:-up}" in
up)
    start=$(date +%s)
    checkout contratacion-auth "${AUTH_REF:-dev}"
    checkout contratacion-files "${FILES_REF:-dev}"
    checkout contratacion-back "${BACK_REF:-dev}"
    up_auth
    up_files &   # files builds in Docker while the back compiles with Gradle
    files_pid=$!
    up_back
    wait "$files_pid"
    seed_users
    check_s2s || echo "WARNING: contratacion-files rejects the back's service token: calls from back to files will fail (401/502)" >&2
    echo "stack up in $(($(date +%s) - start))s: back http://localhost:8080/api, keycloak :8180, files :8084"
    ;;
token) token "$2" ;;
psql) psql_back "$2" ;;
logs) tail -n "${2:-80}" "$STACK_DIR/back.log" ;;
down) down ;;
check) check_s2s ;;
*) echo "usage: $0 up | down | check | token <user> | psql <sql> | logs [n]" >&2; exit 2 ;;
esac
