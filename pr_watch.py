"""Reviews open, conflict-free Bitbucket PRs where someone mentioned me in a new comment.

Runs from GitHub Actions every 30 min (see .github/workflows/pr-watch.yml). Polling is plain Python;
Claude (Sonnet 5.5, medium effort) only runs when a PR qualifies: OPEN + merge outcome CLEAN + a
comment by someone else containing @ME newer than the last one handled.

Env: BITBUCKET_TOKEN, CLAUDE_CODE_OAUTH_TOKEN. Pass --once for a single pass (the default in CI).
First run (no state.json) records the existing mentions as handled: no backlog reviews.
"""

import json
import os
import subprocess
import sys
import time
import urllib.request

ME = "alejitocordoba"
BASE = "https://pi2tools.icesi.edu.co/bitbucket/rest/api/1.0/projects/PIB/repos"
REPOS = ["contratacion-back", "contratacion-front", "contratacion-files", "contratacion-auth"]
HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(HERE, "state.json")
INTERVAL = 30 * 60
TOKEN = os.environ["BITBUCKET_TOKEN"]
WORKDIR = os.environ.get("WORKDIR", os.path.expanduser("~/work"))  # repos get cloned here
# Only what the review routine needs; anything else is denied (no prompt is available headless).
ALLOWED = (
    "Read Grep Glob Write Edit "
    "Bash(git:*) Bash(curl:*) Bash(docker:*) Bash(python3:*) Bash(./gradlew:*) "
    "Bash(npm:*) Bash(npx:*) Bash(ls:*) Bash(grep:*) Bash(sed:*) Bash(cat:*) Bash(mkdir:*) "
    "Bash(cp:*) Bash(rm:*) Bash(sleep:*) Bash(openssl:*) "
    "Bash(cd:*) Bash(echo:*) Bash(head:*) Bash(tail:*) Bash(wc:*) Bash(tr:*) Bash(cut:*) "
    "Bash(sort:*) Bash(uniq:*) Bash(find:*) Bash(awk:*) Bash(jq:*) Bash(tee:*) Bash(mv:*) "
    "Bash(chmod:*) Bash(kill:*) Bash(timeout:*) Bash(date:*) Bash(which:*) Bash(test:*) "
    "Bash(sudo:*) Bash(java:*) Bash(node:*)"
)
# ponytail: this list is not a sandbox (python3/docker/git can run anything); the disposable runner is.

PROMPT = """Review Bitbucket PR {repo}#{pr} ({url}). {author} mentioned me in comment #{cid}: "{text}".
Read {agents} first and follow it exactly (routine, comment style, verdict, merge rules).
Auth: Bearer $BITBUCKET_TOKEN."""


def get(path):
    request = urllib.request.Request(path, headers={"Authorization": f"Bearer {TOKEN}"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def comments(nodes):
    for node in nodes:
        yield node
        yield from comments(node.get("comments", []))


def new_mentions(repo, pr_id, after):
    """Mentions of me by others with id > after, oldest first."""
    activities = get(f"{BASE}/{repo}/pull-requests/{pr_id}/activities?limit=100")["values"]
    found = [
        c
        for a in activities
        if a.get("comment")
        for c in comments([a["comment"]])
        if f"@{ME}" in c["text"].lower() and c["author"]["slug"] != ME and c["id"] > after
    ]
    return sorted(found, key=lambda c: c["id"])


def review(repo, pr, mention):
    prompt = PROMPT.format(
        repo=repo,
        pr=pr["id"],
        url=pr["links"]["self"][0]["href"],
        author=mention["author"]["displayName"],
        cid=mention["id"],
        text=mention["text"][:300],
        agents=os.path.join(HERE, "AGENTS.md"),
    )
    os.makedirs(WORKDIR, exist_ok=True)
    # Output is discarded on purpose: this repo is public and the logs would leak private code.
    try:
        subprocess.run(
            ["claude", "-p", prompt, "--model", "claude-sonnet-5-5", "--effort", "medium",
             "--allowedTools", ALLOWED],
            cwd=WORKDIR,
            check=False,
            timeout=100 * 60,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        print("review timed out", flush=True)


def scan(state, first_run):
    for repo in REPOS:
        for pr in get(f"{BASE}/{repo}/pull-requests?state=OPEN&limit=100")["values"]:
            key = f"{repo}#{pr['id']}"
            mentions = new_mentions(repo, pr["id"], state.get(key, 0))
            if not mentions:
                continue
            last = mentions[-1]
            if first_run:
                state[key] = last["id"]
                continue
            if get(f"{BASE}/{repo}/pull-requests/{pr['id']}/merge")["outcome"] != "CLEAN":
                continue  # not marked: retried next pass once the conflicts are gone
            print(f"{time.strftime('%F %T')} reviewing {key} (mention #{last['id']})", flush=True)
            review(repo, pr, last)
            state[key] = last["id"]  # marked even if the review failed, no token loops
            json.dump(state, open(STATE, "w"), indent=1)  # saved per PR in case a later one dies


def main():
    first_run = not os.path.exists(STATE)
    state = json.load(open(STATE)) if not first_run else {}
    while True:
        try:
            scan(state, first_run)
            first_run = False  # only after a full successful pass, or old mentions would trigger reviews
        except Exception as error:  # network blips must not kill the loop
            print(f"{time.strftime('%F %T')} error: {error}", flush=True)
            if "--once" in sys.argv:
                sys.exit(1)
        if not first_run:
            json.dump(state, open(STATE, "w"), indent=1)
        if "--once" in sys.argv:
            return
        deadline = time.time() + INTERVAL
        while time.time() < deadline:  # wall clock: after a sleep/wake it scans within a minute
            time.sleep(60)


if __name__ == "__main__":
    main()
