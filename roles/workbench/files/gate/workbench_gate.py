#!/usr/bin/python3 -I
"""Push a workbench branch, or open or edit its PR, as the user.

Runs as the gate account, the only one that can read the write token. Each
repository has a socket of its own, mounted into that repository's
container alone, and systemd starts this for every connection to it:

    workbench-gate serve app

The repository is the socket's, from the unit, never the client's. The
client, bench-git, sends one line, a JSON list of the action and its
arguments, then the action's input; it gets back what the gate says, and a
last line with its exit status:

    ["push", "feat/3-runs"] or ["push", "feat/3-runs", "--force"]   then a bundle
    ["pr", "feat/3-runs"] or ["pr-edit", "12"]   then {"title": ..., "body": ...}
    ["check"]

Root, and the play, run the same actions directly:

    workbench-gate push app feat/3-runs [--force] < bundle
    workbench-gate check app

The token pushes as the user, and GitHub Free gives a private repository no
branch protection, so this script is all that stands between a session and
the default branch. Hence what it will do and nothing else:

- push one branch whose name matches the configured pattern, never the
  default branch, never a tag, and replace it only when GitHub still has
  the very commit the gate itself last pushed there;
- push only commits whose author and committer are the configured identity
  and whose messages and paths match nothing in the forbidden lists;
- use a repository's workflow token, if it has one, only for a push whose new
  commits touch .github/workflows/. That one can change what CI runs, and
  those commits pass every check above, the default branch's included;
- open a PR from such a branch, a draft on a public repository, and edit
  the title or body of an open PR whose head is such a branch, with the
  same check on the text. Marking a draft ready is the user's alone.

It never runs git in the workbench's checkout, whose config and hooks are
the workbench's to write. What crosses is a git bundle on stdin, fetched into
the gate's own mirror, and the arguments, of which the repository is a name
from the config file, never a URL.

Standard library only, on the system python3.
"""

import base64
import contextlib
import fcntl
import json
import os
import re
import socket
import struct
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import IO, Any, cast

CONFIG = "/etc/workbench/gate.json"
MAX_BUNDLE = 200 * 1024 * 1024
MAX_TITLE = 256
MAX_BODY = 65536
MAX_REQUEST = 4096
WORKFLOWS = re.compile(r"^\.github/workflows/")
# How many arguments a push has with --force after its name and branch, and
# how many the script has when systemd starts it as "serve <name>".
ARGS_WITH_FORCE = 4
ARGV_TO_SERVE = 3

# The config file as parsed, and urllib.request.urlopen or what stands in for it.
Config = dict[str, Any]
Opener = Callable[..., Any]


# Named for what the gate answers, "refused: ...", and what the tests expect.
class Refused(Exception):  # noqa: N818
    """What the gate will not do, said in one line."""


def load_config(path: str) -> Config:
    """The gate's config, as the play wrote it."""
    with open(path) as file:
        return json.load(file)


def read_token(config: Config) -> str:
    """The write token, from the file the gate account alone reads."""
    with open(config["token"]) as token_file:
        return token_file.read().strip()


def read_workflow_token(config: Config, name: str) -> str | None:
    """A repository's workflow token, or None if it has none."""
    path = config["repos"][name].get("workflow_token")
    if path is None:
        return None
    with open(path) as file:
        return file.read().strip()


def api(
    config: Config, method: str, path: str, body: dict[str, Any] | None = None, opener: Opener = urllib.request.urlopen,
) -> dict[str, Any]:
    """One call to GitHub's API with the write token, and its answer as parsed JSON."""
    # The address is the config's, which root owns, never the client's.
    req = urllib.request.Request(  # noqa: S310
        config["api"] + path,
        method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {read_token(config)}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "workbench-gate",
        },
    )
    with opener(req, timeout=30) as response:
        return json.load(response)


def default_branch(config: Config, repo: str, opener: Opener = urllib.request.urlopen) -> str:
    """The repository's default branch, as GitHub has it now."""
    return cast("str", api(config, "GET", f"/repos/{repo}", opener=opener)["default_branch"])


def check_branch_name(config: Config, branch: str) -> None:
    """Before anything else, and without the token or the network."""
    if not re.fullmatch(config["branch_pattern"], branch) or ".." in branch or branch.endswith(".lock"):
        raise Refused(f"{branch!r} is not a branch the workbench pushes; names match {config['branch_pattern']}")


def check_not_default(branch: str, default: str) -> None:
    """The default branch is never the workbench's to push or to open a PR from."""
    if branch == default:
        raise Refused(f"{branch!r} is the default branch")


def check_text(config: Config, what: str, text: str) -> None:
    """Refuse text that matches a forbidden pattern, in whatever case and on any line."""
    for pattern in config["forbidden"]:
        if re.search(pattern, text, re.IGNORECASE | re.MULTILINE):
            raise Refused(f"{what} matches the forbidden pattern {pattern!r}")


PERSON = re.compile(r"^(author|committer) (.*) <(.*)> \d+ [+-]\d{4}$")


def parse_commit(raw: str) -> tuple[dict[str, tuple[str, str]], str]:
    """The author and committer of a raw commit object, and its message.

    The first of each, as git and GitHub read them: a commit may carry
    further headers of any name, a second "author" among them, and the one
    that counted last would let a commit by anyone show the user's name
    here. Split on the newline alone, which is all that ends a header.
    """
    headers, _, message = raw.partition("\n\n")
    people: dict[str, tuple[str, str]] = {}
    for line in headers.split("\n"):
        match = PERSON.match(line)
        if match and match[1] not in people:
            people[match[1]] = (match[2], match[3])
    return people, message


def git_env(config: Config, token: str) -> dict[str, str]:
    """No system or global config, no prompt, and the token in the
    environment, which no other account can read, rather than in an argument,
    which every account can.
    """
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return {
        "PATH": "/usr/bin:/bin",
        "HOME": config["state_dir"],
        "LANG": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": f"http.{config['git_base']}/.extraHeader",
        "GIT_CONFIG_VALUE_0": f"Authorization: Basic {basic}",
    }


class Mirror:
    """A bare repository the gate alone writes, one per configured name."""

    def __init__(self, config: Config, name: str, token: str) -> None:
        """Where the mirror is, what it mirrors, and the environment its git runs in."""
        self.dir = os.path.join(config["state_dir"], f"{name}.git")
        self.url = f"{config['git_base']}/{config['repos'][name]['repo']}.git"
        self.env = git_env(config, token)

    def git(self, *args: str, stdin: IO[bytes] | int | None = None) -> str:
        """Run git in the mirror with hooks off and fsck on, and refuse if it fails."""
        argv = ["git", "-c", "core.hooksPath=/dev/null", "-c", "transfer.fsckObjects=true", "-C", self.dir, *args]
        # A commit message need not be UTF-8; replaced, it is still checked.
        result = subprocess.run(argv, env=self.env, stdin=stdin, check=False, capture_output=True, text=True, errors="replace")
        if result.returncode != 0:
            raise Refused(f"git {args[0]} failed: {result.stderr.strip()}")
        return result.stdout

    def sync(self) -> None:
        """Create the mirror if it is not there, and fetch GitHub's branches into it."""
        if not os.path.isdir(self.dir):
            subprocess.run(["git", "init", "-q", "--bare", self.dir], env=self.env, check=True, capture_output=True)
        self.git("fetch", "-q", "--prune", "--no-tags", self.url, "+refs/heads/*:refs/remotes/origin/*")

    def rev(self, ref: str) -> str | None:
        """The commit a ref names in the mirror, or None if it names none."""
        result = subprocess.run(
            ["git", "-C", self.dir, "rev-parse", "-q", "--verify", f"{ref}^{{commit}}"],
            env=self.env,
            check=False,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip() or None


def take_bundle(stdin: IO[bytes], directory: str) -> str:
    """Stdin to a file the gate owns, refusing anything past MAX_BUNDLE."""
    fd, path = tempfile.mkstemp(dir=directory, suffix=".bundle")
    size = 0
    with os.fdopen(fd, "wb") as bundle_file:
        while chunk := stdin.read(1 << 20):
            size += len(chunk)
            if size > MAX_BUNDLE:
                bundle_file.close()
                os.unlink(path)
                raise Refused(f"the bundle is over {MAX_BUNDLE // (1024 * 1024)} MB")
            bundle_file.write(chunk)
    return path


def check_commits(config: Config, mirror: Mirror, sha: str) -> tuple[int, bool]:
    """Every commit not already on GitHub: who made it, what it says, and
    what it touches. Returns how many there are, and whether any of them
    touches a workflow.
    """
    workflows = False
    new = mirror.git("rev-list", sha, "--not", "--glob=refs/remotes/origin/*").split()
    identity = (config["identity"]["name"], config["identity"]["email"])
    for commit in new:
        people, message = parse_commit(mirror.git("cat-file", "commit", commit))
        for role in ("author", "committer"):
            if people.get(role) != identity:
                name, email = people.get(role, ("?", "?"))
                raise Refused(f"commit {commit[:12]} has {role} {name} <{email}>, not {identity[0]} <{identity[1]}>")
        check_text(config, f"commit {commit[:12]}'s message", message)
        # -m, or a merge commit lists nothing, and a merge can add a file
        # neither parent had. Against each parent, so also what the merged
        # side changed, which is stricter than needed and never looser.
        # -z, or git prints a path with a quote, a control character or
        # anything past ASCII quoted and escaped, and no pattern anchored at
        # its start matches it.
        paths = mirror.git("diff-tree", "--no-commit-id", "--name-only", "-z", "-r", "-m", "--root", commit).split("\0")
        for path in filter(None, paths):
            for pattern in config["forbidden_paths"]:
                if re.search(pattern, path):
                    raise Refused(f"commit {commit[:12]} touches {path}, which matches {pattern!r}")
            workflows = workflows or bool(WORKFLOWS.match(path))
    return len(new), workflows


# The arguments are the action's, in the order main() and the tests hand them over.
def push(  # noqa: PLR0913, PLR0917
    config: Config, name: str, branch: str, force: bool, stdin: IO[bytes], opener: Opener = urllib.request.urlopen,  # noqa: FBT001
) -> str:
    """Fast-forward the branch, or with force, replace it, but only when
    GitHub still has what the gate itself last pushed there. Never on the
    session's word: a session talked into it could otherwise name any
    commit it fetched, and rewrite a branch the user pushed from elsewhere.
    Commits added on top of someone's branch lose nothing, so a fast-forward
    needs no record.
    """
    repo = config["repos"][name]["repo"]
    check_branch_name(config, branch)
    check_not_default(branch, default_branch(config, repo, opener))
    mirror = Mirror(config, name, read_token(config))
    mirror.sync()
    bundle = take_bundle(stdin, config["state_dir"])
    incoming = f"refs/incoming/{branch}"
    try:
        mirror.git("fetch", "-q", "--no-tags", bundle, f"+refs/heads/{branch}:{incoming}")
    finally:
        os.unlink(bundle)
    try:
        sha = mirror.rev(incoming)
        if sha is None:
            raise Refused(f"the bundle carries no refs/heads/{branch}")
        count, workflows = check_commits(config, mirror, sha)
        remote = mirror.rev(f"refs/remotes/origin/{branch}")
        if remote == sha:
            return f"{branch} is already at {sha[:12]} on {repo}"
        fast_forward = remote is None or subprocess.run(
            ["git", "-C", mirror.dir, "merge-base", "--is-ancestor", remote, sha], env=mirror.env, check=False,
        ).returncode == 0
        if not fast_forward:
            if not force:
                raise Refused(f"{branch} on {repo} is not an ancestor of {sha[:12]}; push --force to replace it")
            if mirror.rev(f"refs/pushed/{branch}") != remote:
                raise Refused(
                    f"{branch} on {repo} is at {remote[:12]}, which the workbench did not push there; "
                    "fetch, and build on it rather than replacing it",
                )
        # Without a workflow token GitHub refuses such a push, which is the
        # answer for every other repository, so the gate need not.
        if workflows:
            token = read_workflow_token(config, name)
            # An empty file is no token either.
            if token is not None and token != "":
                mirror.env = git_env(config, token)
        # The lease is the commit just checked, so nothing that lands in
        # between is overwritten either.
        lease = f"--force-with-lease=refs/heads/{branch}:{remote or ''}"
        mirror.git("push", "-q", "--no-verify", lease, mirror.url, f"{sha}:refs/heads/{branch}")
        mirror.git("update-ref", f"refs/remotes/origin/{branch}", sha)
        # The gate's own record, which only it writes: what it may replace.
        mirror.git("update-ref", f"refs/pushed/{branch}", sha)
    finally:
        subprocess.run(["git", "-C", mirror.dir, "update-ref", "-d", incoming], env=mirror.env, check=False, capture_output=True)
    return f"pushed {branch} to {repo} at {sha[:12]}, {count} new commit{'s' if count != 1 else ''}"


def read_text(stdin: IO[str] | IO[bytes]) -> dict[str, str]:
    """A PR's title and body from stdin, as JSON and within the limits."""
    # Room for JSON's escapes at their widest, \uXXXX for every character,
    # so a body within the limits is never cut off and called "not JSON".
    data = stdin.read((MAX_TITLE + MAX_BODY) * 6 + 64)
    try:
        text = json.loads(data)
    except ValueError as error:
        raise Refused(f"stdin is not JSON: {error}") from None
    if not isinstance(text, dict) or not set(text) <= {"title", "body"} or not all(isinstance(value, str) for value in text.values()):
        raise Refused('stdin wants {"title": "...", "body": "..."}, both optional strings')
    if len(text.get("title", "")) > MAX_TITLE or len(text.get("body", "")) > MAX_BODY:
        raise Refused(f"the title is limited to {MAX_TITLE} characters and the body to {MAX_BODY}")
    return cast("dict[str, str]", text)


def check_pr_text(config: Config, text: dict[str, str]) -> None:
    """A PR's title and body, against the same forbidden patterns as a commit's message."""
    for key, value in text.items():
        check_text(config, f"the PR's {key}", value)


def open_pr(config: Config, name: str, branch: str, stdin: IO[str] | IO[bytes], opener: Opener = urllib.request.urlopen) -> str:
    """Open a PR from the branch onto the default branch, and return its address."""
    repo = config["repos"][name]["repo"]
    check_branch_name(config, branch)
    text = read_text(stdin)
    about = api(config, "GET", f"/repos/{repo}", opener=opener)
    default = about["default_branch"]
    check_not_default(branch, default)
    if not text.get("title", ""):
        raise Refused("a PR needs a title")
    check_pr_text(config, text)
    pr = api(
        config,
        "POST",
        f"/repos/{repo}/pulls",
        # A public repository's PR opens as a draft, so nobody starts
        # reviewing it before the user has; marking it ready is theirs.
        # Unless GitHub says private, it is public.
        {"title": text["title"], "body": text.get("body", ""), "head": branch, "base": default, "draft": about.get("private") is not True},
        opener,
    )
    return cast("str", pr["html_url"])


def edit_pr(config: Config, name: str, number: str, stdin: IO[str] | IO[bytes], opener: Opener = urllib.request.urlopen) -> str:
    """Change the title or body of an open PR whose head is a workbench branch of the repository."""
    repo = config["repos"][name]["repo"]
    if not (number.isascii() and number.isdecimal()):
        raise Refused(f"{number!r} is not a PR number")
    pr = api(config, "GET", f"/repos/{repo}/pulls/{number}", opener=opener)
    if pr["state"] != "open" or pr["head"]["repo"] is None or pr["head"]["repo"]["full_name"] != repo:
        raise Refused(f"#{number} is not an open PR from a branch of {repo}")
    check_branch_name(config, pr["head"]["ref"])
    check_not_default(pr["head"]["ref"], pr["base"]["repo"]["default_branch"])
    text = read_text(stdin)
    if not text:
        raise Refused("nothing to change: give a title, a body or both")
    check_pr_text(config, text)
    return cast("str", api(config, "PATCH", f"/repos/{repo}/pulls/{number}", text, opener)["html_url"])


def check(config: Config, name: str, opener: Opener = urllib.request.urlopen) -> str:
    """What GitHub says the repository is, and whether the token may push to it."""
    repo = config["repos"][name]["repo"]
    answer = api(config, "GET", f"/repos/{repo}", opener=opener)
    return json.dumps(
        {"repo": answer["full_name"], "default_branch": answer["default_branch"], "push": answer.get("permissions", {}).get("push")},
    )


USAGE = "usage: workbench-gate push <name> <branch> [--force] | pr <name> <branch> | pr-edit <name> <number> | check <name> | serve <name>"


# One branch per action and one line per failure, which reads better whole.
def main(  # noqa: C901, PLR0911
    argv: list[str], config_path: str = CONFIG, stdin: IO[Any] | None = None, opener: Opener = urllib.request.urlopen,
) -> int:
    """Run one action on a configured repository, under its lock, and return the exit status.

    Stdin is bytes for a push, and text or bytes for a PR.
    """
    shapes = {"push": (3, 4), "pr": (3,), "pr-edit": (3,), "check": (2,)}
    if (
        not argv
        or argv[0] not in shapes
        or len(argv) not in shapes[argv[0]]
        or (len(argv) == ARGS_WITH_FORCE and argv[3] != "--force")
    ):
        print(USAGE, file=sys.stderr)
        return 2
    config = load_config(config_path)
    action, name, *rest = argv
    if name not in config["repos"]:
        print(f"{name!r} is not a workbench repository; known: {', '.join(sorted(config['repos']))}", file=sys.stderr)
        return 2
    os.umask(0o077)
    # One action per repository at a time: the mirror is shared.
    with open(os.path.join(config["state_dir"], f"{name}.lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            if action == "push":
                print(push(config, name, rest[0], rest[1:] == ["--force"], stdin or sys.stdin.buffer, opener))
            elif action == "pr":
                print(open_pr(config, name, rest[0], stdin or sys.stdin, opener))
            elif action == "pr-edit":
                print(edit_pr(config, name, rest[0], stdin or sys.stdin, opener))
            else:
                print(check(config, name, opener))
        except Refused as error:
            print(f"refused: {error}", file=sys.stderr)
            return 1
        except FileNotFoundError as error:
            print(f"the gate is not set up: {error.filename} is missing", file=sys.stderr)
            return 1
        except subprocess.CalledProcessError as error:
            print(f"{error.cmd[0]} failed: {(error.stderr or b'').decode(errors='replace').strip()}", file=sys.stderr)
            return 1
        except urllib.error.HTTPError as error:
            print(f"GitHub answered {error.code} for {error.url}: {error.read().decode(errors='replace')}", file=sys.stderr)
            return 1
        # After HTTPError, which is one: this is GitHub not answering at all.
        except urllib.error.URLError as error:
            print(f"GitHub could not be reached: {error.reason}", file=sys.stderr)
            return 1
    return 0


def peer_uid(fd: int = 0) -> int:
    """The uid of the process at the other end of the socket systemd handed over."""
    with socket.socket(fileno=os.dup(fd)) as conn:
        creds = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    return struct.unpack("3i", creds)[1]


def read_request(conn: IO[bytes]) -> list[str]:
    """The request's one line: a JSON list of the action and its arguments."""
    line = conn.readline(MAX_REQUEST)
    try:
        request = json.loads(line)
    except ValueError:
        request = None
    if not isinstance(request, list) or not request or not all(isinstance(argument, str) for argument in request):
        raise Refused(
            'the request wants one line, a JSON list: ["push", "<branch>"], ["pr", "<branch>"], ["pr-edit", "<number>"] or ["check"]',
        )
    return cast("list[str]", request)


def check_client(config: Config, uid: int) -> None:
    """Refuse any peer but the workbench account itself."""
    if uid != config["client_uid"]:
        raise Refused(f"uid {uid} is not the workbench's")


# The arguments are what the tests stand in for: the config, the socket's two ends, the peer and GitHub.
def serve(  # noqa: PLR0913, PLR0917
    name: str,
    config_path: str = CONFIG,
    conn: IO[bytes] | None = None,
    out: IO[str] | None = None,
    uid: int | None = None,
    opener: Opener = urllib.request.urlopen,
) -> int:
    """One connection on a repository's socket, as stdin and stdout.

    Only the workbench account itself is served, as its uid on this host:
    the socket's mode lets its group in, and a container's root, or any
    user in it but the session's, is another uid.
    """
    conn = cast("IO[bytes]", conn or sys.stdin.buffer)
    out = cast("IO[str]", out or sys.stdout)
    config = load_config(config_path)
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        uid = peer_uid() if uid is None else uid
        try:
            check_client(config, uid)
            action, *rest = read_request(conn)
        except Refused as error:
            print(f"refused: {error}")
            status = 1
        else:
            status = main([action, name, *rest], config_path, conn, opener)
    out.write(f"status {status}\n")
    out.flush()
    # A refusal can come before the input is read, and a socket closed with
    # input unread is reset, which can lose the answer on its way out. So
    # the rest is read and dropped, no more of it than a bundle may be.
    drained = 0
    while drained <= MAX_BUNDLE and (chunk := conn.read(1 << 20)):
        drained += len(chunk)
    return 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["serve"] and len(sys.argv) == ARGV_TO_SERVE:
        sys.exit(serve(sys.argv[2]))
    sys.exit(main(sys.argv[1:]))
