import base64
import io
import json
import re
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import IO, Any, Never, cast
from urllib.request import Request

import pytest
import workbench_gate as gate
import yaml

ME = ("octo", "33314057+octo@users.noreply.github.com")
SOMEONE = ("Someone Else", "someone@example.com")
World = dict[str, Any]
Opener = Callable[[Request, float], IO[bytes]]
DEFAULTS = yaml.safe_load((Path(__file__).parents[3] / "defaults" / "main.yml").read_text())


def git(cwd: Path, *args: str, who: tuple[str, str] = ME, stdin: bytes | None = None) -> bytes:
    env = {
        "GIT_AUTHOR_NAME": who[0],
        "GIT_AUTHOR_EMAIL": who[1],
        "GIT_COMMITTER_NAME": who[0],
        "GIT_COMMITTER_EMAIL": who[1],
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "PATH": "/usr/bin:/bin",
        "HOME": str(cwd),
    }
    return subprocess.run(["git", "-C", str(cwd), *args], env=env, check=True, capture_output=True, input=stdin).stdout


def commit(cwd: Path, name: str, message: str, who: tuple[str, str] = ME) -> str:
    (cwd / name).write_text(message)
    git(cwd, "add", name)
    git(cwd, "commit", "-q", "-m", message, who=who)
    return git(cwd, "rev-parse", "HEAD").decode().strip()


class GitHub:
    """The API calls the gate makes, answered and recorded."""

    def __init__(self, repo: str = "octo/shop", pr: dict[str, Any] | None = None, private: bool | None = True) -> None:
        self.repo = repo
        self.private = private
        self.calls: list[tuple[str, str, Any]] = []
        self.pr = pr or {
            "state": "open",
            "head": {"ref": "feat/3-runs", "repo": {"full_name": repo}},
            "base": {"repo": {"default_branch": "main"}},
            "html_url": f"https://github.com/{repo}/pull/12",
        }

    def __call__(self, req: Request, timeout: float) -> io.BytesIO:
        body = json.loads(cast("bytes", req.data)) if req.data else None
        path = req.full_url.removeprefix("https://api.github.com")
        self.calls.append((req.get_method(), path, body))
        about = {"full_name": self.repo, "default_branch": "main", "private": self.private, "permissions": {"push": True}}
        answers = {
            ("GET", f"/repos/{self.repo}"): about,
            ("POST", f"/repos/{self.repo}/pulls"): {"html_url": f"https://github.com/{self.repo}/pull/13"},
            ("GET", f"/repos/{self.repo}/pulls/12"): self.pr,
            ("PATCH", f"/repos/{self.repo}/pulls/12"): {"html_url": f"https://github.com/{self.repo}/pull/12"},
        }
        return io.BytesIO(json.dumps(answers[(req.get_method(), path)]).encode())


@pytest.fixture
def world(tmp_path: Path) -> World:
    """A GitHub with shop on it, a workbench checkout of it, and the gate's config."""
    origin = tmp_path / "github" / "octo" / "shop.git"
    origin.parent.mkdir(parents=True)
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    work = tmp_path / "work"
    git(tmp_path, "clone", "-q", str(origin), str(work))
    git(work, "switch", "-q", "-c", "main")
    commit(work, "README", "the start", who=SOMEONE)
    git(work, "push", "-q", "origin", "main")
    git(work, "switch", "-q", "-c", "feat/3-runs")
    state = tmp_path / "state"
    state.mkdir()
    token = tmp_path / "token"
    token.write_text("github_pat_secret\n")
    config = {
        "token": str(token),
        "api": "https://api.github.com",
        "git_base": f"file://{tmp_path / 'github'}",
        "state_dir": str(state),
        "identity": {"name": ME[0], "email": ME[1]},
        "branch_pattern": DEFAULTS["workbench_branch_pattern"],
        "forbidden": [r"co-authored-by:.*(claude|anthropic)", r"generated with \[?claude", r"noreply@anthropic\.com"],
        "forbidden_paths": [r"^\.bench/", r"^\.claude/settings\.local\.json$"],
        "repos": {"shop": {"repo": "octo/shop"}, "blog": {"repo": "octo/blog"}},
        "client_uid": 2002,
    }
    path = tmp_path / "gate.json"
    path.write_text(json.dumps(config))
    return {"origin": origin, "work": work, "config": config, "path": path}


def bundle(work: Path, branch: str = "feat/3-runs") -> io.BytesIO:
    return io.BytesIO(git(work, "bundle", "create", "-", f"refs/heads/{branch}"))


def run(world: World, argv: list[str], stdin: io.BytesIO | io.StringIO | None = None, github: Opener | None = None) -> int:
    return gate.main(argv, str(world["path"]), stdin=stdin, opener=github or GitHub())


def on_github(world: World, branch: str) -> str | None:
    argv = ["git", "-C", str(world["origin"]), "rev-parse", "-q", "--verify", f"refs/heads/{branch}"]
    result = subprocess.run(argv, check=False, capture_output=True, text=True)
    return result.stdout.strip() or None


def test_pushes_a_branch_of_the_users_commits(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    sha = commit(world["work"], "a.py", "Add a")
    assert run(world, ["push", "shop", "feat/3-runs"], bundle(world["work"])) == 0
    assert on_github(world, "feat/3-runs") == sha
    assert "1 new commit" in capsys.readouterr().out


class Spy:
    """What the gate pushes with, recorded, and the real git run."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.pushed_with: list[str] = []
        real = subprocess.run

        def run(argv: list[str], *args: Any, **kwargs: Any) -> subprocess.CompletedProcess[Any]:  # noqa: ANN401 - forwards subprocess.run's
            if "push" in argv:
                header = kwargs["env"]["GIT_CONFIG_VALUE_0"].removeprefix("Authorization: Basic ")
                self.pushed_with.append(base64.b64decode(header).decode().split(":", 1)[1])
            return real(argv, *args, **kwargs)

        monkeypatch.setattr(gate.subprocess, "run", run)


def workflow(work: Path) -> str:
    (work / ".github" / "workflows").mkdir(parents=True)
    return commit(work, ".github/workflows/ci.yml", "Add CI")


def with_workflow_token(world: World, tmp_path: Path) -> None:
    token = tmp_path / "workflow-token"
    token.write_text("github_pat_workflows\n")
    world["config"]["repos"]["shop"]["workflow_token"] = str(token)
    world["path"].write_text(json.dumps(world["config"]))


def test_a_workflow_goes_with_the_repositorys_workflow_token(world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with_workflow_token(world, tmp_path)
    sha = workflow(world["work"])
    spy = Spy(monkeypatch)
    assert run(world, ["push", "shop", "feat/3-runs"], bundle(world["work"])) == 0
    assert on_github(world, "feat/3-runs") == sha
    assert spy.pushed_with == ["github_pat_workflows"]


def test_any_other_push_keeps_the_ordinary_token(world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with_workflow_token(world, tmp_path)
    commit(world["work"], "a.py", "Add a")
    spy = Spy(monkeypatch)
    assert run(world, ["push", "shop", "feat/3-runs"], bundle(world["work"])) == 0
    assert spy.pushed_with == ["github_pat_secret"]


def test_a_repository_without_the_token_pushes_a_workflow_with_the_ordinary_one(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    workflow(world["work"])
    spy = Spy(monkeypatch)
    assert run(world, ["push", "shop", "feat/3-runs"], bundle(world["work"])) == 0
    assert spy.pushed_with == ["github_pat_secret"]


def test_the_workflow_token_never_reaches_the_default_branch(world: World, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    with_workflow_token(world, tmp_path)
    git(world["work"], "switch", "-q", "main")
    workflow(world["work"])
    before = on_github(world, "main")
    world["config"]["branch_pattern"] = ".*"
    world["path"].write_text(json.dumps(world["config"]))
    assert run(world, ["push", "shop", "main"], bundle(world["work"], "main")) == 1
    assert on_github(world, "main") == before
    assert "is the default branch" in capsys.readouterr().err


def test_a_workflow_by_someone_else_is_refused_before_the_token_is_used(
    world: World,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with_workflow_token(world, tmp_path)
    (world["work"] / ".github" / "workflows").mkdir(parents=True)
    commit(world["work"], ".github/workflows/ci.yml", "Add CI", who=SOMEONE)
    spy = Spy(monkeypatch)
    assert run(world, ["push", "shop", "feat/3-runs"], bundle(world["work"])) == 1
    assert spy.pushed_with == []
    assert "not octo" in capsys.readouterr().err


def test_the_other_repository_does_not_get_the_token(world: World) -> None:
    world["config"]["repos"]["blog"]["workflow_token"] = "/nonexistent"
    assert gate.read_workflow_token(world["config"], "shop") is None


def test_never_pushes_the_default_branch(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    git(world["work"], "switch", "-q", "main")
    commit(world["work"], "a.py", "Add a")
    before = on_github(world, "main")
    assert run(world, ["push", "shop", "main"], bundle(world["work"], "main")) == 1
    assert on_github(world, "main") == before
    assert "not a branch the workbench pushes" in capsys.readouterr().err


@pytest.mark.parametrize(
    "branch",
    [
        "wip", "main", "feat/../main", "feat/x.lock", "Feat/x", "feat/-x", "refs/heads/main",
        "feat/x y", "107", "107-", "107-X", "x-107", "107/x",
    ],
)
def test_refuses_branch_names_outside_the_pattern(world: World, branch: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert run(world, ["push", "shop", branch], io.BytesIO(b"")) == 1
    assert "refused" in capsys.readouterr().err


def test_refuses_a_name_not_in_the_config(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    assert run(world, ["push", "octo/other", "feat/3-runs"], io.BytesIO(b"")) == 2
    assert "not a workbench repository" in capsys.readouterr().err


def test_refuses_a_commit_by_someone_else(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    commit(world["work"], "a.py", "Add a", who=("example[bot]", "1+example[bot]@users.noreply.github.com"))
    assert run(world, ["push", "shop", "feat/3-runs"], bundle(world["work"])) == 1
    assert on_github(world, "feat/3-runs") is None
    assert "author example[bot]" in capsys.readouterr().err


def test_refuses_a_commit_committed_by_someone_else(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    (world["work"] / "a.py").write_text("a")
    git(world["work"], "add", "a.py")
    subprocess.run(
        ["git", "-C", str(world["work"]), "commit", "-q", "-m", "Add a"],
        env={
            "GIT_AUTHOR_NAME": ME[0],
            "GIT_AUTHOR_EMAIL": ME[1],
            "GIT_COMMITTER_NAME": "Claude",
            "GIT_COMMITTER_EMAIL": "noreply@anthropic.com",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "PATH": "/usr/bin:/bin",
        },
        check=True,
    )
    assert run(world, ["push", "shop", "feat/3-runs"], bundle(world["work"])) == 1
    assert "committer Claude" in capsys.readouterr().err


@pytest.mark.parametrize(
    "message",
    [
        "Add a\n\nCo-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>",
        "Add a\n\nco-authored-by: claude <x@example.com>",
        "Add a\n\n🤖 Generated with [Claude Code](https://claude.com/claude-code)",
    ],
)
def test_refuses_a_commit_that_names_the_agents(world: World, message: str, capsys: pytest.CaptureFixture[str]) -> None:
    commit(world["work"], "a.py", message)
    assert run(world, ["push", "shop", "feat/3-runs"], bundle(world["work"])) == 1
    assert on_github(world, "feat/3-runs") is None
    assert "forbidden pattern" in capsys.readouterr().err


def test_refuses_any_commit_on_the_branch_not_only_the_last(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    commit(world["work"], "a.py", "Add a\n\nCo-Authored-By: Claude <noreply@anthropic.com>")
    commit(world["work"], "b.py", "Add b")
    assert run(world, ["push", "shop", "feat/3-runs"], bundle(world["work"])) == 1


@pytest.mark.parametrize("path", [".bench/feat-3-runs.md", ".claude/settings.local.json"])
def test_refuses_the_workbenchs_own_files(world: World, path: str, capsys: pytest.CaptureFixture[str]) -> None:
    (world["work"] / path).parent.mkdir(parents=True, exist_ok=True)
    commit(world["work"], path, "Add notes")
    assert run(world, ["push", "shop", "feat/3-runs"], bundle(world["work"])) == 1
    assert f"touches {path}" in capsys.readouterr().err


def test_refuses_a_merge_that_brings_in_a_forbidden_path(world: World) -> None:
    work = world["work"]
    commit(work, "a.py", "Add a")
    git(work, "switch", "-q", "-c", "fix/side", "main")
    commit(work, "b.py", "Add b")
    git(work, "switch", "-q", "feat/3-runs")
    git(work, "merge", "-q", "--no-ff", "--no-commit", "fix/side")
    (work / ".bench").mkdir()
    (work / ".bench" / "notes.md").write_text("notes")
    git(work, "add", "-f", ".bench/notes.md")
    git(work, "commit", "-q", "-m", "Merge fix/side")
    assert len(git(work, "rev-list", "--parents", "-n1", "HEAD").split()) == 3
    assert run(world, ["push", "shop", "feat/3-runs"], bundle(work)) == 1
    assert on_github(world, "feat/3-runs") is None


def test_a_plain_merge_goes_through(world: World) -> None:
    work = world["work"]
    commit(work, "a.py", "Add a")
    git(work, "switch", "-q", "-c", "fix/side", "main")
    commit(work, "b.py", "Add b")
    git(work, "switch", "-q", "feat/3-runs")
    git(work, "merge", "-q", "--no-ff", "-m", "Merge fix/side", "fix/side")
    assert run(world, ["push", "shop", "feat/3-runs"], bundle(work)) == 0


def test_a_message_that_is_not_utf8_is_still_checked(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    work = world["work"]
    (work / "a.py").write_text("a")
    git(work, "add", "a.py")
    message = "Add a\n\nCo-authored-by: Claude <noreply@anthropic.com> \xe4".encode("latin-1")
    git(work, "-c", "i18n.commitEncoding=latin-1", "commit", "-q", "-F", "-", stdin=message)
    assert run(world, ["push", "shop", "feat/3-runs"], bundle(work)) == 1
    assert "forbidden pattern" in capsys.readouterr().err


def test_commits_already_on_github_are_not_checked(world: World) -> None:
    commit(world["work"], "a.py", "Add a")
    assert run(world, ["push", "shop", "feat/3-runs"], bundle(world["work"])) == 0


def test_refuses_a_rewrite_without_force(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    commit(world["work"], "a.py", "Add a")
    assert run(world, ["push", "shop", "feat/3-runs"], bundle(world["work"])) == 0
    pushed = on_github(world, "feat/3-runs")
    git(world["work"], "commit", "-q", "--amend", "-m", "Add a, better")
    assert run(world, ["push", "shop", "feat/3-runs"], bundle(world["work"])) == 1
    assert on_github(world, "feat/3-runs") == pushed
    assert "push --force" in capsys.readouterr().err


def test_force_replaces_what_the_gate_pushed(world: World) -> None:
    commit(world["work"], "a.py", "Add a")
    run(world, ["push", "shop", "feat/3-runs"], bundle(world["work"]))
    git(world["work"], "commit", "-q", "--amend", "-m", "Add a, better")
    amended = git(world["work"], "rev-parse", "HEAD").decode().strip()
    assert run(world, ["push", "shop", "feat/3-runs", "--force"], bundle(world["work"])) == 0
    assert on_github(world, "feat/3-runs") == amended
    git(world["work"], "commit", "-q", "--amend", "-m", "Add a, best")
    assert run(world, ["push", "shop", "feat/3-runs", "--force"], bundle(world["work"])) == 0


def push_from_the_workstation(world: World, tmp_path: Path, branch: str = "feat/3-runs", start: str | None = None) -> str:
    other = tmp_path / "other"
    if start is not None:
        git(tmp_path, "clone", "-q", "-b", start, str(world["origin"]), str(other))
        git(other, "switch", "-q", "-c", branch)
    else:
        git(tmp_path, "clone", "-q", "-b", branch, str(world["origin"]), str(other))
    theirs = commit(other, "c.py", "Add c")
    git(other, "push", "-q", "origin", branch)
    return theirs


def test_force_never_replaces_a_push_from_elsewhere(world: World, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    commit(world["work"], "a.py", "Add a")
    run(world, ["push", "shop", "feat/3-runs"], bundle(world["work"]))
    theirs = push_from_the_workstation(world, tmp_path)
    git(world["work"], "commit", "-q", "--amend", "-m", "Add a, better")
    assert run(world, ["push", "shop", "feat/3-runs", "--force"], bundle(world["work"])) == 1
    assert on_github(world, "feat/3-runs") == theirs
    assert "did not push there" in capsys.readouterr().err


def test_force_never_replaces_a_branch_the_gate_never_pushed(world: World, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    theirs = push_from_the_workstation(world, tmp_path, "feat/mine", start="main")
    git(world["work"], "fetch", "-q", "origin")
    git(world["work"], "switch", "-q", "-c", "feat/mine", "origin/main")
    commit(world["work"], "a.py", "Replace it")
    assert run(world, ["push", "shop", "feat/mine", "--force"], bundle(world["work"], "feat/mine")) == 1
    assert on_github(world, "feat/mine") == theirs


def test_commits_on_top_of_someone_elses_branch_go_through(world: World, tmp_path: Path) -> None:
    theirs = push_from_the_workstation(world, tmp_path, "feat/mine", start="main")
    git(world["work"], "fetch", "-q", "origin")
    git(world["work"], "switch", "-q", "-c", "feat/mine", theirs)
    ours = commit(world["work"], "a.py", "Add a")
    assert run(world, ["push", "shop", "feat/mine"], bundle(world["work"], "feat/mine")) == 0
    assert on_github(world, "feat/mine") == ours


@pytest.mark.parametrize("branch", ["feat/3-runs", "fix/12-login-language", "107-health-endpoints-app-metrics-and"])
def test_accepts_a_typed_branch_and_a_jobs_starting_name(branch: str) -> None:
    assert re.fullmatch(DEFAULTS["workbench_branch_pattern"], branch)


def test_commits_on_top_of_a_jobs_unrenamed_branch_go_through(world: World, tmp_path: Path) -> None:
    theirs = push_from_the_workstation(world, tmp_path, "107-health-endpoints", start="main")
    git(world["work"], "fetch", "-q", "origin")
    git(world["work"], "switch", "-q", "-c", "107-health-endpoints", theirs)
    ours = commit(world["work"], "a.py", "Add a")
    assert run(world, ["push", "shop", "107-health-endpoints"], bundle(world["work"], "107-health-endpoints")) == 0
    assert on_github(world, "107-health-endpoints") == ours


def test_refuses_a_bundle_without_the_branch(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    commit(world["work"], "a.py", "Add a")
    git(world["work"], "branch", "fix/other")
    assert run(world, ["push", "shop", "feat/3-runs"], bundle(world["work"], "fix/other")) == 1


def test_refuses_an_oversized_bundle(world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(gate, "MAX_BUNDLE", 10)
    commit(world["work"], "a.py", "Add a")
    assert run(world, ["push", "shop", "feat/3-runs"], bundle(world["work"])) == 1
    assert "over" in capsys.readouterr().err
    assert not list((world["work"].parent / "state").glob("*.bundle"))


def test_never_runs_hooks_from_the_bundle_or_the_mirror(world: World, tmp_path: Path) -> None:
    marker = tmp_path / "ran"
    hooks = tmp_path / "state" / "shop.git" / "hooks"
    commit(world["work"], "a.py", "Add a")
    run(world, ["push", "shop", "feat/3-runs"], bundle(world["work"]))
    (hooks / "pre-push").write_text(f"#!/bin/sh\ntouch {marker}\n")
    (hooks / "pre-push").chmod(0o755)
    commit(world["work"], "b.py", "Add b")
    assert run(world, ["push", "shop", "feat/3-runs"], bundle(world["work"])) == 0
    assert not marker.exists()


def test_the_token_is_never_an_argument(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []
    real = subprocess.run

    def spy(argv: list[str], *args: Any, **kwargs: Any) -> subprocess.CompletedProcess[Any]:  # noqa: ANN401 - forwards subprocess.run's
        seen.append(argv)
        return real(argv, *args, **kwargs)

    monkeypatch.setattr(gate.subprocess, "run", spy)
    commit(world["work"], "a.py", "Add a")
    run(world, ["push", "shop", "feat/3-runs"], bundle(world["work"]))
    assert seen and not any("github_pat_secret" in " ".join(argv) for argv in seen)


def test_opens_a_pr_against_the_default_branch(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    github = GitHub()
    text = io.StringIO(json.dumps({"title": "Adapt the runs", "body": "What and why."}))
    assert run(world, ["pr", "shop", "feat/3-runs"], text, github) == 0
    sent = {"title": "Adapt the runs", "body": "What and why.", "head": "feat/3-runs", "base": "main", "draft": False}
    assert ("POST", "/repos/octo/shop/pulls", sent) in github.calls
    assert "pull/13" in capsys.readouterr().out


@pytest.mark.parametrize("private", [False, None])
def test_opens_a_draft_on_a_public_repository(world: World, private: bool | None) -> None:
    """None stands for an answer without the field: unknown is public."""
    github = GitHub(private=private)
    text = io.StringIO(json.dumps({"title": "Adapt the runs"}))
    assert run(world, ["pr", "shop", "feat/3-runs"], text, github) == 0
    assert github.calls[-1][0] == "POST"
    assert github.calls[-1][2]["draft"] is True


@pytest.mark.parametrize("key", ["title", "body"])
def test_refuses_a_pr_that_names_the_agents(world: World, key: str) -> None:
    text = {"title": "Adapt the runs", "body": "What and why."}
    text[key] += "\n\n🤖 Generated with [Claude Code](https://claude.com/claude-code)"
    github = GitHub()
    assert run(world, ["pr", "shop", "feat/3-runs"], io.StringIO(json.dumps(text)), github) == 1
    assert not [call for call in github.calls if call[0] == "POST"]


@pytest.mark.parametrize("text", ['{"title": "x", "head": "main"}', '{"title": 3}', "not json", '["title"]', '{"body": "no title"}'])
def test_refuses_a_pr_with_anything_but_a_title_and_body(world: World, text: str) -> None:
    github = GitHub()
    assert run(world, ["pr", "shop", "feat/3-runs"], io.StringIO(text), github) == 1
    assert not [call for call in github.calls if call[0] == "POST"]


def test_refuses_a_pr_from_the_default_branch(world: World) -> None:
    github = GitHub()
    assert run(world, ["pr", "shop", "main"], io.StringIO('{"title": "x"}'), github) == 1
    assert not [call for call in github.calls if call[0] == "POST"]


def test_edits_a_workbench_pr(world: World) -> None:
    github = GitHub()
    assert run(world, ["pr-edit", "shop", "12"], io.StringIO('{"body": "Now with tests."}'), github) == 0
    assert ("PATCH", "/repos/octo/shop/pulls/12", {"body": "Now with tests."}) in github.calls


@pytest.mark.parametrize(
    "pr",
    [
        {
            "state": "closed", "head": {"ref": "feat/3-runs", "repo": {"full_name": "octo/shop"}},
            "base": {"repo": {"default_branch": "main"}},
        },
        {
            "state": "open", "head": {"ref": "dependabot/pip/x", "repo": {"full_name": "octo/shop"}},
            "base": {"repo": {"default_branch": "main"}},
        },
        {
            "state": "open", "head": {"ref": "feat/3-runs", "repo": {"full_name": "someone/shop"}},
            "base": {"repo": {"default_branch": "main"}},
        },
        {"state": "open", "head": {"ref": "feat/3-runs", "repo": None}, "base": {"repo": {"default_branch": "main"}}},
    ],
)
def test_edits_no_other_pr(world: World, pr: dict[str, Any]) -> None:
    github = GitHub(pr=pr)
    assert run(world, ["pr-edit", "shop", "12"], io.StringIO('{"body": "x"}'), github) == 1
    assert not [call for call in github.calls if call[0] == "PATCH"]


@pytest.mark.parametrize("number", ["+12", "12 ", "١٢", "../12"])
def test_refuses_anything_but_a_pr_number(world: World, number: str) -> None:
    github = GitHub()
    assert run(world, ["pr-edit", "shop", number], io.StringIO('{"body": "x"}'), github) == 1
    assert not github.calls


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["merge", "shop", "12"],
        ["push", "shop"],
        ["push", "shop", "feat/3-runs", "--tags"],
        ["push", "shop", "feat/3-runs", "--force", "HEAD"],
        ["check"],
        ["pr", "shop", "feat/3-runs", "extra"],
    ],
)
def test_refuses_any_other_shape(world: World, argv: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    assert run(world, argv, io.BytesIO(b"")) == 2
    assert "usage" in capsys.readouterr().err


def test_check_reports_the_repository(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    assert run(world, ["check", "shop"]) == 0
    assert json.loads(capsys.readouterr().out) == {"repo": "octo/shop", "default_branch": "main", "push": True}


@pytest.mark.parametrize("argv", [["push", "shop", "main"], ["pr", "shop", "main"]])
def test_refuses_a_bad_branch_before_touching_the_token_or_github(world: World, argv: list[str]) -> None:
    (world["path"].parent / "token").unlink()
    github = GitHub()
    assert run(world, argv, io.BytesIO(b"") if argv[0] == "push" else io.StringIO('{"title": "x"}'), github) == 1
    assert not github.calls


def test_says_what_is_missing_without_a_token(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    (world["path"].parent / "token").unlink()
    assert run(world, ["check", "shop"]) == 1
    assert "token is missing" in capsys.readouterr().err


def test_says_when_github_cannot_be_reached(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    def offline(req: Request, timeout: float) -> Never:
        raise gate.urllib.error.URLError("no route")

    assert run(world, ["check", "shop"], github=offline) == 1
    assert "could not be reached" in capsys.readouterr().err


def test_a_long_body_within_the_limit_is_not_cut_off(world: World) -> None:
    body = 'ä\n"' * (gate.MAX_BODY // 3)
    github = GitHub()
    assert run(world, ["pr", "shop", "feat/3-runs"], io.StringIO(json.dumps({"title": "x", "body": body})), github) == 0
    assert next(call for call in github.calls if call[0] == "POST")[2]["body"] == body


def serve(
    world: World,
    request: bytes,
    payload: bytes = b"",
    uid: int = 2002,
    github: Opener | None = None,
    name: str = "shop",
) -> tuple[int, str]:
    """A connection on a repository's socket: the request line, then the payload."""
    conn = io.BufferedReader(io.BytesIO(request + b"\n" + payload))
    out = io.StringIO()
    assert gate.serve(name, str(world["path"]), conn, out, uid, github or GitHub()) == 0
    *said, status = out.getvalue().splitlines()
    return int(status.removeprefix("status ")), "\n".join(said)


def test_serves_a_push_on_the_repositorys_socket(world: World) -> None:
    sha = commit(world["work"], "a.py", "Add a")
    status, said = serve(world, b'["push", "feat/3-runs"]', bundle(world["work"]).read())
    assert status == 0
    assert on_github(world, "feat/3-runs") == sha
    assert "1 new commit" in said


def test_the_repository_is_the_sockets_not_the_clients(world: World) -> None:
    commit(world["work"], "a.py", "Add a")
    status, _said = serve(world, b'["push", "blog", "feat/3-runs"]', bundle(world["work"]).read())
    assert status != 0
    assert on_github(world, "feat/3-runs") is None
    github = GitHub(repo="octo/blog")
    status, _ = serve(world, b'["check"]', github=github, name="blog")
    assert status == 0
    assert github.calls == [("GET", "/repos/octo/blog", None)]


@pytest.mark.parametrize("uid", [0, 231072, 1000])
def test_serves_no_one_but_the_workbench(world: World, uid: int) -> None:
    commit(world["work"], "a.py", "Add a")
    github = GitHub()
    status, said = serve(world, b'["push", "feat/3-runs"]', bundle(world["work"]).read(), uid=uid, github=github)
    assert status == 1
    assert f"uid {uid} is not the workbench's" in said
    assert on_github(world, "feat/3-runs") is None
    assert github.calls == []


@pytest.mark.parametrize("request_line", [b"", b"push feat/3-runs", b'{"action": "push"}', b"[]", b'["push", 3]', b"[" * 5000])
def test_refuses_a_request_that_is_not_a_list_of_strings(world: World, request_line: bytes) -> None:
    status, said = serve(world, request_line)
    assert status != 0
    assert "refused" in said or "usage" in said


def test_serves_a_pr_with_its_text(world: World) -> None:
    github = GitHub()
    status, said = serve(world, b'["pr", "feat/3-runs"]', json.dumps({"title": "Adapt the runs"}).encode(), github=github)
    assert status == 0
    assert said == "https://github.com/octo/shop/pull/13"
    assert github.calls[-1][2]["title"] == "Adapt the runs"


def test_says_why_it_refused_on_the_socket(world: World) -> None:
    commit(world["work"], "a.py", "Add a", who=SOMEONE)
    status, said = serve(world, b'["push", "feat/3-runs"]', bundle(world["work"]).read())
    assert status == 1
    assert "author Someone Else" in said


def test_reads_the_peers_uid_from_a_real_socket() -> None:
    import os
    import socket

    a, b = socket.socketpair()
    with a, b:
        assert gate.peer_uid(a.fileno()) == os.getuid()


def test_reads_the_rest_after_refusing_early(world: World) -> None:
    """A refusal before the bundle is read still reads it, and drops it: a
    socket closed with input unread is reset, and the answer can go with it.
    """
    conn = io.BufferedReader(io.BytesIO(b'["push", "wip"]\n' + b"x" * (3 << 20)))
    out = io.StringIO()
    assert gate.serve("shop", str(world["path"]), conn, out, 2002, GitHub()) == 0
    assert out.getvalue().splitlines()[-1] == "status 1"
    assert conn.read() == b""


def test_refuses_a_commit_whose_real_author_hides_behind_a_second_header(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    work = world["work"]
    commit(work, "a.py", "Add a")
    tree = git(work, "rev-parse", "HEAD^{tree}").decode().strip()
    parent = git(work, "rev-parse", "HEAD^").decode().strip()
    raw = (
        f"tree {tree}\nparent {parent}\n"
        "author Claude <noreply@anthropic.com> 1700000000 +0000\n"
        "committer Claude <noreply@anthropic.com> 1700000000 +0000\n"
        f"author {ME[0]} <{ME[1]}> 1700000000 +0000\n"
        f"committer {ME[0]} <{ME[1]}> 1700000000 +0000\n"
        "\nAdd a\n"
    )
    sha = git(work, "hash-object", "-t", "commit", "-w", "--stdin", stdin=raw.encode()).decode().strip()
    git(work, "reset", "-q", "--hard", sha)
    assert run(world, ["push", "shop", "feat/3-runs"], bundle(work)) == 1
    assert on_github(world, "feat/3-runs") is None
    assert "author Claude" in capsys.readouterr().err


@pytest.mark.parametrize("name", [".bench/nötes.md", '.bench/a "quoted" note', ".bench/tab\there"])
def test_refuses_a_forbidden_path_git_would_quote(world: World, name: str, capsys: pytest.CaptureFixture[str]) -> None:
    work = world["work"]
    (work / ".bench").mkdir()
    (work / name).write_text("notes")
    git(work, "add", "-f", name)
    git(work, "commit", "-q", "-m", "Add notes")
    assert run(world, ["push", "shop", "feat/3-runs"], bundle(work)) == 1
    assert on_github(world, "feat/3-runs") is None
    assert ".bench/" in capsys.readouterr().err
