#!/usr/bin/python3 -I
r"""Restart the idle runners still on an old image or old rules.

Every job is a new container, so a runner picks up a rebuilt image, a
rewritten CLAUDE.md or a changed unit when its next job starts, and needs
no restart for it. An idle one, though, keeps what it had when it started
until it gets a job, and its first job then runs on the old. Restarting
every runner fixes that and kills whatever job is running, however long
it has worked. So the play runs
this as coder:

    refresh-runners --image localhost/github-runner:latest \
        github-runner-app-1=/home/coder/claude/app-1/CLAUDE.md,... ...

Each argument names a runner unit, whose container has the same name, and
the files its container depends on. A runner is restarted only when its
container is on another image than the one named, or started before one of
its files last changed, and only when no job is running in it: a job runs
in a Runner.Worker beside the listener. A busy runner is left alone, and
its next container is fresh anyway. No container at all is a runner
between jobs, already starting fresh.

A restart that fails stops none of the others: the unit's Restart=always
tries again, as it does for a registration GitHub refuses or a network that
is down, and the play's verification waits for every repository's runner.

A job can still be handed to a runner in the instant between the check and
the restart; the window is a podman call wide.

Standard library only, on the system python3.
"""

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable
from typing import Any

WORKER = "Runner.Worker"


def podman(*args: str) -> subprocess.CompletedProcess[str]:
    """A podman call, whose failure is the caller's to read."""
    return subprocess.run(["podman", *args], capture_output=True, text=True, check=False)


def systemctl(*args: str) -> subprocess.CompletedProcess[str]:
    """A call to the account's own systemd, whose failure is the caller's to read."""
    return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True, check=False)


def started(stamp: str) -> float:
    """Podman's StartedAt, which has nanoseconds; Python takes six digits."""
    return dt.datetime.fromisoformat(re.sub(r"(\.\d{6})\d+", r"\1", stamp)).timestamp()


def why_stale(container: dict[str, Any], image_id: str, files: dict[str, float]) -> str | None:
    """What the container is behind on, or None when it is current."""
    if container["Image"] != image_id:
        return "an older image"
    since = started(container["State"]["StartedAt"])
    newer = [path for path, mtime in files.items() if mtime > since]
    return f"an older {', '.join(os.path.basename(p) for p in newer)}" if newer else None


def is_busy(top: str) -> bool:
    """Whether a runner's process list has a job's worker in it."""
    return WORKER in top.split()


def unit_and_files(arg: str) -> tuple[str, list[str]]:
    """An argument's runner unit and the files its container depends on."""
    unit, _, files = arg.partition("=")
    return unit, [f for f in files.split(",") if f]


def main(
    argv: list[str],
    run: Callable[..., subprocess.CompletedProcess[str]] = podman,
    restart: Callable[..., subprocess.CompletedProcess[str]] = systemctl,
    mtime: Callable[[str], float] = os.path.getmtime,
) -> int:
    """Restart each runner named that is behind on its image or files and has no job."""
    parser = argparse.ArgumentParser(prog="refresh-runners")
    parser.add_argument("--image", required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("runners", nargs="+", help="UNIT=FILE,FILE...")
    args = parser.parse_args(argv)

    image = run("image", "inspect", "--format", "{{.Id}}", args.image)
    if image.returncode != 0:
        print(f"No image {args.image}: {image.stderr.strip()}", file=sys.stderr)
        return 1
    image_id = image.stdout.strip()

    for unit, paths in map(unit_and_files, args.runners):
        inspected = run("container", "inspect", "--format", "json", unit)
        if inspected.returncode != 0:
            print(f"{unit}: between jobs, starting fresh")
            continue
        container = json.loads(inspected.stdout)[0]
        reason = why_stale(container, image_id, {p: mtime(p) for p in paths})
        if reason is None:
            print(f"{unit}: current")
            continue
        top = run("top", unit, "comm")
        if top.returncode != 0 or is_busy(top.stdout):
            print(f"{unit}: on {reason}, but working a job; its next container is fresh")
            continue
        if args.dry_run:
            print(f"{unit}: on {reason} and idle, would restart")
            continue
        done = restart("restart", f"{unit}.service")
        if done.returncode != 0:
            print(f"{unit}: restart failed, and systemd tries again: {done.stderr.strip()}")
            continue
        print(f"{unit}: on {reason} and idle, restarted")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
