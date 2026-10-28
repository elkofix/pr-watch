"""Reviews open, conflict-free Bitbucket PRs where someone mentioned me in a new comment.

Runs from GitHub Actions every 30 min (see .github/workflows/pr-watch.yml). Polling is plain Python;
Claude (Sonnet 5.5, medium effort) only runs when a PR qualifies: OPEN + merge outcome CLEAN + a
comment by someone else containing @ME newer than the last one handled.

Env: BITBUCKET_TOKEN, CLAUDE_CODE_OAUTH_TOKEN. Pass --once for a single pass (the default in CI).
First run (no state.json) records the existing mentions as handled: no backlog reviews.
"""

import json
import os
import shutil
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
ONLY = os.environ.get("ONLY", "").strip()  # "repo#id": one-off review of that PR, ignoring state
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
    if not shutil.which("claude"):  # installed lazily: runs with nothing to review stay at seconds
        subprocess.run(["npm", "install", "-g", "@anthropic-ai/claude-code"], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    # This repo is public: the agent's text is never printed, only a safe summary (exit code, turns,
    # denied tool calls and, when the run itself failed, the short error message).
    try:
        done = subprocess.run(
            ["claude", "-p", prompt, "--model", "claude-sonnet-5-5", "--effort", "medium",
             "--output-format", "json", "--dangerously-skip-permissions"],
            cwd=WORKDIR,
            check=False,
            timeout=100 * 60,
            capture_output=True,
            text=True,
        )
    except subprocess.TimeoutExpired:
        print("review timed out", flush=True)
        return
    print(f"claude exit={done.returncode}", flush=True)
    try:
        out = json.loads(done.stdout)
        print(f"is_error={out.get('is_error')} subtype={out.get('subtype')} "
              f"turns={out.get('num_turns')}", flush=True)
        if out.get("is_error"):
            print(f"error: {str(out.get('result'))[:300]}", flush=True)
        for denial in out.get("permission_denials", [])[:15]:
            command = str(denial.get("tool_input", {}).get("command", ""))[:300]
            print(f"denied: {denial.get('tool_name')} {command}", flush=True)
    except ValueError:
        print(f"unparseable output ({len(done.stdout)} bytes), stderr: {done.stderr[:200]!r}", flush=True)


def scan(state, first_run):
    for repo in REPOS:
        for pr in get(f"{BASE}/{repo}/pull-requests?state=OPEN&limit=100")["values"]:
            key = f"{repo}#{pr['id']}"
            if ONLY and key != ONLY:
                continue
            mentions = new_mentions(repo, pr["id"], 0 if ONLY else state.get(key, 0))
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
    if not ONLY:  # detection lives in the AWS Lambda (lambda/handler.py); this only reviews one PR
        print("no PR given (only=<repo>#<id>): scanning is done by the Lambda, nothing to do")
        return
    first_run = False
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
