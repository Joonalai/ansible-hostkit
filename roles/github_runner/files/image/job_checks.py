#!/usr/bin/python3 -I
"""Wait for a PR's checks, and say what failed.

    claude-job-checks <number> [-R owner/name]    wait, and print the result
    claude-job-checks --hook                      Claude Code's PreToolUse hook
    claude-job-checks --waiting <number>          a step's: has someone asked since

A job's turn ends at its last push, long before CI has said anything, so
nothing would otherwise look at it. The lead runs this after that push. It
polls until every check on the PR's head has finished, up to nine minutes so
that one call fits Claude Code's longest Bash timeout, and prints each failed
check with the end of its log, and whether the same check fails on the base
too. A job's own run is left out, since on a review event GitHub lists it
among the PR's checks and it could only ever be running.

It exits 0 when nothing failed, or there are no checks; 1 when one failed; 2
when GitHub would not say; and 3 when they are still running.

As the hook it lets every command through except `gh pr ready`, which it
blocks, exit 2, unless the named PR's branch has every commit of its base
and its checks have all finished and none failed. A branch behind its base
was checked against a base that is no longer there, so green says little.
Anything it cannot read is a refusal, so a PR it cannot vouch for stays a
draft, which is the visible way to be wrong. A `--undo` passes.

The hook also blocks it while someone on the allow-list has asked for more
on the PR since this job's run was created: a `@<bot>` comment, review
or review comment. That comment started a job of its own, waiting behind
this one in the same concurrency group, and a PR marked ready over it reads
as done when it is not. A job that started hours after it was asked for,
as one waiting for a runner does, saw the comment in its prompt, and still
did only what it was started for.

`--waiting` is the same question as a step's, for a rebase the bot asked
for itself, which the workflow skips when someone has asked since: their
job comes next and rebases as it works. It writes `waiting=true` or
`waiting=false` to $GITHUB_OUTPUT and exits 0, `false` too when it cannot
read, so the rebase runs as it would have.

It reads GITHUB_REPOSITORY, GITHUB_WORKFLOW, GITHUB_RUN_ID and
CLAUDE_ALLOWED_IDS from the environment, which every step and Claude itself
get. gh finds its token in GH_TOKEN, which needs Actions, Checks and Commit
statuses: read, on the App, and Issues and Pull requests: read.

Standard library only, on the image's python3.
"""

import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from typing import Any, TextIO, cast

# The bot's name, the consumer's: what a comment mentions to start a job,
# and what the labels and the workflow's own marks begin with. The runner's
# unit sets it. Letters, digits and dashes only, since it goes into patterns.
BOT = os.environ.get("CLAUDE_BOT") or "claude"
if not re.fullmatch(r"[A-Za-z0-9-]+", BOT):
    raise SystemExit(f"CLAUDE_BOT is not a name: {BOT!r}")

POLL = 15
# A workflow's checks appear some seconds after the push, and one that has
# appeared and passed says nothing of those still to come.
SETTLE = 30
LIMIT = 540
TAIL = 40
READY = re.compile(r"(?:^|[;&|(\n])\s*gh\s+pr\s+ready\b([^;&|)\n]*)")
PULL = re.compile(r"github\.com/([\w.-]+/[\w.-]+)/pull/(\d+)")
JOB = re.compile(r"/runs/(\d+)/job/(\d+)")
STAMP = re.compile(r"^\S*\d{4}-\d\d-\d\dT[\d:.]+Z\s?")
# Lower case, as the bodies it is looked for in are made.
MENTION = f"@{BOT}".lower()
FIX = "Fix what you can. What stays red stays a draft: say which check and why in your comment."
# Where someone can ask on a PR, and when each says it was written.
# `--waiting` and the PR's number.
WAITING_ARGUMENTS = 2

ASKING = [("issues/{n}/comments", "created_at"), ("pulls/{n}/reviews", "submitted_at"), ("pulls/{n}/comments", "created_at")]


# gh, giving how it ended and what it printed.
type Run = Callable[..., subprocess.CompletedProcess[str]]
# A check as `gh pr checks --json` prints it.
type Check = dict[str, Any]


# The name says what it is about, and the tests know it by this one.
class Unreadable(Exception):  # noqa: N818
    """GitHub would not say, or said what cannot be read."""


def sh(*argv: str) -> subprocess.CompletedProcess[str]:
    """A command as it ended, failed or not: the caller reads what it printed."""
    return subprocess.run(argv, check=False, capture_output=True, text=True)


def read(number: str, repo: str, env: Mapping[str, str], run: Run = sh) -> list[Check]:
    """The PR's checks, without the job's own. Empty when it has none."""
    done = run("gh", "pr", "checks", number, "-R", repo, "--json", "name,bucket,link,workflow")
    if "no checks reported" in done.stderr:
        return []
    try:
        checks = json.loads(done.stdout)
    except ValueError:
        raise Unreadable((done.stderr or done.stdout).strip()[:300]) from None
    return [check for check in checks if check.get("workflow") != env.get("GITHUB_WORKFLOW")]


def failed(checks: list[Check]) -> list[Check]:
    """Those that failed or were cancelled."""
    return [check for check in checks if check["bucket"] in ("fail", "cancel")]


def running(checks: list[Check]) -> list[Check]:
    """Those that have not finished."""
    return [check for check in checks if check["bucket"] == "pending"]


def label(check: Check) -> str:
    """The check's name, after its workflow's when it has one."""
    return f"{check['workflow']} / {check['name']}" if check.get("workflow") else cast("str", check["name"])


def log_tail(check: Check, repo: str, run: Run = sh) -> str:
    """The end of a failed check's log, or why there is none to show."""
    found = JOB.search(check.get("link") or "")
    if not found:
        return "(no log: the check is not a workflow's)"
    done = run("gh", "run", "view", found[1], "--job", found[2], "--log-failed", "-R", repo)
    if done.returncode:
        return f"(log unreadable: {done.stderr.strip()[:200]})"
    lines = [STAMP.sub("", line.split("\t")[-1])[:300] for line in done.stdout.splitlines()]
    return "\n".join(lines[-TAIL:])


def failing_on_base(number: str, repo: str, run: Run = sh) -> set[str]:
    """Best effort: what is red on the base says only where a failure began."""
    done = run("gh", "pr", "view", number, "-R", repo, "--json", "baseRefOid", "--jq", ".baseRefOid")
    if done.returncode:
        return set()
    done = run(
        "gh", "api", f"repos/{repo}/commits/{done.stdout.strip()}/check-runs?per_page=100",
        "--jq", '[.check_runs[] | select(.conclusion == "failure") | .name]',
    )
    try:
        return set(json.loads(done.stdout))
    except ValueError:
        return set()


# One argument more than the rules like: the clock and the sleep are the tests' to replace.
def wait(  # noqa: PLR0913, PLR0917
    number: str,
    repo: str,
    env: Mapping[str, str],
    run: Run = sh,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], object] = time.sleep,
) -> list[Check]:
    """The checks once they have finished, or as they are at the limit."""
    start = clock()
    while True:
        checks = read(number, repo, env, run)
        waited = clock() - start
        if waited >= LIMIT or (waited >= SETTLE and not running(checks)):
            return checks
        sleep(POLL)


def result(number: str, repo: str, checks: list[Check], run: Run = sh) -> int:
    """Print what failed, what still runs or that all passed, and give the exit code for it."""
    red, unfinished = failed(checks), running(checks)
    base = failing_on_base(number, repo, run) if red else set()
    for check in red:
        where = " (also failing on the base)" if check["name"] in base else ""
        print(f"FAILED: {label(check)}{where}\n{check.get('link', '')}\n{log_tail(check, repo, run)}\n")
    if unfinished:
        print("Still running: " + ", ".join(label(check) for check in unfinished))
    if red:
        return 1
    if unfinished:
        return 3
    if checks and all(check["bucket"] == "skipping" for check in checks):
        print(
            f"Skipped: all {len(checks)} checks, so CI has tried nothing yet. A repository may run"
            " them only once the PR is ready: after `gh pr ready`, run claude-job-checks again.",
        )
    else:
        print(f"Passed: all {len(checks)} checks." if checks else "This PR has no checks.")
    return 0


def named(arguments: str, env: Mapping[str, str]) -> tuple[str | None, str]:
    """The number and repository a `gh pr ready` names."""
    words = arguments.split()
    repo, number = env.get("GITHUB_REPOSITORY", ""), None
    # Each word with the one before it, which is one more than there are words.
    for before, word in zip(["", *words], words, strict=False):
        if before in ("-R", "--repo"):
            repo = word
        elif word.isdigit():
            number = word
        elif url := PULL.search(word):
            repo, number = str(url[1]), str(url[2])
    return number, repo


def behind(number: str, repo: str, run: Run = sh) -> tuple[str, int]:
    """The PR's base branch, and how many of its commits the head lacks."""
    done = run("gh", "pr", "view", number, "-R", repo, "--json", "baseRefName,headRefOid")
    try:
        pr = json.loads(done.stdout)
        base, head = pr["baseRefName"], pr["headRefOid"]
    except (ValueError, KeyError, TypeError):
        raise Unreadable((done.stderr or done.stdout).strip()[:300]) from None
    done = run("gh", "api", f"repos/{repo}/compare/{base}...{head}", "--jq", ".behind_by")
    if done.returncode or not done.stdout.strip().isdigit():
        raise Unreadable((done.stderr or done.stdout).strip()[:300])
    return cast("str", base), int(done.stdout)


def asked_since(number: str, repo: str, env: Mapping[str, str], run: Run = sh) -> list[str]:
    """The links of what someone on the allow-list asked of the bot on the
    PR since this job's run was created, oldest first.
    """
    try:
        allowed = {str(each) for each in json.loads(env["CLAUDE_ALLOWED_IDS"])}
        own = env["GITHUB_REPOSITORY"]
        done = run("gh", "api", f"repos/{own}/actions/runs/{env['GITHUB_RUN_ID']}", "--jq", ".created_at")
    except (KeyError, ValueError, TypeError) as error:
        raise Unreadable(f"no {error} in the environment") from None
    since = done.stdout.strip()
    if done.returncode or not since:
        raise Unreadable((done.stderr or done.stdout).strip()[:300])
    found = []
    for path, at in ASKING:
        done = run(
            "gh", "api", f"repos/{repo}/{path.format(n=number)}?per_page=100", "--paginate",
            "--jq", f".[] | {{user: .user.id, at: .{at}, url: .html_url, body}} | tojson",
        )
        if done.returncode:
            raise Unreadable((done.stderr or done.stdout).strip()[:300])
        for line in done.stdout.splitlines():
            try:
                each = json.loads(line)
            except ValueError:
                raise Unreadable(line[:300]) from None
            if (
                str(each.get("user")) in allowed and (each.get("at") or "") > since
                and MENTION in (each.get("body") or "").lower()
            ):
                found.append((each["at"], each.get("url") or ""))
    return [url for _, url in sorted(found)]


def refusal(arguments: str, env: Mapping[str, str], run: Run = sh) -> str | None:
    """Why the PR a `gh pr ready` names may not be marked ready, or None."""
    if "--undo" in arguments.split():
        return None
    number, repo = named(arguments, env)
    if number is None:
        return f"name the PR: `gh pr ready <number>`, with `-R <owner>/<repo>` for a linked PR. {FIX}"
    try:
        asked = asked_since(number, repo, env, run)
    except Unreadable as error:
        return f"whether anyone asked for more on {repo}#{number} since this job started could not be read ({error})."
    if asked:
        return (
            f"since this job started, the user asked for more on {repo}#{number}: {' '.join(asked)}. "
            "That started a job of its own, which comes after this one, so leave the PR a draft and "
            "say in your comment that the next job takes what was asked."
        )
    reason = unready(number, repo, env, run)
    return f"{reason} {FIX}" if reason is not None else None


def unready(number: str, repo: str, env: Mapping[str, str], run: Run = sh) -> str | None:
    """Why the PR's branch or checks are not ready, or None."""
    try:
        base, missing = behind(number, repo, run)
    except Unreadable as error:
        return f"whether {repo}#{number} is behind its base could not be read ({error})."
    if missing:
        return (
            f"{repo}#{number} is {missing} commit(s) behind `{base}`: `git fetch origin {base}`, "
            f"`git rebase origin/{base}`, run the tests, push, and wait for the checks with "
            f"`claude-job-checks {number} -R {repo}` before marking it ready."
        )
    try:
        checks = read(number, repo, env, run)
    except Unreadable as error:
        return f"the checks of {repo}#{number} could not be read ({error})."
    red, unfinished = failed(checks), running(checks)
    if red:
        return f"{repo}#{number} has failing checks: {', '.join(map(label, red))}."
    if unfinished:
        return f"the checks of {repo}#{number} are still running; wait with `claude-job-checks {number} -R {repo}`."
    return None


def hook(env: Mapping[str, str], stdin: TextIO, run: Run = sh) -> int:
    """Block a command that marks a PR ready before it is, and let every other through."""
    try:
        command = json.load(stdin).get("tool_input", {}).get("command", "")
    except ValueError:
        return 0
    for arguments in READY.findall(command):
        reason = refusal(arguments, env, run)
        if reason is not None:
            print(f"Blocked: {reason}", file=sys.stderr)
            return 2
    return 0


def waiting(number: str, env: Mapping[str, str], run: Run = sh) -> int:
    """The step's answer, in $GITHUB_OUTPUT. Always 0: a rebase it cannot
    vouch for skipping runs as it would have.
    """
    try:
        asked = asked_since(number, env.get("GITHUB_REPOSITORY", ""), env, run)
    except Unreadable as error:
        print(f"::warning::Whether anyone asked on #{number} since could not be read, so the job goes on: {error}")
        asked = []
    if asked:
        print(f"::notice::Asked for since this job's run was created, so that job does it: {' '.join(asked)}")
    if output := env.get("GITHUB_OUTPUT", ""):
        with open(output, "a") as file:
            file.write(f"waiting={'true' if asked else 'false'}\n")
    return 0


def main(argv: list[str], env: Mapping[str, str] = os.environ, stdin: TextIO = sys.stdin, run: Run = sh) -> int:
    """Answer as the hook or the step, or wait for the named PR's checks and print the result."""
    if argv == ["--hook"]:
        return hook(env, stdin, run)
    if len(argv) == WAITING_ARGUMENTS and argv[0] == "--waiting" and argv[1].isdigit():
        return waiting(argv[1], env, run)
    number, repo = named(" ".join(argv), env)
    if number is None or not repo:
        print("usage: claude-job-checks <number> [-R owner/name]", file=sys.stderr)
        return 2
    try:
        return result(number, repo, wait(number, repo, env, run), run)
    except Unreadable as error:
        print(f"The checks could not be read: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
