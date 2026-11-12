import json
import os
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path

import job_rebase_runner as runner
import pytest


def git(cwd: Path, *argv: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *argv], check=True, capture_output=True, text=True).stdout.strip()


def commit(cwd: Path, files: dict[str, str], message: str) -> str:
    for name, text in files.items():
        (cwd / name).write_text(text)
    git(cwd, "add", "-A")
    git(cwd, "commit", "-q", "-m", message)
    return git(cwd, "rev-parse", "HEAD")


LIST = "".join(f"line {n}\n" for n in range(1, 11))


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """Origin with main, and the PR's branch fix/3 adding to list.txt and app.txt."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    work = tmp_path / "work"
    subprocess.run(["git", "clone", "-q", str(origin), str(work)], check=True, capture_output=True)
    git(work, "config", "user.name", "someone")
    git(work, "config", "user.email", "someone@example.com")
    commit(work, {"list.txt": LIST, "app.txt": "a\nb\nc\n", "other.txt": "x\n"}, "start")
    git(work, "push", "-q", "origin", "main")
    git(work, "checkout", "-q", "-b", "fix/3")
    commit(work, {"list.txt": LIST.replace("line 10\n", "line 10\npr line\n"), "app.txt": "a\nb\nc\npr\n"}, "the PR")
    git(work, "push", "-q", "origin", "fix/3")
    git(work, "checkout", "-q", "main")
    return work


def main_moves(work: Path, files: dict[str, str]) -> None:
    git(work, "checkout", "-q", "main")
    commit(work, files, "main moves")
    git(work, "push", "-q", "origin", "main")
    git(work, "fetch", "-q", "origin")


class Gh(list[tuple[str, ...]]):
    def __init__(self, work: Path, draft: bool = False) -> None:
        super().__init__()
        self.work, self.draft = work, draft

    def __call__(self, *argv: str) -> str:
        self.append(argv)
        if argv[1:3] == ("pr", "view"):
            head = git(self.work, "rev-parse", "origin/fix/3")
            return json.dumps({"state": "OPEN", "isDraft": self.draft, "isCrossRepository": False,
                               "headRefName": "fix/3", "headRefOid": head, "baseRefName": "main"})
        return ""

    def comments(self) -> list[str]:
        return [call[-1] for call in self if call[1:3] == ("pr", "comment")]


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    out = tmp_path / "output"
    out.write_text("")
    temp = tmp_path / "temp"
    temp.mkdir()
    return {"CLAUDE_RUNNER_KIND": "rebase", "NUMBER": "7", "RUNNER_TEMP": str(temp), "GITHUB_OUTPUT": str(out),
            "BOT_NAME": "bot[bot]", "BOT_EMAIL": "1+bot[bot]@users.noreply.github.com", "GH_TOKEN": "ghs_secret",
            "GITHUB_REPOSITORY": "owner/repo"}


def in_repo(work: Path) -> Callable[..., subprocess.CompletedProcess[str]]:
    def run_git(*argv: str, env: Mapping[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["git", "-C", str(work), *argv], check=False, capture_output=True, text=True, env=env)
    return run_git


def outputs(env: dict[str, str]) -> dict[str, str]:
    return dict(line.split("=", 1) for line in open(env["GITHUB_OUTPUT"]).read().splitlines())


def test_a_rebase_with_no_conflicts_is_pushed_and_claude_never_starts(repo: Path, env: dict[str, str]) -> None:
    main_moves(repo, {"other.txt": "x\ny\n"})
    gh = Gh(repo)

    assert runner.try_rebase(env, run=gh, git=in_repo(repo)) == 0

    assert outputs(env) == {"claude": "false"}
    remote = git(repo, "ls-remote", "origin", "refs/heads/fix/3").split()[0]
    assert git(repo, "merge-base", "--is-ancestor", "origin/main", remote) == ""
    assert "with no conflicts" in gh.comments()[0]
    assert git(repo, "log", "-1", "--format=%cn", remote) == "bot[bot]"


def test_the_token_is_never_an_argument(repo: Path, env: dict[str, str]) -> None:
    main_moves(repo, {"other.txt": "x\ny\n"})
    seen: list[tuple[str, ...]] = []

    def spy(*argv: str, env: Mapping[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        seen.append(argv)
        return in_repo(repo)(*argv, env=env)

    runner.try_rebase(env, run=Gh(repo), git=spy)

    assert not [call for call in seen if any("ghs_secret" in part for part in call)]


def conflict(repo: Path, env: dict[str, str]) -> Gh:
    """Main adds its own last line to list.txt, where the PR added one."""
    main_moves(repo, {"list.txt": LIST.replace("line 10\n", "line 10\nmain line\n")})
    gh = Gh(repo)
    runner.try_rebase(env, run=gh, git=in_repo(repo))
    return gh


def test_a_conflict_is_left_midway_for_claude(repo: Path, env: dict[str, str]) -> None:
    conflict(repo, env)

    assert outputs(env)["claude"] == "true"
    state = json.loads(outputs(env)["state"])
    assert state["conflicted"] == ["list.txt"] and state["branch"] == "fix/3"
    assert runner.rebasing(str(repo))


def settle(repo: Path, env: dict[str, str], gh: Gh, **state: object) -> int:
    """Settle as the workflow runs it: try's state from its output, and
    the checkout's origin standing in for GitHub.
    """
    saved = json.loads(outputs(env)["state"]) | state
    origin = git(repo, "remote", "get-url", "origin")
    return runner.settle(env | {"STATE": json.dumps(saved)}, run=gh, checkout=str(repo), remote=origin)


def resolve(repo: Path, text: str) -> None:
    (repo / "list.txt").write_text(text)
    git(repo, "add", "list.txt")
    subprocess.run(["git", "-C", str(repo), "-c", "core.editor=true", "rebase", "--continue"], check=True, capture_output=True)


def test_a_resolution_keeping_both_sides_is_pushed(repo: Path, env: dict[str, str]) -> None:
    gh = conflict(repo, env)
    old = git(repo, "rev-parse", "origin/fix/3")
    resolve(repo, LIST.replace("line 10\n", "line 10\nmain line\npr line\n"))

    assert settle(repo, env, gh) == 0

    assert git(repo, "ls-remote", "origin", "refs/heads/fix/3").split()[0] != old
    assert "resolving the conflicts in `list.txt`" in gh.comments()[-1]
    assert not [a for a in gh if a[1:3] == ("pr", "ready")]


def test_a_resolution_that_rewrites_another_file_is_handed_on(repo: Path, env: dict[str, str]) -> None:
    gh = conflict(repo, env)
    old = git(repo, "rev-parse", "origin/fix/3")
    (repo / "app.txt").write_text("a\nB\nc\npr\n")
    git(repo, "add", "app.txt")
    resolve(repo, LIST.replace("line 10\n", "line 10\nmain line\npr line\n"))

    settle(repo, env, gh)

    assert git(repo, "ls-remote", "origin", "refs/heads/fix/3").split()[0] == old
    assert ("gh", "pr", "ready", "7", "--undo") in gh
    body = gh.comments()[-1]
    assert body.startswith("@claude review: rebase\n\n<!-- claude:auto-escalate ")
    assert "`app.txt`" in body and "git range-diff" in body


def test_a_resolution_moving_too_much_is_handed_on(repo: Path, env: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner, "MAX_MOVED", 2)
    gh = conflict(repo, env)
    resolve(repo, LIST.replace("line 10\n", "line 10\nmain line\npr line\nmore\nand more\nstill more\n"))

    settle(repo, env, gh)

    assert "more than the 2 a rebase here may" in gh.comments()[-1]


def test_an_unfinished_rebase_is_undone_and_handed_on(repo: Path, env: dict[str, str]) -> None:
    gh = conflict(repo, env)

    settle(repo, env, gh)

    assert "left unfinished" in gh.comments()[-1]


def test_conflict_markers_left_in_are_handed_on(repo: Path, env: dict[str, str]) -> None:
    gh = conflict(repo, env)
    resolve(repo, LIST.replace("line 10\n", "line 10\n<<<<<<< HEAD\nmain line\n=======\npr line\n>>>>>>> pr\n"))

    settle(repo, env, gh)

    assert "conflict markers" in gh.comments()[-1]


def test_a_draft_or_up_to_date_pr_is_left_alone(repo: Path, env: dict[str, str]) -> None:
    gh = Gh(repo, draft=True)
    assert runner.try_rebase(env, run=gh, git=in_repo(repo)) == 0
    assert outputs(env) == {"claude": "false"}

    open(env["GITHUB_OUTPUT"], "w").close()
    gh = Gh(repo)
    git(repo, "fetch", "-q", "origin")
    assert runner.try_rebase(env, run=gh, git=in_repo(repo)) == 0
    assert outputs(env) == {"claude": "false"}
    assert gh.comments() == []


def test_nothing_runs_off_the_rebase_runner(repo: Path, env: dict[str, str], capsys: pytest.CaptureFixture[str]) -> None:
    gh = Gh(repo)

    assert runner.try_rebase(env | {"CLAUDE_RUNNER_KIND": "work"}, run=gh, git=in_repo(repo)) == 1
    assert runner.settle(env | {"CLAUDE_RUNNER_KIND": ""}, run=gh, checkout=str(repo)) == 1
    assert gh == []


def test_settle_with_no_conflict_waiting_does_nothing(repo: Path, env: dict[str, str]) -> None:
    gh = Gh(repo)

    assert runner.settle(env, run=gh, checkout=str(repo)) == 0
    assert gh == []


def test_the_change_is_counted_by_lines_not_by_where_they_sit() -> None:
    before = {"a": runner.collections.Counter({"+x": 1, "-y": 1})}
    after = {"a": runner.collections.Counter({"+x": 1, "-y": 1}), "b": runner.collections.Counter({"+z": 2})}

    assert runner.moved(before, after) == {"b": 2}
    assert runner.moved(before, before) == {}


def resolved(repo: Path, env: dict[str, str]) -> Gh:
    gh = conflict(repo, env)
    resolve(repo, LIST.replace("line 10\n", "line 10\nmain line\npr line\n"))
    return gh


def test_a_state_naming_the_default_branch_is_refused(repo: Path, env: dict[str, str]) -> None:
    gh = resolved(repo, env)
    main = git(repo, "rev-parse", "origin/main")

    assert settle(repo, env, gh, branch="main", old_head=main) == 1
    assert git(repo, "ls-remote", "origin", "refs/heads/main").split()[0] == main
    assert gh.comments() == []


def test_a_state_naming_another_head_than_the_prs_pushes_nothing(repo: Path, env: dict[str, str]) -> None:
    gh = resolved(repo, env)
    old = git(repo, "rev-parse", "origin/fix/3")

    assert settle(repo, env, gh, old_head=git(repo, "rev-parse", "origin/main")) == 0
    assert git(repo, "ls-remote", "origin", "refs/heads/fix/3").split()[0] == old
    assert gh.comments() == []


def booby_trap(repo: Path) -> None:
    """What Claude could leave in the checkout to hide a change or run code."""
    (repo / ".git" / "info").mkdir(exist_ok=True)
    (repo / ".git" / "info" / "attributes").write_text("* -diff\n")
    git(repo, "config", "diff.external", "true")
    hook = repo / ".git" / "hooks" / "pre-push"
    hook.write_text(f"#!/bin/sh\ntouch {repo / 'pwned'}\nexit 1\n")
    hook.chmod(0o755)


def test_config_attributes_and_hooks_in_the_checkout_hide_nothing(repo: Path, env: dict[str, str]) -> None:
    gh = conflict(repo, env)
    old = git(repo, "rev-parse", "origin/fix/3")
    (repo / "app.txt").write_text("a\nB\nc\npr\n")
    git(repo, "add", "app.txt")
    resolve(repo, LIST.replace("line 10\n", "line 10\nmain line\npr line\n"))
    booby_trap(repo)

    settle(repo, env, gh)

    assert git(repo, "ls-remote", "origin", "refs/heads/fix/3").split()[0] == old
    assert "`app.txt`" in gh.comments()[-1]
    assert not (repo / "pwned").exists()


def test_a_good_resolution_is_pushed_past_the_checkouts_hook(repo: Path, env: dict[str, str]) -> None:
    gh = resolved(repo, env)
    booby_trap(repo)

    settle(repo, env, gh)

    assert "resolving the conflicts" in gh.comments()[-1]
    assert not (repo / "pwned").exists()


def test_a_binary_file_changed_is_counted(repo: Path, env: dict[str, str]) -> None:
    gh = conflict(repo, env)
    (repo / "logo.bin").write_bytes(b"\0\1\2")
    git(repo, "add", "logo.bin")
    resolve(repo, LIST.replace("line 10\n", "line 10\nmain line\npr line\n"))

    settle(repo, env, gh)

    assert "`logo.bin`" in gh.comments()[-1]


def test_strays_are_everything_of_the_account_off_my_line() -> None:
    processes = {1: (0, 1000), 7: (1, 1000), 8: (7, 1000), 9: (8, 1000), 20: (1, 1000), 21: (20, 1000), 30: (1, 0)}

    assert runner.strays(processes, 9, 1000) == [20, 21]


def test_lines_of_a_generated_file_that_conflicted_are_not_counted(
    repo: Path, env: dict[str, str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runner, "MAX_MOVED", 2)
    main_moves(repo, {"list.txt": LIST.replace("line 10\n", "line 10\nmain line\n"),
                      ".gitattributes": "list.txt linguist-generated\n"})
    gh = Gh(repo)
    runner.try_rebase(env, run=gh, git=in_repo(repo))
    resolve(repo, LIST.replace("line 10\n", "line 10\nmain line\npr line\nmore\nand more\nstill more\n"))

    settle(repo, env, gh)

    assert "resolving the conflicts" in gh.comments()[-1]


def test_a_generated_file_outside_the_conflicts_is_still_handed_on(repo: Path, env: dict[str, str]) -> None:
    main_moves(repo, {"list.txt": LIST.replace("line 10\n", "line 10\nmain line\n"),
                      ".gitattributes": "app.txt linguist-generated\n"})
    gh = Gh(repo)
    runner.try_rebase(env, run=gh, git=in_repo(repo))
    (repo / "app.txt").write_text("a\nB\nc\npr\n")
    git(repo, "add", "app.txt")
    resolve(repo, LIST.replace("line 10\n", "line 10\nmain line\npr line\n"))

    settle(repo, env, gh)

    assert "`app.txt`" in gh.comments()[-1]


def test_a_catalogues_source_references_do_not_count(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    git(tmp_path, "config", "user.name", "x")
    git(tmp_path, "config", "user.email", "x@example.com")
    first = commit(tmp_path, {"fi.po": '#: a.py:1\nmsgid "a"\nmsgstr "A"\n'}, "a")
    second = commit(tmp_path, {"fi.po": '#: a.py:7 b.py:2\nmsgid "a"\nmsgstr "Ä"\n'}, "b")

    found = runner.changes(in_repo(tmp_path), first, second)

    assert found == {"fi.po": runner.collections.Counter({'-msgstr "A"': 1, '+msgstr "Ä"': 1})}


@pytest.mark.parametrize(("path", "rebuilt"), [
    ("uv.lock", True), ("components/frontend/bun.lock", True), ("package-lock.json", True),
    ("docs/openapi.json", True), ("components/sdk-python/src/blog/models/x.py", True),
    ("components/server/src/register.py", False), ("openapi.json", False),
])
def test_what_counts_as_rebuilt(path: str, rebuilt: bool) -> None:
    patterns = runner.generated(
        "# c\n/docs/openapi.json linguist-generated\ncomponents/sdk-python/** linguist-generated=true\n*.png binary\n",
    )

    assert runner.rebuilt(path, patterns) is rebuilt


def test_strays_are_killed_until_none_is_left() -> None:
    me, parent, uid = os.getpid(), os.getppid(), os.getuid()
    mine = {me: (parent, uid), parent: (1, uid), 1: (0, uid)}
    rounds = [mine | {4242: (1, uid)}, mine | {4243: (1, uid)}, mine]
    killed = []

    assert runner.stop_strays(kill=lambda pid, sig: killed.append(pid), processes=lambda: rounds.pop(0), sleep=lambda s: None)

    assert killed == [4242, 4243]


def test_strays_that_will_not_stop_are_reported() -> None:
    me, parent, uid = os.getpid(), os.getppid(), os.getuid()
    table = {me: (parent, uid), parent: (1, uid), 1: (0, uid), 4242: (1, uid)}

    assert not runner.stop_strays(kill=lambda pid, sig: None, processes=lambda: table, sleep=lambda s: None)


def test_a_mode_changed_outside_the_conflicts_is_handed_on(repo: Path, env: dict[str, str]) -> None:
    gh = conflict(repo, env)
    old = git(repo, "rev-parse", "origin/fix/3")
    (repo / "app.txt").chmod(0o755)
    git(repo, "add", "app.txt")
    resolve(repo, LIST.replace("line 10\n", "line 10\nmain line\npr line\n"))

    settle(repo, env, gh)

    assert git(repo, "ls-remote", "origin", "refs/heads/fix/3").split()[0] == old
    assert "`app.txt`" in gh.comments()[-1]


def test_an_empty_file_added_outside_the_conflicts_is_handed_on(repo: Path, env: dict[str, str]) -> None:
    gh = conflict(repo, env)
    old = git(repo, "rev-parse", "origin/fix/3")
    (repo / "conftest.py").write_text("")
    git(repo, "add", "conftest.py")
    resolve(repo, LIST.replace("line 10\n", "line 10\nmain line\npr line\n"))

    settle(repo, env, gh)

    assert git(repo, "ls-remote", "origin", "refs/heads/fix/3").split()[0] == old
    assert "`conftest.py`" in gh.comments()[-1]
