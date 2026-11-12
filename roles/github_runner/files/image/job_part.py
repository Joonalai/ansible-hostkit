#!/usr/bin/python3 -I
"""Run a long Claude job in parts, each with a GitHub token of its own.

    claude-job-part continue     the lead, at its part's stop time
    claude-job-part next         the workflow, between parts

A job's App token lasts an hour, and <bot>:full takes up to two: past
the hour every push, gh call and CI read of the lead's failed, and so did
the action's own comment. So the workflow runs Claude in parts. The lead
stops its part at the time its prompt gives, after pushing what it has,
and asks for another with `continue`, which leaves a marker in
$RUNNER_TEMP. `next` then decides: another part when the lead asked and its
part ended well, or when the part was cut off at its limit, which a lead
busy past its stop time is, and either way only while the job's time
leaves room for one worth having. GitHub ends a step at its limit as
cancelled, which a part cut off is, or as failed. A part that failed before
its limit crashed or hit a usage limit, and is not resumed.

It writes to $GITHUB_OUTPUT continue (true or false), part-minutes, stop-by
and stop, as claude-job-level does for the first part, and session, the
lead's session to resume: the action sets none for a part it cut off, so it
is the newest session transcript in ~/.claude/projects. When another part
follows, it moves this one's execution file, which the next would
overwrite, aside for claude-job-report, and records when the next part's
limit falls, as `claude-part-ends`. The workflow mints the token and
resumes the session.

It reads STARTED and MINUTES (claude-job-level's started and minutes),
OUTCOME (the part's), PART (the next part's number), RUNNER_TEMP and HOME.

Standard library only, on the image's python3.
"""

import glob
import os
import re
import sys
import time
from collections.abc import Mapping

MARKER = "claude-continue"
ENDS = "claude-part-ends"
EXECUTION = "claude-execution-output.json"
PART_MINUTES = 52
PART_STOP_MINUTES = 45
# A part that would have less than this before its lead must stop is not
# worth the session's reload; the lead stops for good instead.
SHORTEST = 10
# A part that failed or was cancelled within this of its limit was cut off by it.
CUT_OFF_SLACK = 60
SESSION = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def clock(seconds: float) -> str:
    """The time of day in UTC, as the lead is told it."""
    return time.strftime("%H:%M", time.gmtime(seconds))


def ask(env: Mapping[str, str]) -> int:
    """Leave the mark that asks for another part, for next to find."""
    with open(os.path.join(env["RUNNER_TEMP"], MARKER), "w") as file:
        file.write("continue\n")
    print("Asked for another part: end your turn now. It resumes this session with a fresh token.")
    return 0


def session(home: str) -> str:
    """The lead's session: the newest transcript named by a session's id.
    A job's container is its own, so they are all this job's, and a
    subagent's sits under its lead's directory or as agent-<id>.
    """
    found = [
        path
        for path in glob.glob(os.path.join(glob.escape(home), ".claude", "projects", "*", "*.jsonl"))
        if SESSION.fullmatch(os.path.basename(path)[: -len(".jsonl")])
    ]
    return os.path.basename(max(found, key=os.path.getmtime))[: -len(".jsonl")] if found else ""


def part_ends(temp: str, started: int, minutes: int) -> float:
    """When the part that just ended had its limit: recorded for a later
    part, the level's for the first.
    """
    try:
        with open(os.path.join(temp, ENDS)) as file:
            return float(file.read())
    except (OSError, ValueError):
        return started + min(minutes, PART_MINUTES) * 60


def plan(env: Mapping[str, str], now: float) -> dict[str, str]:
    """Whether another part follows, asked for or after one cut off at its
    limit, and its time, stop and session.
    """
    temp = env["RUNNER_TEMP"]
    marker = os.path.join(temp, MARKER)
    asked = os.path.exists(marker)
    if asked:
        os.unlink(marker)
    started, minutes = int(env["STARTED"]), int(env["MINUTES"])
    job_end, job_stop = started + minutes * 60, started + (minutes - 10) * 60
    part_stop = min(now + PART_STOP_MINUTES * 60, job_stop)
    outcome = env.get("OUTCOME")
    cut_off = outcome in ("failure", "cancelled") and now >= part_ends(temp, started, minutes) - CUT_OFF_SLACK
    wanted = (asked and outcome == "success") or cut_off
    resume = session(env.get("HOME", ""))
    if not wanted or not resume or part_stop - now < SHORTEST * 60:
        return {"continue": "false"}
    # Only for a part that follows: the last one's file stays where the
    # report reads it. Moved, so that a cut-off part, which writes none,
    # does not count the one before it twice.
    execution = os.path.join(temp, EXECUTION)
    if os.path.exists(execution):
        os.replace(execution, os.path.join(temp, f"claude-execution-output.part-{int(env['PART']) - 1}.json"))
    part_minutes = min(PART_MINUTES, int((job_end - now) // 60))
    with open(os.path.join(temp, ENDS), "w") as file:
        file.write(f"{now + part_minutes * 60}\n")
    if part_stop >= job_stop:
        stop = f"Stop by {clock(job_stop)} UTC."
    else:
        stop = (
            f"Stop this part by {clock(part_stop)} UTC; with claude-job-part continue, another part with a fresh "
            f"token resumes this session, and the job stops by {clock(job_stop)} UTC."
        )
    return {
        "continue": "true",
        "part-minutes": str(part_minutes),
        "stop-by": clock(part_stop),
        "stop": stop,
        "session": resume,
    }


def main(argv: list[str], env: Mapping[str, str] = os.environ, now: float | None = None) -> int:
    """Ask for another part, or write whether one follows to the step's outputs."""
    if argv == ["continue"]:
        return ask(env)
    if argv == ["next"]:
        outputs = plan(env, time.time() if now is None else now)
        with open(env["GITHUB_OUTPUT"], "a") as file:
            file.writelines(f"{key}={value}\n" for key, value in outputs.items())
        return 0
    print("usage: claude-job-part continue | next", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
