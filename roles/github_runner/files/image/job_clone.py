#!/usr/bin/python3 -I
"""Clone a related repository beside the job's checkout.

    claude-job-clone <name>

A repository lists the ones a job may also work in, by name, in its
CLAUDE_RELATED_REPOS variable, and the workflow's token covers exactly those.
This clones one of them next to the checkout, so that a job that touches one
repository pays for no other. A name off the list is refused, and a clone
already there is left as it is, so asking twice is harmless.

git asks gh for its credentials, in the clone's own config, rather than
holding a token: gh reads GH_TOKEN, so the workflow's fresh token works there
after Claude's has expired, and no token lands in a remote's URL. The commits
are the bot's, as in the checkout, and a repository's commit message rules
hold here as they do there, through its own hook.

It reads CLAUDE_RELATED_REPOS, GITHUB_REPOSITORY, GITHUB_WORKSPACE, BOT_NAME
and BOT_EMAIL from the environment, and prints the clone's path.

Standard library only, on the image's python3.
"""

import os
import re
import subprocess
import sys
from collections.abc import Callable, Mapping

NAME = re.compile(r"[A-Za-z0-9._-]+")
CREDENTIALS = "!gh auth git-credential"


def sh(*argv: str) -> str:
    """What a command prints, raising when it fails."""
    return subprocess.run(argv, check=True, capture_output=True, text=True).stdout.strip()


def related(env: Mapping[str, str]) -> list[str]:
    """The related repositories the repository's variable names, without what is no name."""
    names = [name.strip() for name in env.get("CLAUDE_RELATED_REPOS", "").split(",")]
    return [name for name in names if NAME.fullmatch(name) and name.strip(".")]


def main(argv: list[str], env: Mapping[str, str] = os.environ, run: Callable[..., str] = sh) -> int:
    """Clone the named related repository beside the checkout, unless it is
    there already, and print where it is.
    """
    if len(argv) != 1:
        print("usage: claude-job-clone <name>", file=sys.stderr)
        return 2
    name = argv[0]
    allowed = related(env)
    if name not in allowed:
        print(f"{name} is not one of this repository's related repositories: {allowed}", file=sys.stderr)
        return 1
    owner = env["GITHUB_REPOSITORY"].split("/")[0]
    target = os.path.join(os.path.dirname(env["GITHUB_WORKSPACE"].rstrip("/")), name)
    if not os.path.exists(target):
        run(
            "git", "clone", "--quiet",
            # The first, empty, drops any helper set globally for this clone.
            "--config", "credential.helper=",
            "--config", f"credential.helper={CREDENTIALS}",
            "--config", f"user.name={env['BOT_NAME']}",
            "--config", f"user.email={env['BOT_EMAIL']}",
            f"https://github.com/{owner}/{name}.git", target,
        )
        if os.path.exists(os.path.join(target, ".pre-commit-config.yaml")):
            run("prek", "--cd", target, "install", "--hook-type", "commit-msg")
    print(target)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
