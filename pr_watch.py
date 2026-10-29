"""Reviews open, conflict-free Bitbucket PRs where someone mentioned me in a new comment.

Runs from GitHub Actions every 30 min (see .github/workflows/pr-watch.yml). Polling is plain Python;
Claude (Sonnet 5.5, medium effort) only runs when a PR qualifies: OPEN + merge outcome CLEAN + a
comment by someone else containing @ME newer than the last one handled.

Env: BITBUCKET_TOKEN, CLAUDE_CODE_OAUTH_TOKEN. Pass --once for a single pass (the default in CI).
First run (no state.json) records the existing mentions as handled: no backlog reviews.
"""

import json
import os
import re
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
ACTION = os.environ.get("ACTION", "review").strip() or "review"  # "review" | "merge"
COMMENT_ID = os.environ.get("COMMENT_ID", "").strip()  # comment that asked for the review (from the Lambda)
UNCHANGED = os.environ.get("UNCHANGED", "").strip() == "true"  # merge check: head is the reviewed commit
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

PROMPT = """Review Bitbucket PR {repo}#{pr} ({url}). {author} wrote comment #{cid} asking for my review: "{text}".
Read {agents} first and follow it exactly (routine, comment style, verdict, merge rules).
Auth: Bearer $BITBUCKET_TOKEN."""

# A PR I already reviewed that now has the approvals Bitbucket requires (any author, mine included).
PROMPT_MERGE = """Bitbucket PR {repo}#{pr} ({url}) was reviewed before and now has the approvals Bitbucket requires
(canMerge is true). Read {agents} first (section "Waiting for approvals"). My LAST review is the most recent
thing I did on the PR: my latest comment or my latest APPROVED / NEEDS_WORK status change, whichever is newest
(not my first review). Look at the commits and comments made after it: if a commit was pushed after that last
review, or someone left a blocking comment, do the normal review routine and do NOT merge. Otherwise merge it.
Either way, finish by posting one short natural comment saying what you did.
Auth: Bearer $BITBUCKET_TOKEN."""

# Same, but the Lambda checked that the PR head is the commit I already reviewed: no re-review needed.
PROMPT_MERGE_UNCHANGED = """Bitbucket PR {repo}#{pr} ({url}) now has the approvals Bitbucket requires (canMerge is true),
and its latest commit is exactly the one I already reviewed: nothing was pushed since my last review, so do NOT
review it again and do NOT rebuild it. Read {agents} first (section "Waiting for approvals"). Only confirm that
my own status on the PR is APPROVED (or that I was unable to approve it because it is my own PR) and that nobody
left a blocking comment after my last review comment. If so, merge it right away (POST .../merge?version=<current
PR version>) and add a short natural comment saying it was merged. If something blocks, say what and do not merge.
Do not second-guess the claim that no commit was pushed since my review: the activity list also shows older
RESCOPED events from before my last review, they are not new. Either way, always post one short comment.
Auth: Bearer $BITBUCKET_TOKEN."""


# "@alejitocordoba", or someone greeting me by name ("Hi Alejandro," / a line starting with "Alejandro")
NAME = re.compile(r"\b(?:hi|hello|hey|hola|dear)\b[ ,]+alejandro\b|^\s*alejandro\b", re.IGNORECASE | re.MULTILINE)


# A short affirmative reply to my "is this ready?" reminder ("yes", "ready", "listo", "ya quedó"...).
NUDGE_START = "Hi, is this ready for revision?"
AFFIRM = re.compile(r"\b(?:yes|yep|yeah|yup|sure|ready|done|ok|okay|all set|go ahead|please|sí|si|claro|listo|lista|ya|dale)\b", re.IGNORECASE)
NEGATION = re.compile(r"\b(?:not|no|nope|never|wait|still|yet|working on|todavía|aún|falta|casi)\b|n't", re.IGNORECASE)


def is_nudge(comment):
    return comment["author"]["slug"] == ME and comment["text"].startswith(NUDGE_START)


def is_mention(comment, include_own=False, parent=None):
    """A mention by someone else (a greeting by name counts, and so does a short affirmative reply to
    my reminder); my own only with include_own, and only the explicit @ form, so the agent greeting
    me on my PRs can never trigger itself."""
    mine = comment["author"]["slug"] == ME
    text = comment["text"]
    if f"@{ME}" in text.lower():
        return include_own or not mine
    if mine:
        return False
    if NAME.search(text):
        return True
    return bool(parent and is_nudge(parent) and len(text.strip()) <= 200
                and AFFIRM.search(text) and not NEGATION.search(text))



def get(path):
    request = urllib.request.Request(path, headers={"Authorization": f"Bearer {TOKEN}"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def comments(nodes, parent=None):
    """Every comment in the tree as (comment, parent comment or None)."""
    for node in nodes:
        yield node, parent
        yield from comments(node.get("comments", []), node)


def new_mentions(repo, pr_id, after, include_own=False):
    """Mentions of me with id > after, oldest first. Mine only count when include_own is set."""
    activities = get(f"{BASE}/{repo}/pull-requests/{pr_id}/activities?limit=100")["values"]
    found = [
        c
        for a in activities
        if a.get("comment")
        for c, parent in comments([a["comment"]])
        if is_mention(c, include_own, parent) and c["id"] > after
    ]
    return sorted(found, key=lambda c: c["id"])


def review(repo, pr, mention):
    common = dict(repo=repo, pr=pr["id"], url=pr["links"]["self"][0]["href"],
                  agents=os.path.join(HERE, "AGENTS.md"))
    if mention is None:  # action=merge
        prompt = (PROMPT_MERGE_UNCHANGED if UNCHANGED else PROMPT_MERGE).format(**common)
    else:
        prompt = PROMPT.format(author=mention["author"]["displayName"], cid=mention["id"],
                               text=mention["text"][:300], **common)
    os.makedirs(WORKDIR, exist_ok=True)
    if not shutil.which("claude"):  # installed lazily: runs with nothing to review stay at seconds
        subprocess.run(["npm", "install", "-g", "@anthropic-ai/claude-code"], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    # The whole run is printed to the job log on purpose, for debugging future fixes. The owner decided
    # this is not sensitive information (GitHub masks the secrets anyway). Do not remove it.
    try:
        done = subprocess.run(
            ["claude", "-p", prompt, "--model", "claude-sonnet-5-5", "--effort", "medium",
             "--output-format", "stream-json", "--verbose", "--dangerously-skip-permissions"],
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
    for line in done.stdout.splitlines():
        try:
            show(json.loads(line))
        except ValueError:
            print(f"? {line[:300]}", flush=True)
    if done.stderr.strip():
        print(f"stderr: {done.stderr.strip()[:1000]}", flush=True)


def show(event):
    """Print one stream-json event as readable lines: agent text, tool calls, short results, summary."""
    kind = event.get("type")
    content = event.get("message", {}).get("content", [])
    if kind == "assistant":
        for block in content:
            if block.get("type") == "text" and block["text"].strip():
                print(f"agent: {block['text'].strip()[:3000]}", flush=True)
            elif block.get("type") == "tool_use":
                args = block.get("input", {})
                print(f"tool {block.get('name')}: {str(args.get('command') or args)[:600]}", flush=True)
    elif kind == "user" and isinstance(content, list):
        for block in content:
            if block.get("type") == "tool_result":
                body = block.get("content")
                if isinstance(body, list):
                    body = " ".join(str(part.get("text", "")) for part in body)
                flag = " (error)" if block.get("is_error") else ""
                print(f"  result{flag}: {str(body)[:400]}", flush=True)
    elif kind == "result":
        print(f"is_error={event.get('is_error')} subtype={event.get('subtype')} turns={event.get('num_turns')}", flush=True)
        print(f"final: {str(event.get('result'))[:3000]}", flush=True)
        for denial in event.get("permission_denials", [])[:15]:
            print(f"denied: {denial.get('tool_name')} {str(denial.get('tool_input', {}).get('command', ''))[:300]}", flush=True)


def scan(state, first_run):
    for repo in REPOS:
        for pr in get(f"{BASE}/{repo}/pull-requests?state=OPEN&limit=100")["values"]:
            key = f"{repo}#{pr['id']}"
            if ONLY and key != ONLY:
                continue
            if ONLY and ACTION == "merge":  # the Lambda saw the PR reach its required approvals
                print(f"{time.strftime('%F %T')} merge check {key}", flush=True)
                review(repo, pr, None)
                continue
            # With ONLY the Lambda already decided (and filtered my own comments unless in test mode).
            if ONLY and COMMENT_ID:  # the Lambda points at the exact comment (it may not match the regexes)
                activities = get(f"{BASE}/{repo}/pull-requests/{pr['id']}/activities?limit=100")["values"]
                mentions = [c for a in activities if a.get("comment")
                            for c, _ in comments([a["comment"]]) if str(c["id"]) == COMMENT_ID]
            else:
                mentions = new_mentions(repo, pr["id"], 0 if ONLY else state.get(key, 0), bool(ONLY))
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
