#!/usr/bin/python3 -I
"""Stop a Claude job whose issue changed after someone allowed vouched for it.

    claude-job-vouch          a workflow step: comment and fail on a refusal
    claude-job-vouch --hook   Claude Code's PreToolUse hook: block the command

A label says who added it, not what the issue said then. Claude reads the
title and description when the job starts and again whenever it runs
`gh issue view`, so an author off the allow-list could edit them while the
job queues, while it runs, or before a later "@<bot> continue". So:

- The vouch is the latest <bot>* label, other than <bot>:proposed, added
  by someone in CLAUDE_ALLOWED_IDS. A "@<bot>" comment does not renew it,
  since a "continue" is not a re-read of the description.
- A change needing one is the item's creation, a description edit or a
  title rename, by anyone neither in CLAUDE_ALLOWED_IDS nor the Claude App's
  bot (CLAUDE_APP_BOT_ID), which edits its own PRs all the time. A deleted
  account counts as off the list.
- The newest such change must be older than the vouch. One with no vouch at
  all is refused too: a stranger's issue is worked only once labelled.
- A pull request opened by someone off the list is refused whatever its
  labels say. Its commits are code a job would run, and a label is no
  vouch for commits pushed after it.
- A PR is checked together with the issues it closes, which the lead reads
  when it carries on, and with the issue it is a part of, whose
  description's first line says `Part <k>/<n> of #<issue>`, since a part
  closes nothing until the last.

As a step it runs before Claude, and again before the push of what Claude
left, and on a refusal comments on the issue or PR and exits 1. As the
hook it lets every command through untouched except a push, `gh pr`,
`gh issue`, `gh api` and `claude-job-pr-comments`, which wait for a fresh check and are blocked,
exit 2, on a refusal; Claude is told why and to stop. Either way, anything
it cannot check, an API failure or a missing variable, is a refusal.

The hook also blocks any command carrying the mark of the workflow's own
"@<bot> continue", which claude-job-finish posts when a job's time ran
out, or of its "@<bot> quick: rebase", which claude-job-rebase posts on
a PR that conflicts with the default branch, or of claude-job-retry's wait
and its "@<bot> continue" after it, or of the relayed review that asks
for changes. The workflow takes those from the App's bot: only those steps
may start another job, and only so many times in a row, so Claude never
writes any of them.

As a step it also counts, for a job the App's bot started with a marked
comment, the bot's marked "@<bot>" comments on the thread since someone
on the allow-list last commented, and refuses past MAX_UNASKED. Each
script that writes one keeps to three of its own kind, but Claude posts as
the same bot, and a mark the hook missed, one written to a file and
posted with --body-file say, would otherwise start jobs with no end. The
cap is above what the scripts together may write.

It reads GITHUB_EVENT_PATH and GITHUB_REPOSITORY, which every step and
Claude itself get, CLAUDE_ALLOWED_IDS (a JSON list of numeric user ids)
and CLAUDE_APP_BOT_ID. gh finds its token in GH_TOKEN.

Standard library only, on the image's python3.
"""

import datetime as dt
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable, Mapping
from typing import Any, TextIO, cast

# The bot's name, the consumer's: what a comment mentions to start a job,
# and what the labels and the workflow's own marks begin with. The runner's
# unit sets it. Letters, digits and dashes only, since it goes into patterns.
BOT = os.environ.get("CLAUDE_BOT") or "claude"
if not re.fullmatch(r"[A-Za-z0-9-]+", BOT):
    raise SystemExit(f"CLAUDE_BOT is not a name: {BOT!r}")

FIELDS = """
  createdAt
  author { login ...ids }
  lastEditedAt
  editor { login ...ids }
  userContentEdits(last: 100) { nodes { editedAt editor { login ...ids } } }
  newestEdits: userContentEdits(first: 100) { nodes { editedAt editor { login ...ids } } }
  timelineItems(itemTypes: [LABELED_EVENT, RENAMED_TITLE_EVENT], last: 100) {
    nodes {
      __typename
      ... on LabeledEvent { createdAt actor { login ...ids } label { name } }
      ... on RenamedTitleEvent { createdAt actor { login ...ids } }
    }
  }
"""
QUERY = f"""
fragment ids on Actor {{ ... on User {{ databaseId }} ... on Bot {{ databaseId }} }}
query($owner: String!, $name: String!, $number: Int!) {{
  repository(owner: $owner, name: $name) {{
    issueOrPullRequest(number: $number) {{
      __typename
      ... on Issue {{ number {FIELDS} }}
      ... on PullRequest {{
        number body {FIELDS}
        closingIssuesReferences(first: 10) {{ nodes {{ number {FIELDS} }} }}
      }}
    }}
  }}
}}
"""

# The first line of a PR that is one part of an issue done in parts.
PART = re.compile(r"^Part (?:\d+/\d+ )?of #(\d+)\s*$")
# Labels that say a state, not that someone stands behind the text.
NOT_VOUCHING = {f"{BOT}:proposed", f"{BOT}:waiting"}
# What the hook waits on: whatever pushes, or reads or writes the thread.
# Loose on purpose: a commit message saying "push" costs one needless query.
# The marks of the workflow's own comments, claude-job-finish's, -rebase's
# and -retry's, loosely, in case of other spacing.
AUTO = re.compile(rf"{re.escape(BOT)}:auto-")
# The most jobs the bot's marked comments may start in a row, all kinds
# together: continue, rebase and retry three each, and a few of the rest.
MAX_UNASKED = 12
GUARDED = re.compile(r"\bgit\b[^;&|]*\bpush\b|\bgh\s+(pr|issue|api)\b|\bclaude-job-pr-comments\b")

COMMENT = """Claude has stopped and will not act on #{number}: {reason}

Read it as it is now, and if you stand behind it, remove the label and \
add it again. That vouches for the text as it stands.
"""


# gh, giving what it prints.
type Run = Callable[..., str]


def sh(*argv: str) -> str:
    """What a command prints, raising when it fails."""
    return subprocess.run(argv, check=True, capture_output=True, text=True).stdout.strip()


def failure(error: Exception) -> str:
    """What went wrong with a check, in a line."""
    if isinstance(error, subprocess.CalledProcessError):
        return f"gh exited {error.returncode}: {(error.stderr or '').strip()[:300]}"
    return f"{type(error).__name__}: {error}"


def when(stamp: str) -> dt.datetime:
    """One of GitHub's timestamps as a time."""
    return dt.datetime.fromisoformat(stamp)


def who(actor: dict[str, Any] | None) -> int | None:
    """The actor's numeric id, or None for a deleted account."""
    return cast("int | None", (actor or {}).get("databaseId"))


# One rule after another, as the module's docstring lists them.
def refusal(item: dict[str, Any], allowed: set[int], bot: int | None) -> str | None:  # noqa: C901
    """Why this issue or PR may not be acted on, or None when it may."""
    trusted = allowed | ({bot} if bot is not None else set())
    number = item["number"]
    if item.get("__typename") == "PullRequest" and who(item.get("author")) not in trusted:
        return f"#{number} is a pull request from someone not on this repository's allow-list."

    changes = [(item["createdAt"], item.get("author"))]
    # Both ends of the edits, and the last one by its own fields: whichever
    # way GitHub orders the list, an edit past the hundredth must not be the
    # one that goes unread.
    for edits in ("userContentEdits", "newestEdits"):
        changes += [(edit["editedAt"], edit.get("editor")) for edit in (item.get(edits) or {}).get("nodes", [])]
    if item.get("lastEditedAt"):
        changes.append((item["lastEditedAt"], item.get("editor")))
    vouches = []
    for event in item["timelineItems"]["nodes"]:
        if event["__typename"] == "RenamedTitleEvent":
            changes.append((event["createdAt"], event.get("actor")))
        elif event["__typename"] == "LabeledEvent":
            name = (event.get("label") or {}).get("name", "")
            mine = name == BOT or name.startswith(f"{BOT}:")
            if mine and name not in NOT_VOUCHING and who(event.get("actor")) in allowed:
                vouches.append(when(event["createdAt"]))

    untrusted = [when(stamp) for stamp, actor in changes if who(actor) not in trusted]
    if not untrusted:
        return None
    if not vouches:
        return f"#{number} was written by someone not on this repository's allow-list, and nobody on it has labelled it."
    if max(untrusted) > max(vouches):
        return (
            f"the title or description of #{number} was changed after it was labelled, "
            "by someone not on this repository's allow-list."
        )
    return None


def fetch(run: Run, env: Mapping[str, str], number: int) -> dict[str, Any]:
    """The issue or PR with who wrote, edited, renamed and labelled it, and the issues a PR closes."""
    owner, name = env["GITHUB_REPOSITORY"].split("/")
    answer = json.loads(
        run(
            "gh", "api", "graphql",
            "-f", f"query={QUERY}", "-f", f"owner={owner}", "-f", f"name={name}", "-F", f"number={number}",
        ),
    )
    return answer["data"]["repository"]["issueOrPullRequest"]


def part_of(run: Run, env: Mapping[str, str], item: dict[str, Any]) -> list[dict[str, Any]]:
    """The issue a PR is one part of, as its description's first line says."""
    if item.get("__typename") != "PullRequest":
        return []
    first = (item.get("body") or "").replace("\r", "").strip().split("\n", 1)[0].strip()
    found = PART.match(first)
    if not found:
        return []
    issue = fetch(run, env, int(found.group(1)))
    return [issue] if issue and issue.get("__typename") == "Issue" else []


def check(env: Mapping[str, str], run: Run = sh) -> tuple[int, str | None]:
    """The issue or PR's number, and why it may not be acted on, if it may not."""
    with open(env["GITHUB_EVENT_PATH"]) as file:
        event = json.load(file)
    number = (event.get("issue") or event.get("pull_request") or {})["number"]
    allowed = set(json.loads(env["CLAUDE_ALLOWED_IDS"]))
    bot = int(env["CLAUDE_APP_BOT_ID"]) if env.get("CLAUDE_APP_BOT_ID", "") else None
    item = fetch(run, env, number)
    closing = (item.get("closingIssuesReferences") or {}).get("nodes", [])
    for each in [item, *closing, *part_of(run, env, item)]:
        reason = refusal(each, allowed, bot)
        if reason is not None:
            return cast("int", number), reason
    return cast("int", number), None


def runaway(env: Mapping[str, str], run: Run = sh) -> str | None:
    """Why a job the bot's own marked comment started may not run: too many
    of them since someone on the allow-list last said anything.
    """
    with open(env["GITHUB_EVENT_PATH"]) as file:
        event = json.load(file)
    comment = event.get("comment") or {}
    bot = env.get("CLAUDE_APP_BOT_ID", "")
    if not (bot and comment and str((comment.get("user") or {}).get("id")) == bot and AUTO.search(comment.get("body") or "")):
        return None
    allowed = {str(i) for i in json.loads(env["CLAUDE_ALLOWED_IDS"])}
    number = (event.get("issue") or {})["number"]
    lines = run(
        "gh", "api", "--paginate", f"repos/{env['GITHUB_REPOSITORY']}/issues/{number}/comments",
        "--jq", f'.[] | "\\(.user.id) \\(.body | startswith("@{BOT}") and contains("{BOT}:auto-"))"',
    ).splitlines()
    unasked = 0
    for line in lines:
        user, _, marked = line.partition(" ")
        if user in allowed:
            unasked = 0
        elif user == bot and marked == "true":
            unasked += 1
    if unasked > MAX_UNASKED:
        return (
            f"the bot has started {unasked} jobs on #{number} by itself since someone on this "
            f"repository's allow-list last commented, more than the {MAX_UNASKED} it may."
        )
    return None


def step(env: Mapping[str, str], run: Run = sh) -> int:
    """The workflow's step: on a refusal, say why on the issue or PR and fail."""
    try:
        number, reason = check(env, run)
        reason = reason or runaway(env, run)
    except (KeyError, ValueError, TypeError, subprocess.CalledProcessError) as error:
        print(f"::error::Could not check who changed the issue, so Claude does not run: {failure(error)}")
        return 1
    if reason is None:
        return 0
    print(f"::error::{reason}")
    run(
        "gh", "api", f"repos/{env['GITHUB_REPOSITORY']}/issues/{number}/comments",
        "-f", f"body={COMMENT.format(number=number, reason=reason)}",
    )
    return 1


def hook(env: Mapping[str, str], stdin: TextIO, run: Run = sh) -> int:
    """Block a command carrying one of the workflow's own marks, and on a
    refusal one that pushes or touches the thread. Every other passes.
    """
    try:
        command = json.load(stdin).get("tool_input", {}).get("command", "")
    except ValueError:
        command = ""
    if AUTO.search(command):
        print(
            "Blocked: that mark is the workflow's own, for the jobs it starts by itself. "
            "Leave it out; when the work goes on, set Progress to working and stop.",
            file=sys.stderr,
        )
        return 2
    if not GUARDED.search(command):
        return 0
    try:
        _, reason = check(env, run)
    except (KeyError, ValueError, TypeError, subprocess.CalledProcessError) as error:
        reason = f"the check of who changed the issue failed ({failure(error)})."
    if reason is None:
        return 0
    print(
        f"Blocked: {reason} Do not push, comment, or read the issue or PR again, "
        "and do not try another way round this. Stop now and end the job; "
        "the workflow tells the people on the allow-list.",
        file=sys.stderr,
    )
    return 2


def main(argv: list[str], env: Mapping[str, str] = os.environ, stdin: TextIO = sys.stdin) -> int:
    """Run as the hook or as the step, as the argument says."""
    return hook(env, stdin) if argv == ["--hook"] else step(env)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
