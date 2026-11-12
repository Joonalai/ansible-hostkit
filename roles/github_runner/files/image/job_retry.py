#!/usr/bin/python3 -I
"""Start a job again by itself once what stopped it has passed, for the
workflow's steps.

    claude-job-retry limit | wait | sweep

Two failures leave nothing a person needs to decide, only time to pass: a
job that failed before Claude started, at a download GitHub answered with
504, and one whose Claude stopped at the subscription's usage limit. Both
used to wait for a comment by hand.

- limit, after the last part: whether Claude stopped at the usage limit,
  from the result of the last part's execution file. If so, it writes
  wait-until, when the limit resets, to $GITHUB_OUTPUT, read from the
  message when it says, or two hours on when it does not, and
  claude-job-finish leaves Progress at working.
- wait: says on the thread what stopped the job and when it starts again,
  as the App's bot, with the time in a hidden mark, and labels the issue or
  PR <bot>:waiting. WAIT_UNTIL is the time, or empty for a job that
  failed before Claude, which tries again in RETRY_MINUTES. After ROUNDS
  such restarts since someone on the allow-list last commented, it says the
  work waits for them instead, and labels nothing.
- sweep, on the workflow's schedule: for each open issue and PR labelled
  <bot>:waiting whose time has come, takes the label off and comments
  "@<bot> continue", marked as the workflow's own, which the workflow
  takes from the bot as from a person. It asks for the level the first
  comment did. Someone on the allow-list commenting meanwhile has taken
  the work over: the label just goes.

It reads from the environment: GITHUB_REPOSITORY, CLAUDE_ALLOWED_IDS and
CLAUDE_APP_BOT_ID, and for limit OUTCOME (the last part's) and RUNNER_TEMP
or EXECUTION_FILE; for wait NUMBER, KIND (before or limit), WAIT_UNTIL,
ASKED (claude-job-level's), RUN_URL, and GITHUB_OUTPUT. gh finds its token
in GH_TOKEN.

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
from zoneinfo import ZoneInfo

# The bot's name, the consumer's: what a comment mentions to start a job,
# and what the labels and the workflow's own marks begin with. The runner's
# unit sets it. Letters, digits and dashes only, since it goes into patterns.
BOT = os.environ.get("CLAUDE_BOT") or "claude"
if not re.fullmatch(r"[A-Za-z0-9-]+", BOT):
    raise SystemExit(f"CLAUDE_BOT is not a name: {BOT!r}")

LABEL = f"{BOT}:waiting"
# The note that says when, not a trigger: it never starts with @<bot>.
WAIT = re.compile(rf"<!-- {re.escape(BOT)}:auto-wait at=(\d+) asked=(\w*) -->")
# The comment that starts the job again.
AUTO = f"<!-- {BOT}:auto-retry -->"
ROUNDS = 3
RETRY_MINUTES = 10
# Unread reset times, and a margin past a read one.
FALLBACK_HOURS = 2
MARGIN_MINUTES = 5
LIMIT = re.compile(r"usage limit|limit reached|hit your limit|limit will reset", re.IGNORECASE)
EPOCH = re.compile(r"limit reached\|(\d{10})\b", re.IGNORECASE)
RESETS = re.compile(
    r"resets?\s+(?:at\s+)?(?:(?P<day>mon|tue|wed|thu|fri|sat|sun)[a-z]*\s+)?"
    r"(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<half>am|pm)?(?:\s*\((?P<zone>[^)]+)\))?",
    re.IGNORECASE,
)
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
# A clock's noon, and its last hour and minute.
NOON = 12
LAST_HOUR = 23
LAST_MINUTE = 59

# What runs a command and gives its output, sh or a test's stand-in, and
# the environment the steps hand over.
Run = Callable[..., str]
Env = Mapping[str, str]


def sh(*argv: str) -> str:
    """Run a command and give what it printed; a failure raises."""
    return subprocess.run(argv, check=True, capture_output=True, text=True).stdout.strip()


def zone(name: str | None) -> dt.tzinfo:
    """The time zone a message names, or UTC when it names none the image knows."""
    # An empty name is refused like an unknown one, so it is UTC too.
    if name is not None:
        try:
            return ZoneInfo(name.strip())
        except Exception:  # noqa: BLE001, S110 (an unknown name, or no zone data in the image)
            pass
    return dt.UTC


def reset_time(text: str, now: float) -> float:
    """When the limit the message names resets, or FALLBACK_HOURS on."""
    if found := EPOCH.search(text):
        return int(found.group(1)) + MARGIN_MINUTES * 60
    found = RESETS.search(text)
    if not found:
        return now + FALLBACK_HOURS * 3600
    hour, minute, half = int(found["hour"]), int(found["minute"] or 0), (found["half"] or "").lower()
    if half == "pm" and hour < NOON:
        hour += NOON
    if half == "am" and hour == NOON:
        hour = 0
    if hour > LAST_HOUR or minute > LAST_MINUTE:
        return now + FALLBACK_HOURS * 3600
    here = dt.datetime.fromtimestamp(now, zone(found["zone"]))
    at = here.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if found["day"]:
        at += dt.timedelta(days=(DAYS.index(found["day"][:3].lower()) - at.weekday()) % 7)
    while at.timestamp() <= now:
        at += dt.timedelta(days=7 if found["day"] else 1)
    return at.timestamp() + MARGIN_MINUTES * 60


def stopped_at_limit(env: Env) -> str | None:
    """The last part's result text, when it says the usage limit stopped it."""
    if env.get("OUTCOME") == "cancelled":
        return None
    path = env.get("EXECUTION_FILE") or os.path.join(env.get("RUNNER_TEMP", ""), "claude-execution-output.json")
    try:
        with open(path) as execution_file:
            messages = json.load(execution_file)
    except (OSError, ValueError):
        return None
    result = next((m for m in reversed(messages) if isinstance(m, dict) and m.get("type") == "result"), {})
    text = str(result.get("result") or "")
    failed = env.get("OUTCOME") == "failure" or result.get("is_error")
    return text if failed and LIMIT.search(text) else None


def limit(env: Env, now: float) -> int:
    """Say when to start again, in the step's wait-until output, when the usage limit stopped Claude."""
    text = stopped_at_limit(env)
    if text is not None:
        at = int(reset_time(text, now))
        print(f"Claude stopped at the usage limit; starting again after {clock(at)} UTC.")
        with open(env["GITHUB_OUTPUT"], "a") as output_file:
            output_file.write(f"wait-until={at}\n")
    return 0


def clock(at: float) -> str:
    """A time as its weekday, hour and minute, in UTC."""
    return time.strftime("%a %H:%M", time.gmtime(at))


def comments(run: Run, env: Env, number: str) -> list[tuple[str, str]]:
    """A thread's comments, oldest first, each as its author's id and its body."""
    lines = run(
        "gh", "api", f"repos/{env['GITHUB_REPOSITORY']}/issues/{number}/comments", "--paginate",
        "--jq", ".[] | [(.user.id | tostring), .body] | tojson",
    )
    return [tuple(json.loads(line)) for line in lines.splitlines() if line]


def since_allowed(env: Env, thread: list[tuple[str, str]]) -> list[str]:
    """The bot's comments since someone on the allow-list last commented."""
    allowed = {str(each) for each in json.loads(env["CLAUDE_ALLOWED_IDS"])}
    left: list[str] = []
    for user, body in thread:
        if user in allowed:
            left = []
        elif user == env["CLAUDE_APP_BOT_ID"]:
            left.append(body)
    return left


def target(run: Run, env: Env) -> str:
    """The job's PR, when its branch has one, or the issue or PR it started on."""
    if env.get("KIND") == "limit":
        branch = run("git", "branch", "--show-current")
        if branch and branch != env.get("DEFAULT_BRANCH"):
            found = run("gh", "pr", "list", "--head", branch, "--state", "open", "--json", "number", "--jq", ".[0].number")
            if found:
                return found
    return env["NUMBER"]


def wait(run: Run, env: Env, now: float) -> int:
    """Say on the thread when the job starts again and label it as waiting, or after ROUNDS in a row that it does not."""
    number = target(run, env)
    repo = env["GITHUB_REPOSITORY"]
    before = env.get("KIND") != "limit"
    at = now + RETRY_MINUTES * 60 if before else int(env["WAIT_UNTIL"])
    what = (
        "The job failed before Claude started, at one of the workflow's own steps, and changed nothing"
        if before else "Claude stopped at the subscription's usage limit"
    )
    done = sum(AUTO in body for body in since_allowed(env, comments(run, env, number)))
    url = env["RUN_URL"]
    if done >= ROUNDS:
        again = "Re-run it, or comment again." if before else f"Comment `@{BOT} continue` once the limit has reset."
        body = f"{what}, and it has started again by itself {done} times in a row already. {again} [Job run]({url})"
        run("gh", "api", f"repos/{repo}/issues/{number}/comments", "-f", f"body={body}")
        return 0
    asked = env.get("ASKED", "") if env.get("ASKED") in ("quick", "review", "full") else ""
    body = (
        f"{what}. It starts again by itself after {clock(at)} UTC, {done + 1} of {ROUNDS} in a row; "
        f"a comment of yours before then goes first. [Job run]({url})\n\n"
        f"<!-- {BOT}:auto-wait at={int(at)} asked={asked} -->"
    )
    run("gh", "api", f"repos/{repo}/issues/{number}/comments", "-f", f"body={body}")
    run("gh", "api", f"repos/{repo}/issues/{number}/labels", "-f", f"labels[]={LABEL}")
    return 0


def due(env: Env, thread: list[tuple[str, str]], now: float) -> tuple[str, str] | None:
    """('go', asked) when the wait is over, ('drop', '') when someone took
    the work over or no wait is left, or None while it runs.
    """
    allowed = {str(each) for each in json.loads(env["CLAUDE_ALLOWED_IDS"])}
    for user, body in reversed(thread):
        if user in allowed:
            return ("drop", "")
        if user == env["CLAUDE_APP_BOT_ID"] and (found := WAIT.search(body)):
            return ("go", str(found.group(2))) if now >= int(found.group(1)) else None
    return ("drop", "")


def sweep(run: Run, env: Env, now: float) -> int:
    """Start again each waiting thread whose time has come, and drop the label where someone took the work over."""
    repo = env["GITHUB_REPOSITORY"]
    numbers = run(
        "gh", "api", f"repos/{repo}/issues?state=open&labels={LABEL}&per_page=100", "--paginate", "--jq", ".[].number",
    ).split()
    failed = False
    for number in numbers:
        try:
            verdict = due(env, comments(run, env, number), now)
            if verdict is None:
                print(f"#{number}: still waiting")
                continue
            run("gh", "api", "--method", "DELETE", f"repos/{repo}/issues/{number}/labels/{LABEL}")
            if verdict[0] == "drop":
                print(f"#{number}: taken over, label removed")
                continue
            level = f" {verdict[1]}:" if verdict[1] else ""
            body = (
                f"@{BOT}{level} continue\n\n{AUTO}\nThe wait is over, so the work starts again by itself: "
                "what the job before was asked, from Next in the PR's Progress where there is one."
            )
            run("gh", "api", f"repos/{repo}/issues/{number}/comments", "-f", f"body={body}")
            print(f"#{number}: started again")
        except (KeyError, ValueError, subprocess.CalledProcessError) as error:
            print(f"::warning::#{number}: {error}")
            failed = True
    return 1 if failed else 0


def main(argv: list[str], env: Env, run: Run = sh, now: float | None = None) -> int:
    """Run the command named, limit, wait or sweep; anything else gets the usage and 2."""
    now = time.time() if now is None else now
    command = argv[1] if len(argv) > 1 else ""
    if command == "limit":
        return limit(env, now)
    try:
        if command == "wait":
            return wait(run, env, now)
        if command == "sweep":
            return sweep(run, env, now)
    except (KeyError, ValueError, subprocess.CalledProcessError) as error:
        print(f"::error::{error}")
        return 1
    print("usage: claude-job-retry limit | wait | sweep", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv, os.environ))
