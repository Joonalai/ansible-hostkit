#!/usr/bin/python3 -I
"""Hold new work while the repository's runners are full, and start the
next thing when one is free, for the workflow's steps.

    claude-job-queue gate | next

With many issues labelled at once, every issue got its first job and a
draft PR before any PR was finished, and each merge sent the others to a
rebase. So the number of the bot's drafts in work is capped at the
repository's work runners, CLAUDE_WORK_RUNNERS, and what comes past the
cap waits its turn.

In work is the number of the bot's open draft PRs whose Progress says
Status: working, but for one not updated for STALE_MINUTES, whose job died
with the host and which counts as stopped. A draft labelled <bot>:waiting
is in work whatever its age: claude-job-retry starts it again by itself.
An asked draft and a ready PR hold nothing. What was started in the last
STARTING_MINUTES and has no working draft yet counts too: an open issue
with a working label, not queued, with no PR of the bot's, updated since;
and a stopped draft whose last comment is the bot's marked continue.

- gate, the claude job's first step after the level: on an issues event
  whose label is a working level, for an issue with no open PR of the
  bot's, when in work is at the cap or over, it labels the issue
  <bot>:queued and writes queued=true to $GITHUB_OUTPUT, and the job's
  later steps do not run. Any other event passes: a comment continues work
  that exists, and is never held. Two issues labelled in the same second
  may both pass; the cap is soft.
- next, the claude job's last step and the scheduled sweep's: while in
  work is under the cap, it starts one thing and counts it, in this order:
  a stale working draft, then the stopped drafts, oldest opened first,
  each with a marked "@<bot> continue" that claude-job-finish counts
  among its ROUNDS, so a draft that never finishes ends at asked; a draft
  that went on ROUNDS times already is turned to asked by
  `claude-job-finish spent`, with its note to the owner. Then the
  queued issues, by the earliest of CLAUDE_PRIORITY_LABELS they carry,
  then by when they were queued: the label comes off and a marked
  "@<bot> continue" starts the issue at its labels' level.

With CLAUDE_WORK_RUNNERS unset or 0 it does nothing, so a repository whose
runners know nothing of the cap sees no change.

It reads from the environment: GITHUB_REPOSITORY, CLAUDE_BOT,
CLAUDE_APP_BOT_ID, CLAUDE_ALLOWED_IDS, CLAUDE_WORK_RUNNERS and
CLAUDE_PRIORITY_LABELS (label names, the most urgent first, comma
separated), and for gate GITHUB_EVENT_NAME, GITHUB_EVENT_PATH and
GITHUB_OUTPUT. gh finds its token in GH_TOKEN.

Standard library only, on the image's python3.
"""

import datetime as dt
import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass

# The bot's name, the consumer's: what a comment mentions to start a job,
# and what the labels and the workflow's own marks begin with. The runner's
# unit sets it. Letters, digits and dashes only, since it goes into patterns.
BOT = os.environ.get("CLAUDE_BOT") or "claude"
if not re.fullmatch(r"[A-Za-z0-9-]+", BOT):
    raise SystemExit(f"CLAUDE_BOT is not a name: {BOT!r}")

QUEUED = f"{BOT}:queued"
WAITING = f"{BOT}:waiting"
# The labels that start work, as the workflow triggers on them; <bot>:plan
# starts a plan, which opens no PR and is never held.
WORKING = {BOT, f"{BOT}:review", f"{BOT}:full"}
# claude-job-finish's mark, so that its ROUNDS count bounds these too, and
# this script's own for an issue it takes from the queue.
CONTINUE = f"<!-- {BOT}:auto-continue -->"
FROM_QUEUE = f"<!-- {BOT}:auto-queue -->"
ROUNDS = 4
# Longer than any job, its parts included, so a working draft untouched
# for this long has no job left on it.
STALE_MINUTES = 150
# How long a job takes to open its draft, or to set a continued one back
# to working: an issue or draft started within this is in work already, or
# a second next in that time would start more than the cap.
STARTING_MINUTES = 30
STATUS = re.compile(r"Status: (working|stopped|asked|done)\b")
# What a PR's first line says of its issue: it closes it, or is a part of it.
ISSUE = re.compile(r"\A\s*(?:Closes|Part \d+/\d+ of|Part of) #(\d+)\b", re.IGNORECASE)

# What runs a command and gives its output, sh or a test's stand-in, and
# the environment the steps hand over.
Run = Callable[..., str]
Env = Mapping[str, str]


def sh(*argv: str) -> str:
    """Run a command and give what it printed; a failure raises."""
    return subprocess.run(argv, check=True, capture_output=True, text=True).stdout.strip()


@dataclass(frozen=True)
class Pull:
    """One of the bot's open PRs, as much of it as the queue reads."""

    number: int
    draft: bool
    status: str
    issue: int | None
    opened: str
    updated: float
    labels: frozenset[str]


def cap(env: Env) -> int:
    """The repository's work runners, or 0 when the cap is off."""
    try:
        return max(int(env.get("CLAUDE_WORK_RUNNERS") or 0), 0)
    except ValueError:
        return 0


def stamp(text: str) -> float:
    """A GitHub time as seconds since the epoch."""
    return dt.datetime.fromisoformat(text).timestamp()


def pulls(run: Run, env: Env) -> list[Pull]:
    """The bot's open PRs, drafts and ready ones."""
    lines = run(
        "gh", "api", f"repos/{env['GITHUB_REPOSITORY']}/pulls?state=open&per_page=100", "--paginate",
        "--jq", ".[] | {number, draft, body, created_at, updated_at, user: (.user.id | tostring), "
        "labels: [.labels[].name]} | tojson",
    )
    found = []
    for line in lines.splitlines():
        if not line:
            continue
        pr = json.loads(line)
        if pr["user"] != env["CLAUDE_APP_BOT_ID"]:
            continue
        body = pr.get("body") or ""
        status = STATUS.search(body)
        issue = ISSUE.search(body)
        found.append(Pull(
            number=int(pr["number"]), draft=bool(pr["draft"]), status=status.group(1) if status else "",
            issue=int(issue.group(1)) if issue else None, opened=pr["created_at"], updated=stamp(pr["updated_at"]),
            labels=frozenset(pr.get("labels") or ()),
        ))
    return found


def stale(pr: Pull, now: float) -> bool:
    """A working draft whose job died: untouched for STALE_MINUTES, and not
    waiting for a usage limit to reset.
    """
    return WAITING not in pr.labels and now - pr.updated > STALE_MINUTES * 60


def in_work(prs: list[Pull], now: float) -> int:
    """How many of the bot's drafts a job is on, or goes on by itself."""
    return sum(1 for pr in prs if pr.draft and pr.status == "working" and not stale(pr, now))


def gate(run: Run, env: Env, now: float) -> int:
    """Hold a newly labelled issue while the runners are full."""
    limit = cap(env)
    if not limit or env.get("GITHUB_EVENT_NAME") != "issues":
        return 0
    with open(env["GITHUB_EVENT_PATH"]) as event_file:
        event = json.load(event_file)
    label = (event.get("label") or {}).get("name", "")
    issue = event.get("issue") or {}
    if event.get("action") != "labeled" or label not in WORKING or "pull_request" in issue:
        return 0
    number = int(issue["number"])
    prs = pulls(run, env)
    if any(pr.issue == number for pr in prs):
        return 0
    held = in_work(prs, now) + starting(run, env, prs, now, but=number)
    if held < limit:
        return 0
    run("gh", "api", f"repos/{env['GITHUB_REPOSITORY']}/issues/{number}/labels", "-f", f"labels[]={QUEUED}")
    with open(env["GITHUB_OUTPUT"], "a") as output_file:
        output_file.write("queued=true\n")
    print(f"#{number}: queued, {held} of {limit} in work")
    return 0


def thread(run: Run, env: Env, number: int) -> list[tuple[str, bool, float]]:
    """A draft's comments, oldest first: each author's id, whether it is a
    marked continue, and when it was made.
    """
    lines = run(
        "gh", "api", f"repos/{env['GITHUB_REPOSITORY']}/issues/{number}/comments", "--paginate",
        "--jq", f'.[] | [(.user.id | tostring), (.body | contains("{CONTINUE}")), .created_at] | tojson',
    )
    found = []
    for line in lines.splitlines():
        if line:
            user, marked, made = json.loads(line)
            found.append((str(user), bool(marked), stamp(made)))
    return found


def rounds(env: Env, comments: list[tuple[str, bool, float]]) -> int:
    """The marked continues since someone on the allow-list last commented,
    as claude-job-finish counts them.
    """
    allowed = {str(each) for each in json.loads(env["CLAUDE_ALLOWED_IDS"])}
    count = 0
    for user, marked, _ in comments:
        if user in allowed:
            count = 0
        elif user == env["CLAUDE_APP_BOT_ID"] and marked:
            count += 1
    return count


def continued(env: Env, comments: list[tuple[str, bool, float]], now: float) -> bool:
    """Whether the last comment is the bot's marked continue, made within
    STARTING_MINUTES: its job has yet to set the draft to working.
    """
    if not comments:
        return False
    user, marked, made = comments[-1]
    return user == env["CLAUDE_APP_BOT_ID"] and marked and now - made < STARTING_MINUTES * 60


def starting(run: Run, env: Env, prs: list[Pull], now: float, but: int = 0) -> int:
    """The issues a job has started on in the last STARTING_MINUTES and
    that have no PR of the bot's yet, but the one named.
    """
    since = dt.datetime.fromtimestamp(now - STARTING_MINUTES * 60, dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines = run(
        "gh", "api", f"repos/{env['GITHUB_REPOSITORY']}/issues?state=open&since={since}&per_page=100",
        "--paginate", "--jq", ".[] | select(.pull_request | not) | {number, labels: [.labels[].name]} | tojson",
    )
    claimed = {pr.issue for pr in prs} | {but}
    count = 0
    for line in lines.splitlines():
        if line:
            issue = json.loads(line)
            names = set(issue["labels"])
            if names & WORKING and QUEUED not in names and int(issue["number"]) not in claimed:
                count += 1
    return count


def priorities(env: Env) -> list[str]:
    """CLAUDE_PRIORITY_LABELS, the most urgent first."""
    return [name.strip() for name in env.get("CLAUDE_PRIORITY_LABELS", "").split(",") if name.strip()]


def queued_at(run: Run, env: Env, number: int) -> str:
    """When the queued label last went on the issue, as GitHub writes the time."""
    lines = run(
        "gh", "api", f"repos/{env['GITHUB_REPOSITORY']}/issues/{number}/events", "--paginate",
        "--jq", f'.[] | select(.event == "labeled" and .label.name == "{QUEUED}") | .created_at',
    )
    times = lines.split()
    return times[-1] if times else ""


def queue(run: Run, env: Env) -> list[int]:
    """The queued issues, in the order they start."""
    lines = run(
        "gh", "api", f"repos/{env['GITHUB_REPOSITORY']}/issues?state=open&labels={QUEUED}&per_page=100",
        "--paginate", "--jq", ".[] | select(.pull_request | not) | {number, labels: [.labels[].name]} | tojson",
    )
    order = priorities(env)
    keyed = []
    for line in lines.splitlines():
        if not line:
            continue
        issue = json.loads(line)
        names = set(issue["labels"])
        rank = min((order.index(name) for name in names if name in order), default=len(order))
        keyed.append((rank, queued_at(run, env, int(issue["number"])), int(issue["number"])))
    return [number for _, _, number in sorted(keyed)]


def comment(run: Run, env: Env, number: int, body: str) -> None:
    """Comment on an issue or PR, as the App's bot."""
    run("gh", "api", f"repos/{env['GITHUB_REPOSITORY']}/issues/{number}/comments", "-f", f"body={body}")


def go_on(run: Run, env: Env, pr: Pull, why: str, now: float) -> bool:
    """Continue a draft, unless it was just continued or went on ROUNDS
    times already. Whether it takes a place: continued now or just before.
    """
    comments = thread(run, env, pr.number)
    if continued(env, comments, now):
        print(f"#{pr.number}: continued already, starting")
        return True
    if rounds(env, comments) >= ROUNDS:
        # Left as it is, it would read as work that goes on by itself and
        # never does: finish's own edit and note, so there is one of each.
        run("claude-job-finish", "spent", str(pr.number))
        print(f"#{pr.number}: went on {ROUNDS} times already, asked the owner")
        return False
    body = f"@{BOT} continue\n\n{CONTINUE}\n{why}, and a runner is free, so the work goes on by itself."
    comment(run, env, pr.number, body)
    print(f"#{pr.number}: continued")
    return True


def start(run: Run, env: Env, number: int) -> None:
    """Take an issue off the queue and start it at its labels' level."""
    run("gh", "api", "--method", "DELETE", f"repos/{env['GITHUB_REPOSITORY']}/issues/{number}/labels/{QUEUED}")
    body = f"@{BOT} continue\n\n{FROM_QUEUE}\nA runner is free, so this issue's turn has come."
    comment(run, env, number, body)
    print(f"#{number}: started from the queue")


def next_(run: Run, env: Env, now: float) -> int:
    """Fill the free places: stale drafts, then stopped ones, then the queue.
    A place it cannot fill is a warning: the next job's step or the sweep
    fills it.
    """
    limit = cap(env)
    if not limit:
        return 0
    prs = pulls(run, env)
    free = limit - in_work(prs, now) - starting(run, env, prs, now)
    if free <= 0:
        print(f"{limit - free} of {limit} in work, nothing started")
        return 0
    drafts = sorted((pr for pr in prs if pr.draft), key=lambda pr: pr.opened)
    candidates = [(pr, "Its job ended with the host") for pr in drafts if pr.status == "working" and stale(pr, now)]
    candidates += [(pr, "It stopped with work left") for pr in drafts if pr.status == "stopped"]
    for pr, why in candidates:
        if free <= 0:
            break
        try:
            if go_on(run, env, pr, why, now):
                free -= 1
        except (KeyError, ValueError, subprocess.CalledProcessError) as error:
            print(f"::warning::#{pr.number}: {error}")
    if free > 0:
        for number in queue(run, env)[:free]:
            try:
                start(run, env, number)
            except (KeyError, ValueError, subprocess.CalledProcessError) as error:
                print(f"::warning::#{number}: {error}")
    return 0


def main(argv: list[str], env: Env, run: Run = sh, now: float | None = None) -> int:
    """Run the command named, gate or next; anything else gets the usage and 2."""
    now = time.time() if now is None else now
    command = argv[1] if len(argv) > 1 else ""
    try:
        if command == "gate":
            return gate(run, env, now)
        if command == "next":
            return next_(run, env, now)
    except (KeyError, ValueError, OSError, subprocess.CalledProcessError) as error:
        # The cap is soft: a gate that cannot read lets the job run, and a
        # place next could not fill the next job's step or the sweep fills.
        # Either is the job's first or last step, whose failure would turn
        # work that went well red.
        print(f"::warning::{error}")
        return 0
    print("usage: claude-job-queue gate | next", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv, os.environ))
