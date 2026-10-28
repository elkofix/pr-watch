"""Per-minute Bitbucket watcher (AWS Lambda, triggered by an EventBridge rule).

For every open PR in the PIB repos:
  * a new mention of ME by someone else on a CLEAN PR -> dispatch the review workflow on GitHub
    (only=<repo>#<id>), claiming the mention first so it is handled once;
  * a PR the agent already reviewed that now has the approvals Bitbucket requires -> dispatch a
    "merge" check so the agent merges it if nothing blocking changed;
  * a PR nobody has mentioned me on since it became ready (created, or went from conflicted to
    CLEAN), after a random 1-5 min wait -> post one reminder asking the author to mention me.
State lives in DynamoDB (pk = "<repo>#<id>") and is kept for closed PRs too, so a reopened PR is
not reminded or reviewed twice. The first run only records what already exists.
Secrets (SSM SecureString): /prwatch/bitbucket-token, /prwatch/github-token.
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

table = boto3.resource("dynamodb").Table(os.environ.get("TABLE", "prwatch"))
ssm = boto3.client("ssm")
_secrets = {}


def secret(name):
    if name not in _secrets:
        _secrets[name] = ssm.get_parameter(Name=name, WithDecryption=True)["Parameter"]["Value"].strip()
    return _secrets[name]


# "@alejitocordoba", or someone greeting me by name ("Hi Alejandro," / a line starting with "Alejandro")
NAME = re.compile(r"\b(?:hi|hello|hey|hola|dear)\b[ ,]+alejandro\b|^\s*alejandro\b", re.IGNORECASE | re.MULTILINE)


def is_mention(comment, include_own=False):
    """A mention by someone else (a greeting by name counts); my own only with include_own, and
    only the explicit @ form, so the agent greeting me on my PRs can never trigger itself."""
    mine = comment["author"]["slug"] == ME
    if f"@{ME}" in comment["text"].lower():
        return include_own or not mine
    return not mine and bool(NAME.search(comment["text"]))



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


def comments(nodes):
    for node in nodes:
        yield node
        yield from comments(node.get("comments", []))


def mentions_after(repo, pr_id, after):
    """Mentions of ME by others with id > after, oldest first."""
    activities = bb(f"{repo}/pull-requests/{pr_id}/activities?limit=100")["values"]
    found = [
        c
        for a in activities
        if a.get("comment")
        for c in comments([a["comment"]])
        if is_mention(c, TEST_OWN) and c["id"] > after
    ]
    return sorted(found, key=lambda c: c["id"])


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


def dispatch(pk, action):
    status, _ = call("POST", DISPATCH, secret("/prwatch/github-token"),
                     {"ref": "main", "inputs": {"only": pk, "action": action}})
    return status


def review(pk, last_id, previous):
    sets = {"handled": last_id, "nudged": True, "reviewed": True, "merge_requested": False}
    if not claim(pk, sets, {"handled": previous}):
        return
    status = dispatch(pk, "review")
    print(f"{pk} mention #{last_id} -> dispatch {status}")
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
    status = dispatch(pk, "merge")
    print(f"{pk} reached its approvals -> merge check dispatch {status}")
    if status != 204:
        table.update_item(Key={"pk": pk}, UpdateExpression="SET merge_requested = :f",
                          ExpressionAttributeValues={":f": False})


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
    if item is None:
        if not baseline:  # first ever run: record what exists, react to nothing
            old = mentions_after(repo, pr["id"], 0)
            table.put_item(Item={"pk": pk, "handled": old[-1]["id"] if old else 0, "nudged": True,
                                 "nudge_at": 0, "clean": clean, "since": 0})
            return
        item = {"pk": pk, "handled": 0, "nudged": False, "clean": clean,
                "nudge_at": now + random.randint(60, 300),
                "since": int(pr.get("createdDate", now * 1000) / 1000)}
        table.put_item(Item=item)
    elif item.get("clean") is not clean:
        fields = {"clean": clean}
        if item.get("clean") is False and clean:  # conflicts just got resolved: remind once more,
            # unless someone mentions me in the meantime (3 min of slack for a comment posted just before)
            fields.update(nudged=False, nudge_at=now + random.randint(60, 300), since=now - 180)
        update(pk, fields)
        item = {**item, **fields}
    handled = int(item["handled"])
    mentions = mentions_after(repo, pr["id"], handled)
    if mentions:
        if clean:
            review(pk, mentions[-1]["id"], handled)
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
