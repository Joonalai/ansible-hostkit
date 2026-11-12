#!/usr/bin/python3 -I
"""Print the comments on a pull request, for a lead reading a linked PR.

    claude-job-pr-comments <repository name> <number>

The action puts only the triggering PR's comments in the lead's prompt, and
`gh pr view` leaves out inline review comments. So when the user reviews
several linked PRs and starts one job, this is how the job reads the others:
conversation comments, review summaries and inline comments, oldest first.

Only what someone on CLAUDE_ALLOWED_IDS or the Claude App's bot
(CLAUDE_APP_BOT_ID) wrote is printed, as the workflow's own filter does for
the prompt: anyone else's text is counted, never shown. It matches numeric
ids, which unlike a login cannot be renamed and taken over, and a missing
variable is a failure rather than an unfiltered thread.

The repository is this one or a name from CLAUDE_RELATED_REPOS, under
GITHUB_REPOSITORY's owner. gh finds its token in GH_TOKEN.

Standard library only, on the image's python3.
"""

import json
import os
import re
import subprocess
import sys
from collections.abc import Callable, Mapping
from typing import Any

NAME = re.compile(r"[A-Za-z0-9._-]+")
# The repository's name and the PR's number.
ARGUMENTS = 2

# gh, giving what it prints.
type Run = Callable[..., str]


def sh(*argv: str) -> str:
    """What a command prints, raising when it fails."""
    return subprocess.run(argv, check=True, capture_output=True, text=True).stdout.strip()


def related(env: Mapping[str, str]) -> list[str]:
    """The related repositories the repository's variable names, without what is no name."""
    names = [name.strip() for name in env.get("CLAUDE_RELATED_REPOS", "").split(",")]
    return [name for name in names if NAME.fullmatch(name) and name.strip(".")]


def pages(run: Run, path: str) -> list[dict[str, Any]]:
    """Everything a paginated path lists, its pages joined."""
    return [each for page in json.loads(run("gh", "api", "--paginate", "--slurp", path)) for each in page]


def entries(run: Run, repo: str, number: str) -> list[dict[str, Any]]:
    """Every comment as {when, user, where, body}, in no particular order."""
    found = [
        {"when": each["created_at"], "user": each.get("user"), "where": "comment", "body": each.get("body")}
        for each in pages(run, f"repos/{repo}/issues/{number}/comments")
    ]
    for each in pages(run, f"repos/{repo}/pulls/{number}/reviews"):
        # A review of inline comments alone has an empty body, and one
        # still pending has no time and is its author's alone.
        if each.get("body") and each.get("submitted_at"):
            where = f"review, {each.get('state', '').lower()}"
            found.append({"when": each["submitted_at"], "user": each.get("user"), "where": where, "body": each["body"]})
    for each in pages(run, f"repos/{repo}/pulls/{number}/comments"):
        line = each.get("line") or each.get("original_line") or "?"
        where = f"inline on {each.get('path')}:{line}"
        found.append({"when": each["created_at"], "user": each.get("user"), "where": where, "body": each.get("body")})
    return found


def render(found: list[dict[str, Any]], trusted: set[int]) -> str:
    """The comments of those on the allow-list, oldest first, and how many were left out."""
    shown = [each for each in found if (each["user"] or {}).get("id") in trusted]
    blocks = [
        f"--- {each['when']} {each['user'].get('login')} ({each['where']})\n{each['body']}"
        for each in sorted(shown, key=lambda each: each["when"])
    ]
    if len(found) > len(shown):
        blocks.append(f"--- left out: {len(found) - len(shown)} from people not on the allow-list")
    return "\n\n".join(blocks) or "No comments."


def main(argv: list[str], env: Mapping[str, str] = os.environ, run: Run = sh) -> int:
    """Print the allow-list's comments on a PR of this repository or a related one."""
    if len(argv) != ARGUMENTS or not argv[1].isdecimal():
        print("usage: claude-job-pr-comments <repository name> <number>", file=sys.stderr)
        return 2
    name, number = argv
    owner, own = env["GITHUB_REPOSITORY"].split("/")
    if name != own and name not in related(env):
        print(f"{name} is neither this repository nor one of its related repositories", file=sys.stderr)
        return 1
    trusted = set(json.loads(env["CLAUDE_ALLOWED_IDS"])) | {int(env["CLAUDE_APP_BOT_ID"])}
    print(render(entries(run, f"{owner}/{name}", number), trusted))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
