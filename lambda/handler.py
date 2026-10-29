"""Per-minute Bitbucket watcher (AWS Lambda, triggered by an EventBridge rule).

For every open PR in the PIB repos:
  * a new mention of ME by someone else on a CLEAN PR -> dispatch the review workflow on GitHub
    (only=<repo>#<id>), claiming the mention first so it is handled once;
  * a PR the agent already reviewed that now has the approvals Bitbucket requires -> dispatch a
    "merge" check so the agent merges it if nothing blocking changed;
  * a PR nobody has mentioned me on since it became ready (created, went from conflicted to CLEAN, or got
    new commits after I took part), after a random 1-5 min wait -> post one reminder asking the author to mention me.
State lives in DynamoDB (pk = "<repo>#<id>") and is kept for closed PRs too, so a reopened PR is
not reminded or reviewed twice. The first run only records what already exists.
Comments the regex rules do not recognise (e.g. "I pushed the fixes", "can you take a look?") are
classified once by a lightweight Gemini Flash-Lite call, only on PRs I took part in or in replies to my
own comments; a "yes, review it" verdict counts as a mention. The feature is off without the key.
Secrets (SSM SecureString): /prwatch/bitbucket-token, /prwatch/github-token, /prwatch/gemini-api-key.
"""

import json
import os
import random
import re
import time
import urllib.error
import urllib.request

import boto3
from botocore.exceptions import ClientError

ME = "alejitocordoba"
BB = "https://pi2tools.icesi.edu.co/bitbucket/rest/api/1.0/projects/PIB/repos"
REPOS = ["contratacion-back", "contratacion-front", "contratacion-files", "contratacion-auth"]
DISPATCH = "https://api.github.com/repos/elkofix/pr-watch/actions/workflows/pr-watch.yml/dispatches"
TEST_OWN = bool(os.environ.get("TEST_NUDGE_OWN"))  # test mode: my own PRs and mentions count
NUDGE = "Hi, is this ready for revision? If so, remember to mention me so I get notified."
GEMINI_MODELS = [os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite"), "gemini-3.1-flash-lite"]
MAX_LLM = 5  # classifications per minute; the rest waits for the next run
CONFLICTS = "Solve conflicts first"  # reply to a review request on a PR that has merge conflicts

table = boto3.resource("dynamodb").Table(os.environ.get("TABLE", "prwatch"))
ssm = boto3.client("ssm")
_secrets = {}


def secret(name):
    if name not in _secrets:
        _secrets[name] = ssm.get_parameter(Name=name, WithDecryption=True)["Parameter"]["Value"].strip()
    return _secrets[name]


# "@alejitocordoba", or someone greeting me by name ("Hi Alejandro," / a line starting with "Alejandro")
NAME = re.compile(r"\b(?:hi|hello|hey|hola|dear)\b[ ,]+alejandro\b|^\s*alejandro\b", re.IGNORECASE | re.MULTILINE)


# A short affirmative reply to my "is this ready?" reminder ("yes", "ready", "listo", "ya quedó"...).
AFFIRM = re.compile(r"\b(?:yes|yep|yeah|yup|sure|ready|done|ok|okay|all set|go ahead|please|sí|si|claro|listo|lista|ya|dale)\b", re.IGNORECASE)
NEGATION = re.compile(r"\b(?:not|no|nope|never|wait|still|yet|working on|todavía|aún|falta|casi)\b|n't", re.IGNORECASE)


def is_nudge(comment):
    return comment["author"]["slug"] == ME and comment["text"].startswith(NUDGE[:30])


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



def call(method, url, token, body=None):
    headers = {"Authorization": f"Bearer {token}", "User-Agent": "pr-watch-lambda",
               "Accept": "application/json", "X-Atlassian-Token": "no-check"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            raw = response.read()
            return response.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as error:
        return error.code, None


def bb(path, method="GET", body=None):
    status, data = call(method, f"{BB}/{path}", secret("/prwatch/bitbucket-token"), body)
    if status >= 400:
        raise RuntimeError(f"bitbucket {method} {path} -> {status}")
    return data


def comments(nodes, parent=None):
    """Every comment in the tree as (comment, parent comment or None)."""
    for node in nodes:
        yield node, parent
        yield from comments(node.get("comments", []), node)


_threads = {}  # per invocation: (repo, pr) -> every comment as (comment, parent)


def thread(repo, pr_id):
    if (repo, pr_id) not in _threads:
        activities = bb(f"{repo}/pull-requests/{pr_id}/activities?limit=100")["values"]
        _threads[(repo, pr_id)] = [cp for a in activities if a.get("comment")
                                   for cp in comments([a["comment"]])]
    return _threads[(repo, pr_id)]


def mentions_after(repo, pr_id, after):
    """Mentions of ME by others with id > after, oldest first."""
    found = [c for c, parent in thread(repo, pr_id) if is_mention(c, TEST_OWN, parent) and c["id"] > after]
    return sorted(found, key=lambda c: c["id"])


def gemini_ready(comment, parent):
    """True/False: does this comment tell me the PR is ready for another look? None: try again later."""
    try:
        key = secret("/prwatch/gemini-api-key")
    except ClientError:
        return False  # not configured: feature off
    context = ""
    if parent:
        context = f'Comment it replies to ({parent["author"]["name"]}): """{parent["text"][:700]}"""\n'
    prompt = (
        "You help a code reviewer named Alejandro. Decide if the NEW COMMENT on a pull request tells him the "
        "work is ready for him to review (again): e.g. changes are pushed or fixed, the PR is ready, it answers "
        "something he asked and asks for another look, or it asks him to check. Comments that are just "
        "discussion, thanks, status updates between other people, or say it is NOT ready are false. "
        'Reply ONLY with JSON: {"review": true} or {"review": false}.\n'
        f'{context}NEW COMMENT ({comment["author"]["name"]}): """{comment["text"][:1500]}"""'
    )
    body = {"contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0, "responseMimeType": "application/json",
                                 "maxOutputTokens": 512}}
    for model in GEMINI_MODELS:
        request = urllib.request.Request(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
            data=json.dumps(body).encode(), method="POST",
            headers={"Content-Type": "application/json", "x-goog-api-key": key})
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                data = json.load(response)
            text = "".join(p.get("text", "") for p in data["candidates"][0]["content"]["parts"])
            return bool(json.loads(text).get("review"))
        except Exception as error:  # try the fallback model, then give up until the next run
            print(f"gemini {model}: {type(error).__name__}")
    return None


def llm_mentions(repo, pr, item, engaged):
    """Comments by others the regex rules missed that Gemini says ask for a review, and the new cursor."""
    scanned = int(item["scanned"])
    thread_ = thread(repo, pr["id"])
    top = max([c["id"] for c, _ in thread_] + [scanned])
    found, calls = [], 0
    for c, parent in sorted(thread_, key=lambda cp: cp[0]["id"]):
        if c["id"] <= scanned or c["author"]["slug"] == ME or is_mention(c, False, parent):
            continue
        if not (engaged or (parent and parent["author"]["slug"] == ME)):
            continue
        if calls >= MAX_LLM:
            return found, c["id"] - 1
        calls += 1
        verdict = gemini_ready(c, parent)
        if verdict is None:
            return found, c["id"] - 1
        if verdict:
            found.append(c)
    return found, top


def is_clean(repo, pr_id):
    return bb(f"{repo}/pull-requests/{pr_id}/merge").get("outcome") == "CLEAN"


def claim(pk, sets, expect):
    """Conditional update; False when another invocation already changed the item."""
    names = {f"#{k}": k for k in list(sets) + list(expect)}
    values = {f":s_{k}": v for k, v in sets.items()}
    values.update({f":e_{k}": v for k, v in expect.items()})
    try:
        table.update_item(
            Key={"pk": pk},
            UpdateExpression="SET " + ", ".join(f"#{k} = :s_{k}" for k in sets),
            ConditionExpression=" AND ".join(f"#{k} = :e_{k}" for k in expect),
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )
        return True
    except ClientError as error:
        if error.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise


def dispatch(pk, action, comment_id=None, unchanged=False):
    inputs = {"only": pk, "action": action}
    if comment_id:
        inputs["comment_id"] = str(comment_id)  # the comment that asked for the review
    if unchanged:
        inputs["unchanged"] = "true"  # merge check: no new commits since the agent reviewed it
    status, _ = call("POST", DISPATCH, secret("/prwatch/github-token"), {"ref": "main", "inputs": inputs})
    return status


REACT = "https://pi2tools.icesi.edu.co/bitbucket/rest/comment-likes/latest/projects/PIB/repos"


def react(repo, pr_id, comment_id, emoticon="eyes"):
    """Acknowledge a comment with an emoji reaction under my account. Never blocks the review."""
    try:
        status, _ = call("PUT", f"{REACT}/{repo}/pull-requests/{pr_id}/comments/{comment_id}/reactions/{emoticon}",
                         secret("/prwatch/bitbucket-token"))
        if status >= 400:
            print(f"reaction on #{comment_id} -> {status}")
    except Exception as error:
        print(f"reaction on #{comment_id}: {type(error).__name__}")


def review(pk, last_id, previous, head=None):
    sets = {"handled": last_id, "nudged": True, "reviewed": True, "merge_requested": False}
    if head:
        sets["reviewed_head"] = head  # the commit the agent is asked to review
    if not claim(pk, sets, {"handled": previous}):
        return
    status = dispatch(pk, "review", last_id)
    print(f"{pk} mention #{last_id} -> dispatch {status}")
    if status == 204:
        repo, pr_id = pk.split("#")
        react(repo, int(pr_id), last_id)  # :eyes: on the comment that triggered the review
    if status != 204:  # give the mention back so the next minute retries it
        claim(pk, {"handled": previous}, {"handled": last_id})


def merge_check(repo, pr, item):
    """A PR the agent already reviewed that reached its required approvals: ask the agent to merge it."""
    pk = f"{repo}#{pr['id']}"
    if not item.get("reviewed") or item.get("merge_requested"):
        return
    if not any(r["status"] == "APPROVED" and r["user"]["slug"] != ME for r in pr.get("reviewers", [])):
        return
    info = bb(f"{repo}/pull-requests/{pr['id']}/merge")
    if not (info.get("canMerge") and info.get("outcome") == "CLEAN"):
        return
    try:
        table.update_item(Key={"pk": pk}, UpdateExpression="SET merge_requested = :t",
                          ConditionExpression="attribute_not_exists(merge_requested) OR merge_requested = :f",
                          ExpressionAttributeValues={":t": True, ":f": False})
    except ClientError as error:
        if error.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return
        raise
    head = pr.get("fromRef", {}).get("latestCommit")
    unchanged = bool(head) and item.get("reviewed_head") == head
    status = dispatch(pk, "merge", unchanged=unchanged)
    print(f"{pk} reached its approvals -> merge check dispatch {status} (unchanged since review: {unchanged})")
    if status != 204:
        table.update_item(Key={"pk": pk}, UpdateExpression="SET merge_requested = :f",
                          ExpressionAttributeValues={":f": False})


def conflicts_reply(repo, pr, pk, comment_id):
    """Someone asked for a review but the PR has conflicts: answer that comment once. The mention stays
    pending, so the review goes out by itself when the PR is clean."""
    try:
        table.update_item(Key={"pk": pk}, UpdateExpression="SET conflict_replied = :n",
                          ConditionExpression="attribute_not_exists(conflict_replied) OR conflict_replied < :n",
                          ExpressionAttributeValues={":n": comment_id})
    except ClientError as error:
        if error.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return  # already answered
        raise
    try:
        bb(f"{repo}/pull-requests/{pr['id']}/comments", "POST",
           {"text": CONFLICTS, "parent": {"id": comment_id}})
        print(f"{pk} conflicts reply to comment #{comment_id}")
    except Exception:
        table.update_item(Key={"pk": pk}, UpdateExpression="SET conflict_replied = :z",
                          ExpressionAttributeValues={":z": 0})  # retry next minute
        raise


def update(pk, fields):
    table.update_item(
        Key={"pk": pk},
        UpdateExpression="SET " + ", ".join(f"#{k} = :{k}" for k in fields),
        ExpressionAttributeNames={f"#{k}": k for k in fields},
        ExpressionAttributeValues={f":{k}": v for k, v in fields.items()},
    )


def process(repo, pr, item, baseline, now):
    pk = f"{repo}#{pr['id']}"
    clean = is_clean(repo, pr["id"])
    head = pr.get("fromRef", {}).get("latestCommit")
    if item is None:
        if not baseline:  # first ever run: record what exists, react to nothing
            old = mentions_after(repo, pr["id"], 0)
            table.put_item(Item={"pk": pk, "handled": old[-1]["id"] if old else 0, "nudged": True,
                                 "nudge_at": 0, "clean": clean, "head": head, "since": 0,
                                 "scanned": max([c["id"] for c, _ in thread(repo, pr["id"])] + [0])})
            return
        item = {"pk": pk, "handled": 0, "nudged": False, "clean": clean, "head": head, "scanned": 0,
                "nudge_at": now + random.randint(60, 300),
                "since": int(pr.get("createdDate", now * 1000) / 1000)}
        table.put_item(Item=item)
    else:
        fields = {}
        if item.get("clean") is not clean:
            fields["clean"] = clean
        if item.get("head") != head:
            fields["head"] = head
        conflicts_fixed = item.get("clean") is False and clean
        # new commits on a PR I took part in (reviewed by the agent, or I am a reviewer)
        engaged = int(item["handled"]) > 0 or any(r["user"]["slug"] == ME for r in pr.get("reviewers", []))
        pushed = item.get("head") is not None and head is not None and item["head"] != head and engaged
        if item["nudged"] and int(item["nudge_at"]) == 0 and not engaged:
            # recorded at the first run (nudge_at 0), never reminded and I never took part in it:
            # give it the reminder it missed; any mention, even an old one, cancels it
            fields.update(nudged=False, nudge_at=now + random.randint(60, 300), since=0)
        if conflicts_fixed or pushed:  # remind once more, unless someone mentions me in the meantime
            # (3 min of slack for a mention posted just before the change was noticed)
            fields.update(nudged=False, nudge_at=now + random.randint(60, 300), since=now - 180)
        if fields:
            update(pk, fields)
            item = {**item, **fields}
    handled = int(item["handled"])
    engaged = handled > 0 or any(r["user"]["slug"] == ME for r in pr.get("reviewers", []))
    if "scanned" not in item:  # row from before this feature: never classify the old comments
        item = {**item, "scanned": max([c["id"] for c, _ in thread(repo, pr["id"])] + [0])}
        update(pk, {"scanned": item["scanned"]})
    extra, scanned = llm_mentions(repo, pr, item, engaged)
    pending = max([c["id"] for c in extra] + [int(item.get("llm_pending", 0))])
    if scanned != int(item["scanned"]) or pending != int(item.get("llm_pending", 0)):
        item = {**item, "scanned": scanned, "llm_pending": pending}
        update(pk, {"scanned": scanned, "llm_pending": pending})  # remembered until the PR is clean
    mentions = mentions_after(repo, pr["id"], handled)
    last_id = max([c["id"] for c in mentions] + [pending if pending > handled else 0])
    if last_id > handled:
        if clean:
            review(pk, last_id, handled, head)
        else:
            conflicts_reply(repo, pr, pk, last_id)
        return
    merge_check(repo, pr, item)
    if item["nudged"] or now < int(item["nudge_at"]):
        return
    own = pr["author"]["user"]["slug"] == ME and not TEST_OWN
    since = int(item.get("since", 0))
    recent = [c for c in mentions_after(repo, pr["id"], 0) if c["createdDate"] / 1000 >= since]
    if own or recent:  # my own PR, or I was already mentioned since it became ready
        claim(pk, {"nudged": True}, {"nudged": False})
        return
    if not clean:
        return  # keeps waiting; the reminder goes out once the PR is clean
    if not claim(pk, {"nudged": True}, {"nudged": False}):
        return
    try:
        bb(f"{repo}/pull-requests/{pr['id']}/comments", "POST", {"text": NUDGE})
        print(f"{pk} reminder posted")
    except Exception:
        claim(pk, {"nudged": False}, {"nudged": True})
        raise


def scan_all():
    items, kwargs = [], {}
    while True:
        page = table.scan(**kwargs)
        items += page["Items"]
        if "LastEvaluatedKey" not in page:
            return items
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def handler(event, context):
    now = int(time.time())
    _threads.clear()  # warm containers are reused between runs
    baseline = "Item" in table.get_item(Key={"pk": "_baseline"})
    items = {i["pk"]: i for i in scan_all()}
    failed = set()
    for repo in REPOS:
        try:
            prs = bb(f"{repo}/pull-requests?state=OPEN&limit=100")["values"]
        except Exception as error:
            print(f"{repo}: {error}")
            failed.add(repo)
            continue
        for pr in prs:
            try:
                process(repo, pr, items.get(f"{repo}#{pr['id']}"), baseline, now)
            except Exception as error:  # one broken PR must not stop the others
                print(f"{repo}#{pr['id']}: {error}")
    if not baseline and not failed:
        table.put_item(Item={"pk": "_baseline"})
    return {"repos_failed": sorted(failed)}
