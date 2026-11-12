import subprocess
from pathlib import Path
from typing import Never

import job_clone
import pytest


def env(workspace: Path, related: str = "blog, car-taker") -> dict[str, str]:
    return {
        "CLAUDE_RELATED_REPOS": related,
        "GITHUB_REPOSITORY": "o/shop",
        "GITHUB_WORKSPACE": str(workspace),
        "BOT_NAME": "claude[bot]",
        "BOT_EMAIL": "1+claude[bot]@users.noreply.github.com",
    }


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    path = tmp_path / "shop" / "shop"
    path.mkdir(parents=True)
    return path


def test_the_list_takes_spaces_and_drops_what_is_no_name() -> None:
    assert job_clone.related({"CLAUDE_RELATED_REPOS": " a, b.c ,,../x,.., d e"}) == ["a", "b.c"]


def test_no_variable_means_no_related_repositories() -> None:
    assert job_clone.related({}) == []


@pytest.mark.parametrize("name", ["server", "..", "../blog", ""])
def test_a_name_off_the_list_is_refused_without_cloning(workspace: Path, name: str, capsys: pytest.CaptureFixture[str]) -> None:
    def run(*argv: str) -> Never:
        raise AssertionError(argv)

    assert job_clone.main([name], env(workspace), run=run) == 1
    assert "not one of" in capsys.readouterr().err


def test_it_clones_beside_the_checkout_and_says_where(workspace: Path, capsys: pytest.CaptureFixture[str]) -> None:
    calls: list[tuple[str, ...]] = []

    def run(*argv: str) -> str:
        calls.append(argv)
        return ""

    assert job_clone.main(["blog"], env(workspace), run=run) == 0

    (call,) = calls
    assert call[-2:] == ("https://github.com/o/blog.git", str(workspace.parent / "blog"))
    assert capsys.readouterr().out.strip() == str(workspace.parent / "blog")


def test_a_repositorys_commit_message_rules_hold_in_its_clone(workspace: Path) -> None:
    target = workspace.parent / "blog"
    calls: list[tuple[str, ...]] = []

    def run(*argv: str) -> str:
        calls.append(argv)
        target.mkdir()
        (target / ".pre-commit-config.yaml").touch()
        return ""

    def after_the_clone(*argv: str) -> str:
        if argv[0] == "prek":
            calls.append(argv)
            return ""
        return run(*argv)

    assert job_clone.main(["blog"], env(workspace), run=after_the_clone) == 0
    assert calls[-1] == ("prek", "--cd", str(target), "install", "--hook-type", "commit-msg")


def test_a_clone_already_there_is_left_alone(workspace: Path) -> None:
    (workspace.parent / "blog").mkdir()

    def run(*argv: str) -> Never:
        raise AssertionError(argv)

    assert job_clone.main(["blog"], env(workspace), run=run) == 0


def test_the_clone_commits_as_the_bot_and_asks_gh_for_credentials(workspace: Path, tmp_path: Path) -> None:
    origin = tmp_path / "origin"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)

    def run(*argv: str) -> str:
        subprocess.run([*argv[:-2], str(origin), argv[-1]], check=True, capture_output=True)
        return ""

    job_clone.main(["blog"], env(workspace), run=run)

    def config(*key: str) -> list[str]:
        return subprocess.run(
            ["git", "-C", str(workspace.parent / "blog"), "config", "--local", *key],
            check=True, capture_output=True, text=True,
        ).stdout.splitlines()

    assert config("--get-all", "credential.helper") == ["", "!gh auth git-credential"]
    assert config("user.name") == ["claude[bot]"]
    assert config("user.email") == ["1+claude[bot]@users.noreply.github.com"]


def test_no_arguments_is_a_usage_error(workspace: Path) -> None:
    assert job_clone.main([], env(workspace)) == 2
