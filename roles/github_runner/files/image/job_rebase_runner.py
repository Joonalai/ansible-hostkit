#!/usr/bin/python3 -I
"""Rebase one of the bot's PRs on the rebase runner, and hand on what is
more than a rebase, for the workflow's job there.

    claude-job-rebase-runner try       before Claude
    claude-job-rebase-runner settle    after Claude, however it ended

After a merge, claude-job-rebase asks for a rebase of each of the bot's
ready PRs that conflicts. Those used to queue behind two-hour jobs on the
work runners. The rebase runner is a slot of its own that takes only them.

try checks out the PR's head and runs `git rebase origin/<base>`. A rebase
with no conflicts is pushed at once, with a lease on the head it started
from, and Claude never starts: the output claude=false. One that stops at a
conflict is left there, midway, for Claude, claude=true, and what settle
needs is the step's output state: the PR, its branch, the head and base it
started from, and the files a merge of the two conflicts in, from `git
merge-tree`. A step output, because Claude can write any file of the job's
and cannot change an earlier step's outputs.

settle decides what Claude's resolution may become, trusting nothing Claude
could have touched:

- It first stops every process of the job's account that is not its own
  ancestor, so nothing Claude left running reads its token from /proc,
  and makes itself undumpable.
- The PR must still be the one try saw: open, from this repository, the
  same branch, base and head.
- Claude's HEAD and the two commits are fetched from the checkout into a
  bare repository of settle's own, by commit id, and checked and pushed
  from there with no global, system or repository config, no hooks and
  no attributes, so no config, hook, diff driver or attribute Claude left
  in the checkout runs or hides a line.
- It is pushed only when the base is in it, no conflict marker is left,
  no binary file moved, and the PR's own change moved by no more than
  MAX_MOVED lines, all of them in the files that conflicted: a
  resolution that rewrote more is no longer a rebase, and nobody would
  review it here.

Anything else is handed on: nothing is pushed, the PR goes back to a draft,
and a marked `@<bot> review: rebase` asks for a job on the work
runners, where the engineer rebases and the reviewer compares the change
with what it was.

How far the change moved is counted per file, as the lines the PR adds and
removes, before the rebase against its old base and after it against the
new one: a line in one and not the other counts once. Context and line
numbers do not count, so a clean rebase moves nothing. Nor does what moves
by itself when anything near it does: a gettext catalogue's `#:` source
references, and the files the base's .gitattributes marks
linguist-generated or that are lock files, which are rebuilt rather than
resolved. Those still have to be among the files that conflicted, as
every moved file does; only their lines are not counted toward MAX_MOVED.

Claude's step gets a read-only token, and the checkout keeps no
credentials, so only these two push. They read CLAUDE_RUNNER_KIND, which
only the rebase runner's unit sets to rebase, and refuse to run anywhere
else; NUMBER, RUNNER_TEMP, GITHUB_REPOSITORY, GITHUB_OUTPUT, BOT_NAME and
BOT_EMAIL, and settle STATE, try's output; and the App's token in
GH_TOKEN, for gh and for the push, which carries it in git's environment,
never in an argument.

Standard library only, on the image's python3.
"""

import base64
import collections
import contextlib
import fnmatch
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping
from typing import Any

# The bot's name, the consumer's: what a comment mentions to start a job,
# and what the labels and the workflow's own marks begin with. The runner's
# unit sets it. Letters, digits and dashes only, since it goes into patterns.
BOT = os.environ.get("CLAUDE_BOT") or "claude"
if not re.fullmatch(r"[A-Za-z0-9-]+", BOT):
    raise SystemExit(f"CLAUDE_BOT is not a name: {BOT!r}")

MAX_MOVED = 80
# How often the processes Claude left are killed and looked for again.
PASSES, PASS_WAIT = 50, 0.1
# Rebuilt, not resolved: their lines do not count toward MAX_MOVED.
LOCKS = ("*.lock", "package-lock.json", "pnpm-lock.yaml", "go.sum")
ESCALATE = f"<!-- {BOT}:auto-escalate"
BRANCH = re.compile(r"[A-Za-z0-9._/-]+")
REPO = re.compile(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+")
SHA = re.compile(r"[0-9a-f]{40}")

# gh and the like, giving what they print, and git, giving how it ended.
type Run = Callable[..., str]
type Git = Callable[..., subprocess.CompletedProcess[str]]


def sh(*argv: str) -> str:
    """What a command prints, raising when it fails."""
    return subprocess.run(argv, check=True, capture_output=True, text=True).stdout.strip()


def git_cmd(*argv: str, env: Mapping[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """Git in the checkout, however it ends: the caller reads its return code."""
    return subprocess.run(["git", *argv], check=False, capture_output=True, text=True, env=env)


def output(env: Mapping[str, str], **values: str) -> None:
    """Add the values to the step's outputs."""
    with open(env["GITHUB_OUTPUT"], "a") as output_file:
        output_file.writelines(f"{key}={value}\n" for key, value in values.items())


def ok(git: Git, *argv: str) -> str:
    """What a git command prints, or a RuntimeError saying how it failed."""
    done = git(*argv)
    if done.returncode:
        raise RuntimeError(f"git {' '.join(argv)}: {(done.stderr or done.stdout).strip()[:300]}")
    return done.stdout.strip()


# One argument more than the rules like, each of them a different thing.
def push(env: Mapping[str, str], git: Git, remote: str, branch: str, old_head: str, head: str = "HEAD") -> None:  # noqa: PLR0913, PLR0917
    """Push head over the branch only while it is still old_head."""
    basic = base64.b64encode(f"x-access-token:{env['GH_TOKEN']}".encode()).decode()
    auth = {
        **os.environ,
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
        "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {basic}",
    }
    done = git(
        "push", f"--force-with-lease=refs/heads/{branch}:{old_head}", remote, f"{head}:refs/heads/{branch}", env=auth,
    )
    if done.returncode:
        raise RuntimeError(f"the push was refused: {(done.stderr or done.stdout).strip()[:300]}")


# Three passes over one diff, which read better together than apart.
def changes(git: Git, a: str, b: str) -> dict[str, collections.Counter[str]]:  # noqa: C901
    """The lines a PR adds and removes between a and b, per file. A binary
    file counts as its blob at b, so it moves when its content does.
    """
    found: dict[str, collections.Counter[str]] = {}
    numstat = ok(git, "diff", "--no-renames", "--no-ext-diff", "--no-textconv", "--numstat", "-z", a, b)
    for entry in numstat.split("\0"):
        added, _, rest = entry.partition("\t")
        removed, _, path = rest.partition("\t")
        if path and added == removed == "-":
            blob = git("rev-parse", "--verify", "-q", f"{b}:{path}").stdout.strip() or "deleted"
            found.setdefault(path, collections.Counter())[f"binary {blob}"] += 1
    # A change of mode alone, or an empty file added or removed, has no
    # line in the patch below: its modes stand for it.
    raw = ok(git, "diff", "--no-renames", "--no-ext-diff", "--no-textconv", "--raw", "--no-abbrev", "-z", a, b)
    fields = raw.split("\0")
    # Not strict: the output ends in a NUL, so the last field is an empty one with no path.
    for meta, path in zip(fields[::2], fields[1::2], strict=False):
        modes = meta.lstrip(":").split()
        if len(modes) > 1 and modes[0] != modes[1]:
            found.setdefault(path, collections.Counter())[f"mode {modes[0]} {modes[1]}"] += 1
    old = new = None
    path = ""
    for line in ok(git, "diff", "--no-color", "--no-renames", "--no-ext-diff", "--no-textconv", "--text",
                   "-U0", a, b).splitlines():
        if line.startswith("diff --git "):
            old = new = None
        elif line.startswith("--- ") and old is None:
            old = line[4:].rstrip("\t")
        elif line.startswith("+++ ") and new is None:
            new = line[4:].rstrip("\t")
            # Git writes a file's --- line before its +++ line, so old is set here.
            path = (new if new != "/dev/null" else old).split("/", 1)[-1]  # ty: ignore[missing-union-attribute]
            found.setdefault(path, collections.Counter())
        elif line.startswith(("+", "-")) and new is not None:
            if path.endswith((".po", ".pot")) and line[1:].startswith("#:"):
                continue
            found[path][line] += 1
    return found


def moved(before: dict[str, collections.Counter[str]], after: dict[str, collections.Counter[str]]) -> dict[str, int]:
    """Per file, how many of the PR's lines differ between the two."""
    counts: dict[str, int] = {}
    for path in before.keys() | after.keys():
        before_lines, after_lines = before.get(path, collections.Counter()), after.get(path, collections.Counter())
        differing = sum(((before_lines - after_lines) + (after_lines - before_lines)).values())
        if differing:
            counts[path] = differing
    return counts


def rebasing(checkout: str) -> bool:
    """Whether a rebase is under way in the checkout, read from its files
    rather than through git, which would run the checkout's config.
    """
    return any(os.path.isdir(os.path.join(checkout, ".git", kind)) for kind in ("rebase-merge", "rebase-apply"))


def generated(attributes: str) -> list[str]:
    """The patterns a .gitattributes marks linguist-generated."""
    found = []
    for line in attributes.splitlines():
        parts = line.split()
        if parts and not parts[0].startswith("#") and (
            "linguist-generated" in parts[1:] or "linguist-generated=true" in parts[1:]
        ):
            found.append(parts[0])
    return found


def rebuilt(path: str, patterns: list[str]) -> bool:
    """Whether path is a lock file or matches one of patterns, as
    .gitattributes reads them: a pattern with no slash names a file
    anywhere, one with a slash a path from the top.
    """
    name = path.rsplit("/", 1)[-1]
    if any(fnmatch.fnmatch(name, lock) for lock in LOCKS):
        return True
    for pattern in patterns:
        if "/" not in pattern.rstrip("/"):
            if fnmatch.fnmatch(name, pattern):
                return True
        elif fnmatch.fnmatch(path, pattern.lstrip("/").replace("**/", "*")):
            return True
    return False


def check(git: Git, state: dict[str, Any], head: str) -> str | None:
    """Why the resolution at head is more than a rebase, or None."""
    if git("merge-base", "--is-ancestor", state["new_base"], head).returncode:
        return f"the branch does not contain `{state['base']}` as it was"
    markers = git("diff", "--no-ext-diff", "--no-textconv", "--text", "--check", state["new_base"], head)
    if "conflict marker" in markers.stdout:
        return "conflict markers are left in it"
    old_base = ok(git, "merge-base", state["old_head"], state["new_base"])
    counts = moved(changes(git, old_base, state["old_head"]), changes(git, state["new_base"], head))
    patterns = generated(git("show", f"{state['new_base']}:.gitattributes").stdout)
    counted = sum(moved_lines for path, moved_lines in counts.items() if not rebuilt(path, patterns))
    outside = sorted(path for path in counts if path not in state["conflicted"])
    if outside:
        return "its change moved outside the files that conflicted: " + ", ".join(f"`{path}`" for path in outside[:5])
    if counted > MAX_MOVED:
        return f"its change moved by {counted} lines, more than the {MAX_MOVED} a rebase here may"
    return None


def clean_git(directory: str) -> Git:
    """Git in a bare repository of settle's own, reading no config, hooks
    or attributes but its own.
    """
    base_env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": directory,
        "LANG": "C",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_ATTR_NOSYSTEM": "1",
    }

    def run_git(*argv: str, env: Mapping[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        extra = {name: value for name, value in (env or {}).items() if name.startswith("GIT_CONFIG_")}
        return subprocess.run(
            ["git", "-C", directory, "-c", "core.hooksPath=/dev/null", "-c", "core.attributesFile=/dev/null",
             "-c", "core.quotePath=false", *argv],
            check=False, capture_output=True, text=True, env=base_env | extra,
        )
    return run_git


def strays(processes: dict[int, tuple[int, int]], me: int, uid: int) -> list[int]:
    """The processes of uid that are neither me nor an ancestor of mine:
    whatever Claude's step left running. processes maps pid to (ppid, uid).
    """
    keep, pid = set(), me
    while pid in processes and pid not in keep:
        keep.add(pid)
        pid = processes[pid][0]
    return sorted(pid_number for pid_number, (_, owner) in processes.items() if owner == uid and pid_number not in keep)


def read_processes() -> dict[int, tuple[int, int]]:
    """Every process but the dead ones waiting for their parent, which
    hold nothing any more.
    """
    found = {}
    for name in os.listdir("/proc"):
        if not name.isdecimal():
            continue
        try:
            with open(f"/proc/{name}/status") as status_file:
                fields = dict(line.split(":", 1) for line in status_file if ":" in line)
            if fields["State"].strip().startswith("Z"):
                continue
            found[int(name)] = (int(fields["PPid"]), int(fields["Uid"].split()[0]))
        except (OSError, KeyError, ValueError):
            continue
    return found


def stop_strays(
    kill: Callable[[int, int], object] = os.kill,
    processes: Callable[[], dict[int, tuple[int, int]]] = read_processes,
    sleep: Callable[[float], object] = time.sleep,
) -> bool:
    """Kill what Claude left running, again until nothing is left: one that
    forks while /proc is read is missed by a single pass. False when some
    still run after PASSES.
    """
    for _ in range(PASSES):
        found = strays(processes(), os.getpid(), os.getuid())
        if not found:
            return True
        for pid in found:
            with contextlib.suppress(OSError):
                kill(pid, signal.SIGKILL)
        sleep(PASS_WAIT)
    return not strays(processes(), os.getpid(), os.getuid())


def guard() -> bool:
    """Keep this process's memory and environment, the token's among them,
    out of /proc, then stop what Claude left running. False when something
    of Claude's outlived it, and nothing may be pushed.
    """
    try:
        import ctypes  # noqa: PLC0415  (here, since only settle needs it)
        ctypes.CDLL(None, use_errno=True).prctl(4, 0, 0, 0, 0)  # PR_SET_DUMPABLE, 0
    except (OSError, AttributeError):
        pass
    return stop_strays()


def kind_is_rebase(env: Mapping[str, str]) -> bool:
    """Whether this is the rebase runner, with an error printed when it is not."""
    if env.get("CLAUDE_RUNNER_KIND") == "rebase":
        return True
    print("::error::claude-job-rebase-runner runs on the rebase runner alone.")
    return False


def try_rebase(env: Mapping[str, str], run: Run = sh, git: Git = git_cmd) -> int:
    """Rebase the PR and push it when nothing conflicts. A conflict is left
    midway for Claude, with what settle needs in the step's outputs.
    """
    if not kind_is_rebase(env):
        return 1
    number = env["NUMBER"]
    pr = json.loads(
        run("gh", "pr", "view", number, "--json", "state,isDraft,isCrossRepository,headRefName,headRefOid,baseRefName"),
    )
    branch, base, old_head = pr["headRefName"], pr["baseRefName"], pr["headRefOid"]
    if pr["state"] != "OPEN" or pr["isDraft"] or pr["isCrossRepository"]:
        print(f"#{number} is not an open, ready PR from this repository: nothing to rebase.")
        output(env, claude="false")
        return 0
    if not (BRANCH.fullmatch(branch) and BRANCH.fullmatch(base) and SHA.fullmatch(old_head)):
        print(f"::error::#{number} names a branch or commit this will not touch.")
        return 1
    ok(git, "config", "user.name", env["BOT_NAME"])
    ok(git, "config", "user.email", env["BOT_EMAIL"])
    ok(git, "checkout", "-q", "-B", branch, old_head)
    new_base = ok(git, "rev-parse", f"origin/{base}")
    if not git("merge-base", "--is-ancestor", new_base, old_head).returncode:
        print(f"#{number} already has `{base}`: nothing to rebase.")
        output(env, claude="false")
        return 0
    tree = git("merge-tree", "--write-tree", "--name-only", "--no-messages", "-z", new_base, old_head)
    conflicted = [path for path in tree.stdout.split("\0")[1:] if path] if tree.returncode == 1 else []
    rebased = git("-c", "core.editor=true", "rebase", f"origin/{base}")
    if not rebased.returncode:
        push(env, git, "origin", branch, old_head)
        run("gh", "pr", "comment", number, "--body", f"Rebased onto `{base}` ({new_base[:7]}) with no conflicts. CI runs on the new head.")
        print(f"#{number}: rebased with no conflicts and pushed.")
        output(env, claude="false")
        return 0
    state = {"number": number, "branch": branch, "base": base, "old_head": old_head, "new_base": new_base, "conflicted": conflicted}
    output(env, state=json.dumps(state, separators=(",", ":")))
    print(f"#{number}: the rebase stopped at a conflict, in {', '.join(conflicted) or 'files git did not name'}; Claude resolves it.")
    output(env, claude="true")
    return 0


# env is unused, and stays for the callers that pass it.
def hand_on(env: Mapping[str, str], run: Run, git: Git, state: dict[str, Any], reason: str) -> None:  # noqa: ARG001
    """Turn the PR back into a draft and ask for its rebase on the work
    runners, saying why this one was not pushed.
    """
    number, base = state["number"], state["base"]
    try:
        run("gh", "pr", "ready", number, "--undo")
    except subprocess.CalledProcessError as error:
        print(f"::warning::#{number} stays as it is, not a draft: {(error.stderr or '').strip()[:200]}")
    try:
        old_base = ok(git, "merge-base", state["old_head"], state["new_base"])
        compare = f" `git range-diff {old_base[:12]}..{state['old_head'][:12]} origin/{base}..HEAD`."
    except RuntimeError:
        compare = f" `git range-diff origin/{base}...{state['old_head'][:12]}` after the rebase."
    run(
        "gh", "pr", "comment", number, "--body",
        f"@{BOT} review: rebase\n\n{ESCALATE} {state['new_base']} -->\n"
        f"`{base}` moved on to {state['new_base'][:7]}, and the rebase runner did not push its rebase: {reason}. "
        "So the engineer rebases it here, and the reviewer compares the change with what it was:" + compare,
    )
    print(f"#{number}: handed on, {reason}.")


def valid(state: object) -> bool:
    """Whether try's state has the shape, and names only the branches and
    commits, that settle will act on.
    """
    return bool(
        isinstance(state, dict)
        and str(state.get("number", "")).isdecimal()
        and all(isinstance(state.get(key), str) and BRANCH.fullmatch(state[key]) for key in ("branch", "base"))
        and all(isinstance(state.get(key), str) and SHA.fullmatch(state[key]) for key in ("old_head", "new_base"))
        and isinstance(state.get("conflicted"), list)
        and state["branch"] != state["base"],
    )


# Each return is one way it ends, and they read best in a row.
def settle(env: Mapping[str, str], run: Run = sh, checkout: str | None = None, remote: str | None = None) -> int:  # noqa: PLR0911
    """Push Claude's resolution when it is no more than a rebase, and hand
    the PR on when it is.
    """
    if not kind_is_rebase(env):
        return 1
    if not env.get("STATE", ""):
        print("No rebase waited for Claude.")
        return 0
    state = json.loads(env["STATE"])
    if not valid(state) or not REPO.fullmatch(env.get("GITHUB_REPOSITORY", "")):
        print("::error::try's state names a branch, commit or repository this will not touch.")
        return 1
    number = state["number"]
    pr = json.loads(run("gh", "pr", "view", number, "--json", "state,isCrossRepository,headRefName,headRefOid,baseRefName"))
    seen = (pr["state"], pr["isCrossRepository"], pr["headRefName"], pr["baseRefName"], pr["headRefOid"])
    if seen != ("OPEN", False, state["branch"], state["base"], state["old_head"]):
        print(f"#{number} changed while Claude worked on it: nothing pushed, nothing handed on.")
        return 0
    checkout = checkout or env.get("GITHUB_WORKSPACE") or os.getcwd()
    # The container's own /tmp: the work folder is on the host, where a
    # container Claude started over the socket could still reach it.
    own = tempfile.mkdtemp(prefix="claude-settle-")
    try:
        git = clean_git(own)
        ok(git, "init", "-q", "--bare")
        if rebasing(checkout):
            reason = "the rebase was left unfinished"
        else:
            fetched = git("fetch", "-q", "--no-tags", checkout,
                          "+HEAD:refs/heads/result", "+refs/remotes/origin/*:refs/remotes/origin/*")
            missing = fetched.returncode or any(
                git("cat-file", "-e", f"{sha}^{{commit}}").returncode for sha in (state["old_head"], state["new_base"])
            )
            try:
                reason = (
                    "its commits could not be read back from the checkout" if missing
                    else check(git, state, "refs/heads/result")
                )
            except RuntimeError as error:
                reason = f"it could not be checked ({error})"
        if reason is not None:
            hand_on(env, run, git, state, reason)
            return 0
        remote = remote or f"https://github.com/{env['GITHUB_REPOSITORY']}.git"
        try:
            push(env, git, remote, state["branch"], state["old_head"], "refs/heads/result")
        except RuntimeError as error:
            hand_on(env, run, git, state, str(error))
            return 0
    finally:
        shutil.rmtree(own, ignore_errors=True)
    run(
        "gh", "pr", "comment", number, "--body",
        f"Rebased onto `{state['base']}` ({state['new_base'][:7]}), resolving the conflicts in "
        + ", ".join(f"`{path}`" for path in state["conflicted"])
        + ". The PR's own change is as it was outside them. CI runs on the new head.",
    )
    print(f"#{number}: rebased, conflicts resolved, pushed.")
    return 0


def main(argv: list[str], env: Mapping[str, str] = os.environ) -> int:
    """Run try or settle, as the argument says."""
    if argv == ["try"]:
        return try_rebase(env)
    if argv == ["settle"]:
        if env.get("CLAUDE_RUNNER_KIND") == "rebase" and not guard():
            print("::error::Processes left from Claude's step would not stop: nothing is pushed.")
            return 1
        return settle(env)
    print("usage: claude-job-rebase-runner try | settle", file=sys.stderr)
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except (RuntimeError, KeyError, ValueError, subprocess.CalledProcessError) as error:
        print(f"::error::{error}")
        sys.exit(1)
