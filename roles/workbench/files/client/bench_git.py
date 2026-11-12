#!/usr/bin/python3 -I
"""Push the current branch, or open or edit its PR, through the gate.

    bench-git push [--force]
    bench-git pr --title <title> [--body-file <file>]
    bench-git pr-edit <number> [--title <title>] [--body-file <file>]
    bench-git check

Run in the workbench's container, in a checkout of its repository. The
session's token only reads, so every write to GitHub goes through the gate,
which runs on the host as another account and checks what it is given; this
only packs it up. The gate's socket is the repository's own, mounted into
this container alone, so the gate knows the repository already: what
crosses is the action, the branch checked out, and a git bundle of that
branch or the PR's title and body as JSON.

--force replaces the branch on GitHub, but the gate allows it only while
GitHub still has what the workbench itself last pushed there: a branch
someone pushed from elsewhere is built on, never replaced.

Standard library only.
"""

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
from collections.abc import Callable
from typing import IO

SOCKET = "/run/workbench-gate.sock"


def git(*args: str) -> str:
    """What git prints, in the checkout this runs in; a failure is raised."""
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout.strip()


def current_branch() -> str:
    """The branch checked out here, which is the one the gate is asked about."""
    branch = subprocess.run(["git", "symbolic-ref", "-q", "--short", "HEAD"], check=False, capture_output=True, text=True).stdout.strip()
    if not branch:
        sys.exit("bench-git: no branch is checked out")
    return branch


def connect() -> socket.socket:
    """A connection to the gate's socket for this container's repository."""
    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    conn.connect(SOCKET)
    return conn


def gate(*args: str, payload: IO[bytes] | None = None, data: bytes = b"", connect: Callable[[], socket.socket] = connect) -> int:
    """Send the request and its input, print what the gate says, return its status.

    The gate may refuse before reading the input, and hang up; what it said
    is still there to read.
    """
    with connect() as conn:
        try:
            conn.sendall(json.dumps(list(args)).encode() + b"\n")
            if payload is not None:
                conn.sendfile(payload)
            else:
                conn.sendall(data)
            conn.shutdown(socket.SHUT_WR)
        except (BrokenPipeError, ConnectionResetError):
            pass
        answer = b""
        try:
            while chunk := conn.recv(65536):
                answer += chunk
        except ConnectionResetError:
            pass
    *said, last = answer.decode(errors="replace").splitlines() or [""]
    status = re.fullmatch(r"status (\d+)", last)
    if not status:
        print("\n".join([*said, last]).strip() or "bench-git: the gate hung up without an answer", file=sys.stderr)
        return 1
    if said:
        print("\n".join(said), file=sys.stdout if status[1] == "0" else sys.stderr)
    return int(status[1])


# The flag is the command line's, handed over as main() parsed it.
def push(force: bool, connect: Callable[[], socket.socket] = connect) -> int:  # noqa: FBT001
    """Bundle the current branch for the gate to push, and on success fetch what it pushed."""
    branch = current_branch()
    args = ["push", branch] + (["--force"] if force else [])
    with tempfile.TemporaryDirectory() as tmp:
        bundle = os.path.join(tmp, "branch.bundle")
        git("bundle", "create", "-q", bundle, f"refs/heads/{branch}")
        with open(bundle, "rb") as bundle_file:
            status = gate(*args, payload=bundle_file, connect=connect)
    if status == 0:
        # The read token fetches, so the checkout sees what it pushed.
        git("fetch", "-q", "origin", f"+refs/heads/{branch}:refs/remotes/origin/{branch}")
        git("branch", "-q", f"--set-upstream-to=origin/{branch}")
    return status


def text(title: str | None, body_file: str | None) -> bytes:
    """A PR's title and body, whichever are given, as the JSON the gate reads."""
    fields: dict[str, str] = {}
    if title is not None:
        fields["title"] = title
    if body_file is not None:
        with open(body_file) as body_handle:
            fields["body"] = body_handle.read()
    # Unescaped, so a body within the gate's limit stays within what it reads.
    return json.dumps(fields, ensure_ascii=False).encode()


def main(argv: list[str], connect: Callable[[], socket.socket] = connect) -> int:
    """Parse the command line, hand the action to the gate, and return its status."""
    parser = argparse.ArgumentParser(prog="bench-git", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("push").add_argument("--force", action="store_true")
    pr = sub.add_parser("pr")
    pr.add_argument("--title", required=True)
    pr.add_argument("--body-file")
    edit = sub.add_parser("pr-edit")
    edit.add_argument("number")
    edit.add_argument("--title")
    edit.add_argument("--body-file")
    sub.add_parser("check")
    args = parser.parse_args(argv)

    if args.action == "push":
        return push(args.force, connect)
    if args.action == "pr":
        return gate("pr", current_branch(), data=text(args.title, args.body_file), connect=connect)
    if args.action == "pr-edit":
        return gate("pr-edit", args.number, data=text(args.title, args.body_file), connect=connect)
    return gate("check", connect=connect)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
