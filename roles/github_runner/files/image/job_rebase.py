#!/usr/bin/python3 -I
"""Ask for a rebase of each of the bot's PRs that conflicts with the
default branch, and start the next part of an issue done in parts, for
the workflow's job on a push to it.

    claude-job-rebase

A PR only behind the default branch merges as it is, so it is left alone:
rebasing it would only push again and run CI again. One that conflicts
cannot be merged until someone rebases it, which was a comment the user
wrote by hand after every merge. This writes it instead, as the App's bot,
`@<bot> quick: rebase` marked as the workflow's own, which the workflow
takes from the bot as from a person, and Claude rebases the PR, resolves
the conflicts and runs the checks again.

Only the bot's own PRs, from this repository, to the default branch, and
not drafts, whose job rebases them as it works. Not one with a job of this
workflow running or waiting on it, found by the run's title, which is the
PR's or its issue's: a second job waiting in the same concurrency group
replaces the first one waiting, so a mention could be lost. And only ROUNDS
times since someone on the allow-list last commented on the PR, so a PR
Claude cannot rebase does not go round with every merge.

GitHub works out whether a PR merges in the background after a push, so
this asks again a few times while it says nothing yet.

An issue too large for one job is done in parts, a PR each, whose
descriptions start `Part <k>/<n> of #<issue>`, the last one's
`Closes #<issue>`. When the push merged one of those parts but the last,
this comments `@<bot> continue` on the issue, marked as the
workflow's own, and the next part starts at the issue's labels' level:
the user's merge is their yes to the part, and the label they put on the
issue covers all of it. Only for the bot's own PR from this repository, an
open issue that still carries a working label, no job on the issue
running or waiting, no open PR of the bot's already on it, and once per
merged part.

It reads from the environment: DEFAULT_BRANCH, CLAUDE_ALLOWED_IDS and
CLAUDE_APP_BOT_ID, and GitHub's own GITHUB_REPOSITORY, GITHUB_SHA and
GITHUB_RUN_ID. gh finds its token in GH_TOKEN. It exits 1 when any PR
could not be checked, after the others.

Standard library only, on the image's python3.
"""

import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from typing import Any, cast

# The bot's name, the consumer's: what a comment mentions to start a job,
# and what the labels and the workflow's own marks begin with. The runner's
# unit sets it. Letters, digits and dashes only, since it goes into patterns.
BOT = os.environ.get("CLAUDE_BOT") or "claude"
if not re.fullmatch(r"[A-Za-z0-9-]+", BOT):
    raise SystemExit(f"CLAUDE_BOT is not a name: {BOT!r}")

# The mark the workflow's work job asks of the bot's comment, followed by
# the commit it was written for.
AUTO = f"<!-- {BOT}:auto-rebase"
ROUNDS = 3
TRIES, WAIT = 6, 10
# The line a PR's description starts with, naming its issue: the whole of
# it, or one part, or a part the workflow opened before the lead said which.
ISSUE = re.compile(r"^(?:Closes|Part (?:\d+/\d+ )?of) #(\d+)\s*$", re.MULTILINE)
PART = re.compile(r"^Part (\d+)/(\d+) of #(\d+)\s*$")
# The mark of the comment that starts the next part, followed by the PR
# that was merged.
NEXT = f"<!-- {BOT}:auto-next"
WORKING = {BOT, f"{BOT}:review", f"{BOT}:full"}


# gh, giving what it prints.
type Run = Callable[..., str]


def sh(*argv: str) -> str:
    """What a command prints, raising when it fails."""
    return subprocess.run(argv, check=True, capture_output=True, text=True).stdout.strip()


def open_prs(run: Run, env: Mapping[str, str]) -> list[dict[str, Any]]:
    """The bot's open PRs from this repository to the default branch."""
    repo = env["GITHUB_REPOSITORY"]
    lines = run(
        "gh", "api", f"repos/{repo}/pulls?state=open&base={env['DEFAULT_BRANCH']}&per_page=100", "--paginate",
        "--jq", ".[] | {number, title, draft, body, user: .user.id, head: .head.repo.full_name} | tojson",
    )
    found = [json.loads(line) for line in lines.splitlines() if line]
    return [pr for pr in found if str(pr["user"]) == env["CLAUDE_APP_BOT_ID"] and pr["head"] == repo]


def prs(listed: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Those of them a rebase may be asked of: drafts are their job's."""
    return [pr for pr in listed if not pr["draft"]]


def first_line(body: str | None) -> str:
    """The first line of a description, or '' when it has none."""
    return (body or "").replace("\r", "").strip().split("\n", 1)[0].strip()


def issue_of(pr: dict[str, Any]) -> str:
    """The issue the PR's description names on its first line, or ''."""
    found = ISSUE.match(first_line(pr.get("body")))
    return cast("str", found.group(1)) if found else ""


def merged_parts(run: Run, env: Mapping[str, str]) -> list[dict[str, int]]:
    """The parts, but the last, of an issue done in parts that the pushed
    commit merged: the bot's own PRs from this repository.
    """
    repo = env["GITHUB_REPOSITORY"]
    lines = run(
        "gh", "api", f"repos/{repo}/commits/{env['GITHUB_SHA']}/pulls",
        "--jq", ".[] | {number, merged_at, body, user: .user.id, base: .base.ref, head: .head.repo.full_name} | tojson",
    )
    found: list[dict[str, int]] = []
    for line in lines.splitlines():
        pr = json.loads(line) if line else {}
        part = PART.match(first_line(pr.get("body")))
        if not (part and pr.get("merged_at")) or str(pr["user"]) != env["CLAUDE_APP_BOT_ID"]:
            continue
        if pr["base"] != env["DEFAULT_BRANCH"] or pr["head"] != repo:
            continue
        part_number, part_total, issue = (int(each) for each in part.groups())
        if part_number < part_total:
            found.append({"number": cast("int", pr["number"]), "part": part_number, "of": part_total, "issue": issue})
    return found


def conflicts(run: Run, env: Mapping[str, str], number: int, sleep: Callable[[float], object]) -> bool | None:
    """Whether the PR conflicts, or None when GitHub has not said."""
    for attempt in range(TRIES):
        said = run("gh", "api", f"repos/{env['GITHUB_REPOSITORY']}/pulls/{number}", "--jq", ".mergeable")
        if said in ("true", "false"):
            return said == "false"
        if attempt < TRIES - 1:
            sleep(WAIT)
    return None


def busy(run: Run, env: Mapping[str, str]) -> set[str]:
    """The titles of this workflow's runs, other than pushes, not yet done."""
    repo = env["GITHUB_REPOSITORY"]
    workflow = run("gh", "api", f"repos/{repo}/actions/runs/{env['GITHUB_RUN_ID']}", "--jq", ".workflow_id")
    lines = run(
        "gh", "api", f"repos/{repo}/actions/workflows/{workflow}/runs?per_page=100",
        "--jq", '.workflow_runs[] | select(.status != "completed" and .event != "push") | .display_title',
    )
    return set(lines.splitlines())


def titles(run: Run, env: Mapping[str, str], pr: dict[str, Any]) -> set[str]:
    """What a run on this PR's work is called: the PR's title, or its issue's."""
    found = {cast("str", pr["title"])}
    if issue := issue_of(pr):
        found.add(run("gh", "api", f"repos/{env['GITHUB_REPOSITORY']}/issues/{issue}", "--jq", ".title"))
    return found


def rounds(run: Run, env: Mapping[str, str], number: int) -> int:
    """The rebases asked for by the bot since someone on the allow-list
    last commented on the PR.
    """
    allowed = {str(each) for each in json.loads(env["CLAUDE_ALLOWED_IDS"])}
    lines = run(
        "gh", "api", f"repos/{env['GITHUB_REPOSITORY']}/issues/{number}/comments", "--paginate",
        "--jq", f'.[] | "\\(.user.id) \\(.body | contains("{AUTO}"))"',
    )
    count = 0
    for line in lines.splitlines():
        user, marked = line.split()
        if user in allowed:
            count = 0
        elif user == env["CLAUDE_APP_BOT_ID"] and marked == "true":
            count += 1
    return count


def note(env: Mapping[str, str], again: int) -> str:
    """The comment that asks for a rebase, marked as the workflow's own."""
    sha = env["GITHUB_SHA"]
    return (
        f"@{BOT} quick: rebase\n\n{AUTO} {sha} -->\n"
        f"`{env['DEFAULT_BRANCH']}` moved on to {sha[:7]} and this PR conflicts with it. Rebase it onto "
        f"`{env['DEFAULT_BRANCH']}`, resolve the conflicts keeping what both sides meant, and run the checks "
        f"again. Asked by the workflow itself, {again} of {ROUNDS} in a row."
    )


def settle(run: Run, env: Mapping[str, str], pr: dict[str, Any], running: set[str], sleep: Callable[[float], object]) -> str:
    """What was done with one PR, in a word or two for the log."""
    number = pr["number"]
    if conflicts(run, env, number, sleep) is not True:
        return "merges as it is, or GitHub has not said"
    if titles(run, env, pr) & running:
        return "conflicts, but a job is on it"
    done = rounds(run, env, number)
    if done >= ROUNDS:
        return f"conflicts, but asked {done} times in a row already"
    run("gh", "api", f"repos/{env['GITHUB_REPOSITORY']}/issues/{number}/comments", "-f", f"body={note(env, done + 1)}")
    return "conflicts, rebase asked for"


def asked_next(run: Run, env: Mapping[str, str], issue: int, number: int) -> bool:
    """Whether the bot already started the part after #number on the issue."""
    mark = f"{NEXT} #{number} -->"
    lines = run(
        "gh", "api", f"repos/{env['GITHUB_REPOSITORY']}/issues/{issue}/comments", "--paginate",
        "--jq", f'.[] | select(.user.id == {int(env["CLAUDE_APP_BOT_ID"])}) | .body | contains("{mark}")',
    )
    return "true" in lines.split()


def start_next(run: Run, env: Mapping[str, str], part: dict[str, int], listed: list[dict[str, Any]], running: set[str]) -> str:
    """What was done about the part after a merged one, for the log."""
    repo, issue, number = env["GITHUB_REPOSITORY"], part["issue"], part["number"]
    found = json.loads(run(
        "gh", "api", f"repos/{repo}/issues/{issue}",
        "--jq", "{state, title, pr: (.pull_request != null), labels: [.labels[].name]}",
    ))
    if found["pr"] or found["state"] != "open":
        return f"part {part['part']} of {part['of']} merged, but #{issue} is no open issue"
    if not WORKING & set(found["labels"]):
        return f"part {part['part']} of {part['of']} merged, but #{issue} carries no working label"
    if found["title"] in running:
        return f"part {part['part']} of {part['of']} merged, but a job is on #{issue}"
    if any(issue_of(pr) == str(issue) for pr in listed):
        return f"part {part['part']} of {part['of']} merged, and a PR of #{issue} is open already"
    if asked_next(run, env, issue, number):
        return f"part {part['part']} of {part['of']} merged, and the next was asked for already"
    body = (
        f"@{BOT} continue\n\n{NEXT} #{number} -->\n"
        f"Part {part['part']} of {part['of']}, #{number}, is merged, so part {part['part'] + 1} starts by itself, "
        f"as the Parts section of #{number} plans it. Remove this issue's working label to stop the parts here."
    )
    run("gh", "api", f"repos/{repo}/issues/{issue}/comments", "-f", f"body={body}")
    return f"part {part['part']} of {part['of']} merged, part {part['part'] + 1} asked for"


def main(env: Mapping[str, str], run: Run = sh, sleep: Callable[[float], object] = time.sleep) -> int:
    """Ask for a rebase of each PR that conflicts and start the next part of
    each merged one, going on past one that cannot be checked.
    """
    try:
        listed = open_prs(run, env)
        parts = merged_parts(run, env)
        running = busy(run, env) if listed or parts else set()
    except (KeyError, ValueError, subprocess.CalledProcessError) as error:
        print(f"::error::Could not list the PRs, the merged parts or the runs: {error}")
        return 1
    failed = False
    for pr in prs(listed):
        try:
            print(f"#{pr['number']}: {settle(run, env, pr, running, sleep)}")
        except (KeyError, ValueError, subprocess.CalledProcessError) as error:
            print(f"::warning::#{pr['number']}: could not check it: {error}")
            failed = True
    for part in parts:
        try:
            print(f"#{part['issue']}: {start_next(run, env, part, listed, running)}")
        except (KeyError, ValueError, subprocess.CalledProcessError) as error:
            print(f"::warning::#{part['issue']}: could not start its next part: {error}")
            failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(os.environ))
