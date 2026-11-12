#!/usr/bin/python3 -I
"""Rebuild the workbench's image, and move the idle containers onto it.

    workbench-rebuild [--config <build.json>]

Run as the workbench account every night by a timer. The build is
rebuild-image's, from the play's root-owned config, the same one the play
builds from; it adds the ISO week, so that once a week the whole image is
built afresh, and Chromium's security updates with it. The play restarts
the containers itself on a deploy, and does not run this.

A container still running an older image is restarted onto the new one
only when no session is open in it, a session being a Claude Code process
started with --sdk-url, as Remote Control starts them. One with sessions
is left for the next night.

Standard library only, on the system python3.
"""

import argparse
import json
import subprocess
import sys
from collections.abc import Callable

CONFIG = "/usr/local/share/workbench/build.json"
REBUILD_IMAGE = "/usr/local/libexec/rebuild-image"

# run(), or what stands in for it.
Run = Callable[..., subprocess.CompletedProcess[str]]


# check is subprocess.run's own, and is passed by name as it is there.
def run(argv: list[str], check: bool = True) -> subprocess.CompletedProcess[str]:  # noqa: FBT001, FBT002
    """Run a command and capture what it prints; a failure is raised unless check is off."""
    return subprocess.run(argv, check=check, capture_output=True, text=True)


def running_image(container: str, run: Run = run) -> str | None:
    """The image a running container was started from, or None if it is not running."""
    answer = run(["podman", "container", "inspect", "--format", "{{.State.Running}} {{.Image}}", container], check=False)
    if answer.returncode != 0:
        return None
    state, _, image = answer.stdout.strip().partition(" ")
    return image if state == "true" else None


def sessions(container: str, run: Run = run) -> int:
    """How many sessions are open in a container: its processes started with --sdk-url."""
    answer = run(["podman", "top", container, "args"])
    return sum("--sdk-url" in line for line in answer.stdout.splitlines()[1:])


def main(argv: list[str], run: Run = run) -> int:
    """Build the image, then restart each idle container still on an older one."""
    parser = argparse.ArgumentParser(prog="workbench-rebuild", description=__doc__.split("\n\n")[0])
    parser.add_argument("--config", default=CONFIG)
    options = parser.parse_args(argv)
    with open(options.config) as config_file:
        config = json.load(config_file)

    try:
        new = run([REBUILD_IMAGE, "--config", options.config]).stdout.strip()
    except subprocess.CalledProcessError as error:
        print(f"workbench-rebuild: the build failed:\n{error.stderr.strip()}", file=sys.stderr)
        return 1
    print(f"image {new[:12]}")

    for container in config["containers"]:
        current = running_image(container, run)
        if current is None:
            print(f"{container}: not running")
        elif current == new:
            print(f"{container}: on the new image")
        elif count := sessions(container, run):
            print(f"{container}: {count} session(s) open, left on the old image until they close")
        else:
            run(["systemctl", "--user", "restart", f"{container}.service"])
            print(f"{container}: restarted onto the new image")
    # What no container uses any more; one still in use is kept.
    run(["podman", "image", "prune", "--force"], check=False)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
