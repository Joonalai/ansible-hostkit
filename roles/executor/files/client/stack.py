#!/usr/bin/python3 -I
"""Bring up, test and take down a checkout's stack on an executor instance.

    stack up [--profile NAME] [--repo OWNER/NAME] [-C DIR] [--json]
    stack run SUITE
    stack logs [SERVICE]
    stack down
    stack list [--json]

up sends the checkout's HEAD as a git bundle, commits not pushed included,
and the instance builds it and starts it as this caller's stack, replacing
its last; it prints the stack's URLs. Uncommitted changes are not in it.
How the repository comes up is its stack.toml. run runs one of the suites
it names against the stack, logs shows the tail of its logs, down removes
it with its data.

Where the instance is and who this caller is come from the environment,
which the host sets:

    STACK_SOCKET      the instance's socket, /run/stack.sock by default
    STACK_KEY         this caller's key
    STACK_REPO        the repository, owner/name, when the checkout's
                      origin does not say it
    STACK_LOCAL_HOST  the instance's address from where this runs, such as
                      host.containers.internal in a container; the URLs
                      for the user use the instance's own host

Exit status 75 means the instance had no room, or is paused: go on without
the stack, and say so.

Standard library only, on the system python3.
"""

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterator
from typing import IO, Any, cast

DEFAULT_SOCKET = "/run/stack.sock"
CHUNK = 65536
GITHUB = re.compile(r"(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)([A-Za-z0-9-]+/[A-Za-z0-9._-]+?)(?:\.git)?/?")


class StackError(Exception):
    """Something this side cannot do, said to the user."""


def git(directory: str, *args: str) -> str:
    """What a git command in the checkout printed; a failure raises."""
    try:
        return subprocess.run(["git", "-C", directory, *args], capture_output=True, text=True, check=True).stdout.strip()
    except subprocess.CalledProcessError as error:
        command = next((arg for arg in args if not arg.startswith("-") and "=" not in arg), "")
        raise StackError(f"git {command} failed: {error.stderr.strip()}") from error


def repo_of(directory: str, env: dict[str, str]) -> str:
    """The repository the checkout is, owner/name."""
    if env.get("STACK_REPO", ""):
        return env["STACK_REPO"]
    url = git(directory, "remote", "get-url", "origin")
    match = GITHUB.fullmatch(url)
    if match is None:
        raise StackError(f"cannot tell the repository from origin {url}: set STACK_REPO or pass --repo")
    return str(match.group(1))


def make_bundle(directory: str, path: str) -> str:
    """A bundle of the checkout's HEAD at path; the commit it brings up.

    A shallow clone's history cannot be bundled, so there it is HEAD's tree
    alone, as a commit with no parent: the instance checks out the
    bundle's one head, whatever its name.
    """
    head = git(directory, "rev-parse", "HEAD")
    commit = head
    if git(directory, "rev-parse", "--is-shallow-repository") == "true":
        # An identity of its own: a container may have none configured, and
        # the commit goes nowhere but the instance.
        identity = ["-c", "user.name=stack", "-c", "user.email=stack@localhost"]
        commit = git(directory, *identity, "commit-tree", "-m", f"HEAD's tree, {head}", f"{head}^{{tree}}")
        git(directory, "update-ref", "refs/stack/head", commit)
        git(directory, "bundle", "create", "--quiet", path, "refs/stack/head")
        git(directory, "update-ref", "-d", "refs/stack/head")
        return head
    git(directory, "bundle", "create", "--quiet", path, "HEAD")
    return commit


def connect(path: str) -> socket.socket:
    """A connection to the instance's socket."""
    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        conn.connect(path)
    except OSError as error:
        conn.close()
        raise StackError(f"cannot reach the executor at {path}: {error}") from error
    return conn


def exchange(conn: socket.socket, request: dict[str, Any], body: IO[bytes] | None) -> Iterator[dict[str, Any]]:
    """Send the request and its body; yield the instance's answer, line by line."""
    conn.sendall(json.dumps(request).encode() + b"\n")
    if body is not None:
        while chunk := body.read(CHUNK):
            conn.sendall(chunk)
    conn.shutdown(socket.SHUT_WR)
    with conn.makefile("rb") as answer:
        for line in answer:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict):
                yield event


def call(
    request: dict[str, Any],
    env: dict[str, str],
    body: IO[bytes] | None = None,
    connector: Callable[[str], socket.socket] = connect,
    err: IO[str] | None = None,
) -> dict[str, Any]:
    """One request to the instance; its output goes to err, stderr by default, its last event is returned."""
    to = cast("IO[str]", sys.stderr) if err is None else err
    key = env.get("STACK_KEY", "")
    if not key:
        raise StackError("STACK_KEY is not set: this host has no executor for this repository")
    with connector(env.get("STACK_SOCKET") or DEFAULT_SOCKET) as conn:
        for event in exchange(conn, {"key": key, **request}, body):
            if "exit" in event:
                return event
            if "out" in event:
                print(event["out"], file=to, flush=True)
    return {"exit": 1, "message": "the executor ended without an answer"}


def urls(result: dict[str, Any], env: dict[str, str]) -> dict[str, Any]:
    """The stack's URLs, the user's and this side's, from the ports it was given."""
    ports = result.get("ports") or []
    page = int(result.get("page") or 1)
    local = env.get("STACK_LOCAL_HOST") or result.get("host", "")
    found = {
        "page": f"http://{result.get('host')}:{ports[page - 1]}/" if ports else "",
        "ports": {str(port): f"http://{result.get('host')}:{port}/" for port in ports},
        "local": f"http://{local}:{ports[page - 1]}/" if ports else "",
    }
    return found | {"local_ports": {str(port): f"http://{local}:{port}/" for port in ports}, "control": result.get("control", "")}


def up(args: argparse.Namespace, env: dict[str, str], out: IO[str], connector: Callable[[str], socket.socket]) -> int:
    """Send the checkout and bring it up; print where it is."""
    directory = args.C
    repo = args.repo or repo_of(directory, env)
    if git(directory, "status", "--porcelain", "--untracked-files=no"):
        print("Uncommitted changes are not in the stack: commit them first to have them.", file=sys.stderr)
    with tempfile.TemporaryDirectory(prefix="stack-") as scratch:
        path = os.path.join(scratch, "head.bundle")
        commit = make_bundle(directory, path)
        size = os.path.getsize(path)
        request = {"action": "up", "repo": repo, "profile": args.profile, "bundle_size": size}
        with open(path, "rb") as bundle:
            result = call(request, env, bundle, connector)
    if result["exit"] != 0:
        print(result.get("message") or "up failed", file=sys.stderr)
        return int(result["exit"])
    found = urls(result, env)
    if args.json:
        print(json.dumps({"commit": commit, "repo": repo, **found}), file=out)
        return 0
    print(f"{repo} at {commit[:12]} is up.", file=out)
    print(f"For the user: {found['page']}", file=out)
    print(f"From here:    {found['local']}", file=out)
    if len(found["ports"]) > 1:
        print("Its ports, from here: " + " ".join(found["local_ports"].values()), file=out)
    if found["control"]:
        print(f"Stop and start it from the phone at {found['control']}", file=out)
    return 0


def show_list(args: argparse.Namespace, env: dict[str, str], out: IO[str], connector: Callable[[str], socket.socket]) -> int:
    """Print the instance's stacks."""
    result = call({"action": "list"}, env, None, connector)
    if result["exit"] != 0:
        print(result.get("message") or "list failed", file=sys.stderr)
        return int(result["exit"])
    if args.json:
        print(json.dumps(result.get("stacks", [])), file=out)
        return 0
    for stack in result.get("stacks", []):
        mine = "  (yours)" if stack.get("caller") == result.get("caller") else ""
        print(
            f"{stack['caller']:<24} {stack['state']:<9} {stack.get('repo', ''):<32} "
            f"{stack.get('memory_mb', 0):>6} MB  {stack.get('url', '')}{mine}",
            file=out,
        )
    return 0


def parser() -> argparse.ArgumentParser:
    """The command line."""
    top = argparse.ArgumentParser(prog="stack", description="A checkout's stack on an executor instance.")
    actions = top.add_subparsers(dest="action", required=True)
    up_parser = actions.add_parser("up", help="send HEAD and bring it up as this caller's stack")
    up_parser.add_argument("--profile", default="default")
    up_parser.add_argument("--repo", help="owner/name, if origin does not say")
    up_parser.add_argument("-C", default=".", metavar="DIR", help="the checkout, . by default")
    up_parser.add_argument("--json", action="store_true")
    run_parser = actions.add_parser("run", help="run one of the repository's suites against the stack")
    run_parser.add_argument("suite")
    logs_parser = actions.add_parser("logs", help="the tail of the stack's logs")
    logs_parser.add_argument("service", nargs="?")
    actions.add_parser("down", help="remove the stack and its data")
    list_parser = actions.add_parser("list", help="the instance's stacks")
    list_parser.add_argument("--json", action="store_true")
    return top


def main(
    argv: list[str],
    env: dict[str, str] | None = None,
    out: IO[str] = sys.stdout,
    connector: Callable[[str], socket.socket] = connect,
) -> int:
    """Run the command; returns its exit status."""
    args = parser().parse_args(argv)
    env = dict(os.environ) if env is None else env
    try:
        if args.action == "up":
            return up(args, env, out, connector)
        if args.action == "list":
            return show_list(args, env, out, connector)
        request: dict[str, Any] = {"action": args.action}
        if args.action == "run":
            request["suite"] = args.suite
        elif args.action == "logs" and args.service:
            request["service"] = args.service
        result = call(request, env, None, connector)
    except StackError as error:
        print(f"stack: {error}", file=sys.stderr)
        return 1
    if result.get("message"):
        print(result["message"], file=sys.stderr if result["exit"] else out)
    return int(result["exit"])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
