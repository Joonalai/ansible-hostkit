#!/usr/bin/python3 -I
"""Decide a Claude job's level and time limit, for the workflow's first step.

    claude-job-level

It reads the event from $GITHUB_EVENT_PATH and writes the step's outputs to
$GITHUB_OUTPUT:

- level: the highest level label on the issue or PR. Claude labels its PR
  with the issue's level, so "@<bot> continue" on the PR keeps it. Any
  working label outranks <bot>:plan, so adding one to a planned issue
  starts the work. "@<bot> quick:", "@<bot> review:" or "@<bot>
  full:" sets it for that one comment instead; review: is what the rebase
  runner asks with when it hands a rebase on.
- label-level: the labels' level, whatever the comment asked, for the PR's
  own label.
- asked: quick, review or full when the comment asked for it, or empty, so that a
  job started again by itself asks the same.
- note: a phrase for the lead's prompt when the comment chose the level,
  and when <bot>:ui applies: a modifier, not a level, which with
  <bot>:review or <bot>:full adds the UI tester's walkthrough of the
  preview, and its time. On its own it starts nothing, since the workflow's
  trigger does not list it. It also names the repositories in
  CLAUDE_RELATED_REPOS, the ones a job may work in beside this one.
- minutes: the time the level may take, job-stop-by: ten minutes before
  that runs out, in UTC, and started: when, in seconds since the epoch.
- part-minutes and stop-by: the first part's limit and the time its lead
  stops by. A job's GitHub token lasts an hour, so a level longer than a
  part comes in parts, each with a fresh token, resuming the same session:
  claude-job-part decides the next. stop-by is the first part's, so a
  workflow that knows nothing of parts still stops its lead in time.
- stop: the sentence for the lead's prompt that says both.

The comment is read from the event file, never from the command line, so
nothing in it can reach a shell.

Standard library only, on the image's python3.
"""

import json
import os
import re
import sys
import time
from collections.abc import Mapping
from typing import Any

# The bot's name, the consumer's: what a comment mentions to start a job,
# and what the labels and the workflow's own marks begin with. The runner's
# unit sets it. Letters, digits and dashes only, since it goes into patterns.
BOT = os.environ.get("CLAUDE_BOT") or "claude"
if not re.fullmatch(r"[A-Za-z0-9-]+", BOT):
    raise SystemExit(f"CLAUDE_BOT is not a name: {BOT!r}")

# Highest first; the first one on the issue or PR wins.
LEVELS = [(f"{BOT}:full", 100), (f"{BOT}:review", 60), (BOT, 40), (f"{BOT}:plan", 20)]
DEFAULT = (BOT, 40)
ASKED = {"quick": (BOT, 30), "review": (f"{BOT}:review", 60), "full": (f"{BOT}:full", 100)}
# The walkthrough's label, the levels that have a team for it, and its time.
UI = f"{BOT}:ui"
UI_LEVELS = {f"{BOT}:review", f"{BOT}:full"}
UI_MINUTES = 20
# A part's limit and the lead's stop within it, inside the token's hour,
# which starts a minute or two before this step.
PART_MINUTES = 52
PART_STOP_MINUTES = 45
ASKING = re.compile(rf"@{re.escape(BOT)}\s+(quick|review|full):", re.IGNORECASE)
NAME = re.compile(r"[A-Za-z0-9._-]+")


def related(env: Mapping[str, str]) -> list[str]:
    """The related repositories the repository's variable names, without what is no name."""
    names = [name.strip() for name in env.get("CLAUDE_RELATED_REPOS", "").split(",")]
    return [name for name in names if NAME.fullmatch(name) and name.strip(".")]


def levels(event: dict[str, Any], repositories: list[str] | None = None) -> dict[str, str]:
    """The job's level, from the labels or the comment that asks for one,
    with its minutes and a note of it for the lead's prompt.
    """
    item = event.get("issue") or event.get("pull_request") or {}
    labels = {label.get("name") for label in item.get("labels") or []}
    label_level, minutes = next(((name, level_minutes) for name, level_minutes in LEVELS if name in labels), DEFAULT)
    body = (event.get("comment") or {}).get("body") or (event.get("review") or {}).get("body") or ""
    found = ASKING.search(body)
    asked = found.group(1).lower() if found else ""
    level, minutes = ASKED.get(asked, (label_level, minutes))
    note = f", asked for with {asked}: in this comment, while the labels say {label_level}" if asked else ""
    if asked == "full":
        note += ", so call every role that level names, whatever the comment's size"
    if UI in labels and level in UI_LEVELS:
        minutes += UI_MINUTES
        note += f", with the UI walkthrough: the labels include {UI}"
    if repositories is not None and repositories:
        note += f", and this repository's related repositories are {', '.join(repositories)}"
    return {"level": level, "label-level": label_level, "asked": asked, "note": note, "minutes": str(minutes)}


def clock(seconds: float) -> str:
    """The time of day in UTC, as the lead is told it."""
    return time.strftime("%H:%M", time.gmtime(seconds))


def stop_sentence(part_stop: float, job_stop: float) -> str:
    """What the lead is told about when to stop: this part, and the job when more parts fit."""
    if part_stop >= job_stop:
        return f"Stop by {clock(job_stop)} UTC."
    return (
        f"Stop this part by {clock(part_stop)} UTC; with claude-job-part continue, another part with a fresh "
        f"token resumes this session, and the job stops by {clock(job_stop)} UTC."
    )


def parts(minutes: int, now: float) -> dict[str, str]:
    """The first part's time and stop, within a job of that many minutes."""
    job_stop = now + (minutes - 10) * 60
    part_stop = min(now + PART_STOP_MINUTES * 60, job_stop)
    return {
        "started": str(int(now)),
        "job-stop-by": clock(job_stop),
        "part-minutes": str(min(minutes, PART_MINUTES)),
        "stop-by": clock(part_stop),
        "stop": stop_sentence(part_stop, job_stop),
    }


def main(env: Mapping[str, str] = os.environ, now: float | None = None) -> int:
    """Write the level and the first part's time to the step's outputs."""
    with open(env["GITHUB_EVENT_PATH"]) as file:
        outputs = levels(json.load(file), related(env))
    outputs.update(parts(int(outputs["minutes"]), time.time() if now is None else now))
    with open(env["GITHUB_OUTPUT"], "a") as file:
        file.writelines(f"{key}={value}\n" for key, value in outputs.items())
    return 0


if __name__ == "__main__":
    sys.exit(main())
