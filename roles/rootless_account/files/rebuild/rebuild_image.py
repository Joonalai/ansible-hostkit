#!/usr/bin/python3 -I
"""Build an account's image from the play's config, afresh once a week.

    rebuild-image --config <build.json>

Run as the account whose image it is, by the play and every night by the
including role's timer, so the two never build differently. The config is
the play's, root-owned: the image, the build context, the build options
and arguments. The build adds one argument of its own, the ISO week as
REFRESH, which the Containerfile declares before its first layer, so that
once a week the whole image is built afresh from an updated Debian, and
its security updates with it; within a week the cache makes it a no-op.
It prints the image's ID; moving containers onto it is the caller's.

Standard library only, on the system python3.
"""

import argparse
import datetime as dt
import json
import subprocess
import sys
from collections.abc import Callable
from typing import Any

# run(), or what stands in for it.
Run = Callable[[list[str]], subprocess.CompletedProcess[str]]


def run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    """Run a command and capture what it prints; a failure is raised."""
    return subprocess.run(argv, check=True, capture_output=True, text=True)


def week(today: dt.date) -> str:
    """The ISO week a day is in, which turns on Monday."""
    return today.strftime("%G-W%V")


def build(config: dict[str, Any], today: dt.date, run: Run = run) -> None:
    """Build the image with the config's options and arguments, and the week as REFRESH."""
    args = {**config["args"], "REFRESH": week(today)}
    run(
        [
            "podman",
            "build",
            "--pull=newer",
            *config.get("options", []),
            *(f"--build-arg={key}={value}" for key, value in sorted(args.items())),
            "--tag",
            config["image"],
            config["context"],
        ],
    )


def main(argv: list[str], run: Run = run, today: dt.date | None = None) -> int:
    """Build the image the config names and print its ID."""
    parser = argparse.ArgumentParser(prog="rebuild-image", description=__doc__.split("\n\n")[0])
    parser.add_argument("--config", required=True)
    options = parser.parse_args(argv)
    with open(options.config) as config_file:
        config = json.load(config_file)
    try:
        # The week is the host's own, in its own time zone.
        build(config, today or dt.date.today(), run)  # noqa: DTZ011
    except subprocess.CalledProcessError as error:
        print(f"rebuild-image: the build failed:\n{error.stderr.strip()}", file=sys.stderr)
        return 1
    print(run(["podman", "image", "inspect", "--format", "{{.Id}}", config["image"]]).stdout.strip())
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
