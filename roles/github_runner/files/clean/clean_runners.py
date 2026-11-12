#!/usr/bin/python3 -I
r"""Throw away the containers, images and dependency caches coder's jobs leave behind.

Every rebuild of the runner image, and every test service a job builds, leaves
the previous image untagged in coder's storage, a gigabyte or two each:
the dev container had 16 GB of them after a day of work. And every
repository's jobs share one dependency cache, uv's, pip's and npm's, which
only grows. coder's timer runs this daily:

    clean-runners --cache-max-gb 5 --strays-max-age-hours 12 \
        /home/coder/cache/app=github-runner-app-1,github-runner-app-2 ...

First it removes the containers a job started on its own, with `docker
run` rather than compose, and left behind: a database for its tests, a
service under a name of its own. A stray is a container in no compose
project, not a systemd unit's (the runners are quadlets) and not named
github-runner-*, created longer ago than the hours given, which is far
longer than any job runs. Its volumes stay.

Then it removes the dangling images, which are the untagged ones no container
and no other image uses. The layers of the images still tagged stay, so
the next build is as cached as ever. Then each cache over the limit is
emptied, but only while none of its repository's runners is working a job:
a job runs in a Runner.Worker beside the listener. A busy repository's
cache waits for the next day. The directory itself stays, since the idle
runners have it mounted.

Standard library only, on the system python3.
"""

import argparse
import contextlib
import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from typing import Any

WORKER = "Runner.Worker"
PROJECT = "com.docker.compose.project"
UNIT = "PODMAN_SYSTEMD_UNIT"


def podman(*args: str) -> subprocess.CompletedProcess[str]:
    """A podman call, whose failure is the caller's to read."""
    return subprocess.run(["podman", *args], capture_output=True, text=True, check=False)


def size(path: str) -> int:
    """Bytes under path, not following links, as du would count them."""
    total = 0
    for root, dirs, files in os.walk(path):
        for name in dirs + files:
            with contextlib.suppress(FileNotFoundError):
                total += os.lstat(os.path.join(root, name)).st_size
    return total


def empty(path: str) -> None:
    """Remove what is in path, skipping what coder may not: a container's
    subordinate uid can write there through the socket.
    """
    for entry in os.scandir(path):
        try:
            if entry.is_dir(follow_symlinks=False):
                shutil.rmtree(entry.path)
            else:
                os.unlink(entry.path)
        except OSError as e:
            print(f"Left {entry.path}: {e}", file=sys.stderr)


def strays(containers: list[dict[str, Any]], max_age_s: float, now: float) -> dict[str, str]:
    """Containers older than max_age_s that no compose project, no systemd
    unit and no runner owns, by name, with their IDs.
    """
    found = {}
    for container in containers:
        labels = container.get("Labels") or {}
        name = (container.get("Names") or [container["Id"]])[0]
        if PROJECT in labels or UNIT in labels or name.startswith("github-runner-"):
            continue
        if now - container.get("Created", now) > max_age_s:
            found[name] = container["Id"]
    return dict(sorted(found.items()))


def remove_strays(run: Callable[..., subprocess.CompletedProcess[str]], max_age_hours: float, now: float, *, dry_run: bool) -> None:
    """Remove the containers jobs left outside any compose project, keeping their volumes."""
    listed = run("ps", "--all", "--format", "json")
    if listed.returncode != 0:
        print(f"strays: could not list the containers: {listed.stderr.strip()}", file=sys.stderr)
        return
    for name, container_id in strays(json.loads(listed.stdout or "[]"), max_age_hours * 3600, now).items():
        if dry_run:
            print(f"{name}: a container of no job's, made over {max_age_hours:g} hours ago, would remove")
            continue
        run("rm", "--force", "--volumes=false", container_id)
        print(f"{name}: a container of no job's, made over {max_age_hours:g} hours ago, removed")


def is_busy(top: str) -> bool:
    """Whether a runner's process list has a job's worker in it."""
    return WORKER in top.split()


def cache_and_units(arg: str) -> tuple[str, list[str]]:
    """An argument's cache directory and the runner units that use it."""
    cache, _, units = arg.partition("=")
    return cache, [u for u in units.split(",") if u]


def main(
    argv: list[str],
    run: Callable[..., subprocess.CompletedProcess[str]] = podman,
    measure: Callable[[str], int] = size,
    wipe: Callable[[str], None] = empty,
    now: float | None = None,
) -> int:
    """Remove the strays and the dangling images, then empty each cache over the limit whose runners are idle."""
    parser = argparse.ArgumentParser(prog="clean-runners")
    parser.add_argument("--cache-max-gb", type=float, required=True)
    parser.add_argument("--strays-max-age-hours", type=float)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("caches", nargs="*", help="CACHE_DIR=UNIT,UNIT...")
    args = parser.parse_args(argv)

    if args.strays_max_age_hours is not None:
        remove_strays(run, args.strays_max_age_hours, time.time() if now is None else now, dry_run=args.dry_run)
    if args.dry_run:
        dangling = run("images", "--quiet", "--filter", "dangling=true")
        print(f"images: would remove {len(dangling.stdout.split())} dangling")
    else:
        pruned = run("image", "prune", "--force")
        if pruned.returncode != 0:
            print(f"images: prune failed: {pruned.stderr.strip()}", file=sys.stderr)
            return 1
        print(f"images: removed {len(pruned.stdout.split())} dangling")

    limit = int(args.cache_max_gb * 1024**3)
    for cache, units in map(cache_and_units, args.caches):
        used = measure(cache)
        if used <= limit:
            print(f"{cache}: {used / 1024**3:.1f} GB, under the limit")
            continue
        tops = [run("top", unit, "comm") for unit in units]
        # No container is a runner between jobs, about to start fresh.
        if any(top.returncode == 0 and is_busy(top.stdout) for top in tops):
            print(f"{cache}: {used / 1024**3:.1f} GB, but a job is running; tomorrow")
            continue
        if args.dry_run:
            print(f"{cache}: {used / 1024**3:.1f} GB and idle, would empty")
            continue
        wipe(cache)
        print(f"{cache}: {used / 1024**3:.1f} GB and idle, emptied")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
