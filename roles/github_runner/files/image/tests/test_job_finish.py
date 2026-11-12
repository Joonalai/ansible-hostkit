import json
import pathlib
import subprocess
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any, override

import job_finish
import pytest

PR_URL = "https://github.com/o/r/pull/7"


class Gh(list[tuple[str, ...]]):
    """Real git in the checkout, a recorded gh with an open PR or none."""

    def __init__(
        self,
        checkout: Path,
        pr: str = "",
        draft_ok: bool = True,
        body: str = "",
        draft: bool = True,
        comments: Iterable[str] = (),
        notes: Iterable[str] = (),
        graph: Iterable[dict[str, Any]] | None = (),
        firsts: Iterable[str] = (),
    ) -> None:
        super().__init__()
        self.firsts = list(firsts)
        self.comments, self.notes, self.graph = list(comments), list(notes), None if graph is None else list(graph)
        self.checkout, self.pr, self.draft_ok, self.body, self.draft = checkout, pr, draft_ok, body, draft

    def __call__(self, *argv: str) -> str:  # noqa: C901, PLR0911
        if argv[0] == "git":
            return subprocess.run(
                ["git", "-C", str(self.checkout), *argv[1:]], check=True, capture_output=True, text=True,
            ).stdout.strip()
        self.append(argv)
        if argv[1:3] == ("pr", "list") and "--search" in argv:
            return "\n".join(self.firsts)
        if argv[1:3] == ("pr", "list"):
            return self.pr
        if argv[1:3] == ("api", "graphql"):
            if "--paginate" not in argv:
                return ""
            if self.graph is None:
                raise subprocess.CalledProcessError(1, argv, stderr="HTTP 502")
            return "\n".join(json.dumps(node) for node in self.graph)
        if argv[1] == "api" and argv[2].endswith("/comments"):
            return "\n".join(self.comments)
        if argv[1] == "api" and argv[2].endswith("/jobs"):
            return json.dumps({"jobs": [{"id": 5, "runner_name": "other"}, {"id": 6, "runner_name": "slot-1"}]})
        if argv[1] == "api" and argv[2].endswith("/annotations"):
            return json.dumps([{"message": note} for note in self.notes])
        if argv[1:3] == ("pr", "view"):
            return json.dumps({"body": self.body, "isDraft": self.draft})
        if argv[1:3] == ("pr", "create"):
            if "--draft" in argv and not self.draft_ok:
                raise subprocess.CalledProcessError(1, argv)
            return PR_URL
        return ""

    def called(self, *prefix: str) -> list[tuple[str, ...]]:
        return [call for call in self if call[: len(prefix)] == prefix]


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    def git(*argv: str, cwd: Path = tmp_path / "work") -> None:
        subprocess.run(["git", *argv], cwd=cwd, check=True, capture_output=True)

    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(tmp_path / "origin")], check=True)
    subprocess.run(["git", "clone", "-q", str(tmp_path / "origin"), str(tmp_path / "work")], check=True)
    git("-c", "user.name=u", "-c", "user.email=u@x", "commit", "-q", "--allow-empty", "-m", "start")
    git("push", "-q", "origin", "main")
    return tmp_path / "work"


def env(outcome: str = "failure") -> dict[str, str]:
    return {
        "OUTCOME": outcome,
        "NUMBER": "3",
        "DEFAULT_BRANCH": "main",
        "LEVEL": "claude:full",
        "RUN_URL": "https://github.com/o/r/actions/runs/9",
        "BOT_NAME": "claude[bot]",
        "BOT_EMAIL": "1+claude[bot]@users.noreply.github.com",
        "GITHUB_RUN_ID": "9",
    }


def branches(checkout: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(checkout), "ls-remote", "--heads", "origin"], check=False, capture_output=True, text=True,
    ).stdout


def test_work_left_on_main_goes_to_a_wip_branch_and_a_draft(checkout: Path) -> None:
    (checkout / "new.py").write_text("x = 1\n")
    gh = Gh(checkout)

    assert job_finish.main(env(), run=gh) == 0

    assert "refs/heads/wip/3-interrupted-9" in branches(checkout)
    assert branches(checkout).count("refs/heads/main") == 1
    (create,) = gh.called("gh", "pr", "create")
    assert "--draft" in create
    assert create[create.index("--body") + 1].startswith("Closes #3\n")
    assert create[create.index("--label") + 1] == "claude:full"
    assert gh.called("gh", "pr", "comment", PR_URL)
    assert not gh.called("gh", "issue")


def test_work_left_on_an_issue_done_in_parts_opens_a_draft_that_closes_nothing(checkout: Path) -> None:
    (checkout / "new.py").write_text("x = 1\n")
    gh = Gh(checkout, firsts=["Part 1/3 of #3", "Part 2/3 of #31", "Closes #4"])

    assert job_finish.main(env(), run=gh) == 0

    (create,) = gh.called("gh", "pr", "create")
    text = create[create.index("--body") + 1]
    assert text.startswith("Part of #3\n")
    assert "Closes" not in text


@pytest.mark.parametrize("firsts", [["Part 2/3 of #31", "Closes #3"], ["See Part 1/3 of #3"], []])
def test_a_draft_closes_an_issue_no_part_names(checkout: Path, firsts: list[str]) -> None:
    (checkout / "new.py").write_text("x = 1\n")
    gh = Gh(checkout, firsts=firsts)

    job_finish.main(env(), run=gh)

    (create,) = gh.called("gh", "pr", "create")
    assert create[create.index("--body") + 1].startswith("Closes #3\n")


def test_without_drafts_the_pr_is_marked_wip_in_its_title(checkout: Path) -> None:
    (checkout / "new.py").write_text("x = 1\n")
    gh = Gh(checkout, draft_ok=False)

    job_finish.main(env(), run=gh)

    create = gh.called("gh", "pr", "create")[-1]
    assert create[create.index("--title") + 1] == "[WIP] WIP: #3, interrupted"


def test_nothing_changed_with_a_pr_notes_it_on_the_pr(checkout: Path) -> None:
    gh = Gh(checkout, pr=PR_URL)

    job_finish.main(env(), run=gh)

    assert gh.called("gh", "pr", "comment", PR_URL)
    assert not gh.called("gh", "issue")
    assert not gh.called("gh", "pr", "create")


def test_nothing_changed_without_a_pr_notes_it_on_the_issue(checkout: Path) -> None:
    gh = Gh(checkout)

    job_finish.main(env(), run=gh)

    (comment,) = gh.called("gh", "issue", "comment", "3")
    assert "before changing anything" in comment[-1]


def test_a_successful_job_that_pushed_everything_is_left_alone(checkout: Path) -> None:
    gh = Gh(checkout, pr=PR_URL)

    assert job_finish.main(env("success"), run=gh) == 0

    assert gh.called("gh", "pr", "list")
    assert not gh.called("gh", "pr", "edit")
    assert not gh.called("gh", "pr", "comment")


def test_an_existing_pr_gets_the_leftovers_pushed_to_its_branch(checkout: Path) -> None:
    subprocess.run(["git", "-C", str(checkout), "switch", "-q", "-c", "feat/3-thing"], check=True)
    (checkout / "new.py").write_text("x = 1\n")
    gh = Gh(checkout, pr=PR_URL)

    job_finish.main(env("success"), run=gh)

    assert "refs/heads/feat/3-thing" in branches(checkout)
    assert not gh.called("gh", "pr", "create")
    assert not gh.called("gh", "pr", "comment")


def test_a_ready_pr_goes_back_to_a_draft_before_the_leftovers_are_pushed(checkout: Path) -> None:
    subprocess.run(["git", "-C", str(checkout), "switch", "-q", "-c", "feat/3-thing"], check=True)
    (checkout / "new.py").write_text("x = 1\n")
    gh = Gh(checkout, pr=PR_URL, draft=False)

    job_finish.main(env("failure"), run=gh)

    assert gh.called("gh", "pr", "ready", PR_URL, "--undo")
    assert "refs/heads/feat/3-thing" in branches(checkout)


def test_a_ready_pr_whose_branch_github_already_has_stays_ready(checkout: Path) -> None:
    subprocess.run(["git", "-C", str(checkout), "switch", "-q", "-c", "feat/3-thing"], check=True)
    (checkout / "new.py").write_text("x = 1\n")
    subprocess.run(["git", "-C", str(checkout), "add", "new.py"], check=True)
    subprocess.run(
        ["git", "-C", str(checkout), "-c", "user.name=u", "-c", "user.email=u@x", "commit", "-q", "-m", "new"], check=True,
    )
    subprocess.run(["git", "-C", str(checkout), "push", "-q", "origin", "feat/3-thing"], check=True)
    gh = Gh(checkout, pr=PR_URL, body="Level: claude · Status: done\n", draft=False)

    assert job_finish.main(env("success"), run=gh) == 0

    assert not gh.called("gh", "pr", "ready")


def test_a_draft_is_not_asked_to_become_one(checkout: Path) -> None:
    subprocess.run(["git", "-C", str(checkout), "switch", "-q", "-c", "feat/3-thing"], check=True)
    (checkout / "new.py").write_text("x = 1\n")
    gh = Gh(checkout, pr=PR_URL)

    job_finish.main(env("failure"), run=gh)

    assert not gh.called("gh", "pr", "ready")


def test_a_draft_it_cannot_make_still_gets_the_work_pushed(checkout: Path, capsys: pytest.CaptureFixture[str]) -> None:
    subprocess.run(["git", "-C", str(checkout), "switch", "-q", "-c", "feat/3-thing"], check=True)
    (checkout / "new.py").write_text("x = 1\n")

    class Refusing(Gh):
        @override
        def __call__(self, *argv: str) -> str:
            if argv[1:3] == ("pr", "ready"):
                self.append(argv)
                raise subprocess.CalledProcessError(1, argv, stderr="no permission")
            return super().__call__(*argv)

    gh = Refusing(checkout, pr=PR_URL, draft=False)

    assert job_finish.main(env("failure"), run=gh) == 0
    assert "refs/heads/feat/3-thing" in branches(checkout)
    assert "Could not make" in capsys.readouterr().out


@pytest.fixture
def sibling(checkout: Path, tmp_path: Path) -> Path:
    """A related repository cloned beside the checkout, as claude-job-clone leaves it."""
    origin = tmp_path / "blog-origin"
    path = checkout.parent / "blog"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    subprocess.run(["git", "clone", "-q", str(origin), str(path)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.name=u", "-c", "user.email=u@x", "commit", "-q", "--allow-empty", "-m", "start"],
        cwd=path, check=True,
    )
    subprocess.run(["git", "push", "-q", "origin", "main"], cwd=path, check=True)
    subprocess.run(["git", "remote", "set-head", "origin", "main"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "bot"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "bot@x"], cwd=path, check=True)
    return path


def related_env(checkout: Path, outcome: str = "failure") -> dict[str, str]:
    return {
        **env(outcome),
        "CLAUDE_RELATED_REPOS": "blog, car-taker",
        "GITHUB_REPOSITORY": "o/shop",
        "GITHUB_WORKSPACE": str(checkout),
    }


def test_no_related_repositories_need_no_workspace() -> None:
    assert job_finish.siblings({}) == []
    assert job_finish.siblings({"CLAUDE_RELATED_REPOS": ""}) == []


def test_only_the_repositories_the_job_cloned_are_walked(checkout: Path, sibling: Path) -> None:
    assert job_finish.siblings(related_env(checkout)) == [("blog", str(sibling))]


def test_work_left_in_a_sibling_gets_its_own_pr_that_closes_nothing(checkout: Path, sibling: Path) -> None:
    (sibling / "new.py").write_text("x = 1\n")
    gh = Gh(checkout)

    assert job_finish.main(related_env(checkout), run=gh) == 0

    assert "refs/heads/wip/shop-3-interrupted-9" in branches(sibling)
    assert branches(sibling).count("refs/heads/main") == 1
    (create,) = gh.called("gh", "pr", "create", "-R", "o/blog")
    assert "--draft" in create
    assert create[create.index("--base") + 1] == "main"
    assert create[create.index("--label") + 1] == "claude:full"
    text = create[create.index("--body") + 1]
    assert text.startswith("Part of o/shop#3.")
    assert "Closes" not in text
    (note,) = gh.called("gh", "issue", "comment", "3")[-1:]
    assert PR_URL in note[-1]


def test_a_sibling_branch_keeps_its_name(checkout: Path, sibling: Path) -> None:
    subprocess.run(["git", "switch", "-q", "-c", "feat/shop-3-login"], cwd=sibling, check=True)
    (sibling / "new.py").write_text("x = 1\n")
    gh = Gh(checkout, pr=PR_URL)

    assert job_finish.main(related_env(checkout), run=gh) == 0

    assert "refs/heads/feat/shop-3-login" in branches(sibling)
    assert not gh.called("gh", "pr", "create")


def test_a_siblings_ready_pr_github_already_has_stays_ready(checkout: Path, sibling: Path) -> None:
    subprocess.run(["git", "switch", "-q", "-c", "feat/shop-3-login"], cwd=sibling, check=True)
    (sibling / "new.py").write_text("x = 1\n")
    subprocess.run(["git", "add", "new.py"], cwd=sibling, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "new"], cwd=sibling, check=True)
    subprocess.run(["git", "push", "-q", "origin", "feat/shop-3-login"], cwd=sibling, check=True)
    gh = Gh(checkout, pr=PR_URL, draft=False)

    assert job_finish.main(related_env(checkout, "success"), run=gh) == 0

    assert not gh.called("gh", "pr", "ready")


def test_a_siblings_ready_pr_goes_back_to_a_draft_before_its_push(checkout: Path, sibling: Path) -> None:
    subprocess.run(["git", "switch", "-q", "-c", "feat/shop-3-login"], cwd=sibling, check=True)
    (sibling / "new.py").write_text("x = 1\n")
    gh = Gh(checkout, pr=PR_URL, draft=False)

    assert job_finish.main(related_env(checkout), run=gh) == 0

    assert gh.called("gh", "pr", "ready", PR_URL, "--undo")
    assert "refs/heads/feat/shop-3-login" in branches(sibling)


def test_a_sibling_left_clean_is_not_touched(checkout: Path, sibling: Path) -> None:
    gh = Gh(checkout)

    assert job_finish.main(related_env(checkout), run=gh) == 0

    assert not gh.called("gh", "pr", "create")
    assert branches(sibling).count("refs/heads/") == 1


def test_a_sibling_needs_no_note_when_claude_finished(checkout: Path, sibling: Path) -> None:
    (sibling / "new.py").write_text("x = 1\n")
    gh = Gh(checkout)

    assert job_finish.main(related_env(checkout, "success"), run=gh) == 0

    assert gh.called("gh", "pr", "create", "-R", "o/blog")
    assert not gh.called("gh", "issue", "comment")


def test_a_sibling_that_fails_stops_no_other_and_fails_the_step(checkout: Path, sibling: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (checkout / "new.py").write_text("x = 1\n")
    (sibling / "new.py").write_text("x = 1\n")
    subprocess.run(["git", "remote", "set-url", "origin", "/nonexistent"], cwd=sibling, check=True)
    gh = Gh(checkout)

    assert job_finish.main(related_env(checkout), run=gh) == 1

    assert "refs/heads/wip/3-interrupted-9" in branches(checkout)
    assert "Could not save the work in blog" in capsys.readouterr().out


def test_a_failed_checkout_still_saves_the_siblings(checkout: Path, sibling: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (checkout / "new.py").write_text("x = 1\n")
    (sibling / "new.py").write_text("x = 1\n")
    subprocess.run(["git", "remote", "set-url", "origin", "/nonexistent"], cwd=checkout, check=True)
    gh = Gh(checkout)

    assert job_finish.main(related_env(checkout), run=gh) == 1

    assert "refs/heads/wip/shop-3-interrupted-9" in branches(sibling)
    assert "Could not save the checkout" in capsys.readouterr().out


@pytest.fixture
def pr_checkout(checkout: Path) -> Path:
    """On a PR's branch, with the action's restore of CLAUDE.md from main done."""

    def git(*argv: str) -> None:
        subprocess.run(
            ["git", "-c", "user.name=u", "-c", "user.email=u@x", *argv], cwd=checkout, check=True, capture_output=True,
        )

    (checkout / "CLAUDE.md").write_text("main's\n")
    (checkout / "code.py").write_text("x = 1\n")
    git("add", "-A")
    git("commit", "-q", "-m", "main")
    git("push", "-q", "origin", "main")
    git("switch", "-q", "-c", "feat/x")
    (checkout / "CLAUDE.md").write_text("the PR's\n")
    (checkout / "CLAUDE.local.md").write_text("only on the PR\n")
    git("add", "-A")
    git("commit", "-q", "-m", "the PR")
    git("push", "-q", "origin", "feat/x")
    git("checkout", "origin/main", "--", "CLAUDE.md")
    git("rm", "-q", "CLAUDE.local.md")
    return checkout


def event_file(tmp_path: Path, event: dict[str, Any]) -> str:
    path = tmp_path / "event.json"
    path.write_text(json.dumps(event))
    return str(path)


def show(checkout: Path, ref: str, path: str) -> str:
    return subprocess.run(
        ["git", "-C", str(checkout), "show", f"{ref}:{path}"], capture_output=True, text=True, check=True,
    ).stdout


@pytest.mark.parametrize(
    "event", [{"issue": {"pull_request": {"url": "u"}}}, {"pull_request": {"number": 3}}], ids=["comment", "review"],
)
def test_a_pr_job_that_changed_nothing_commits_nothing(pr_checkout: Path, tmp_path: Path, event: dict[str, Any]) -> None:
    before = branches(pr_checkout)

    assert job_finish.main({**env("success"), "GITHUB_EVENT_PATH": event_file(tmp_path, event)}, run=Gh(pr_checkout, pr=PR_URL)) == 0

    assert branches(pr_checkout) == before


def test_a_pr_job_saves_its_work_without_what_the_action_restored(pr_checkout: Path, tmp_path: Path) -> None:
    (pr_checkout / "code.py").write_text("x = 2\n")
    path = event_file(tmp_path, {"issue": {"pull_request": {"url": "u"}}})

    assert job_finish.main({**env(), "GITHUB_EVENT_PATH": path}, run=Gh(pr_checkout, pr=PR_URL)) == 0

    assert show(pr_checkout, "origin/feat/x", "code.py") == "x = 2\n"
    assert show(pr_checkout, "origin/feat/x", "CLAUDE.md") == "the PR's\n"
    assert show(pr_checkout, "origin/feat/x", "CLAUDE.local.md") == "only on the PR\n"


def test_an_issue_job_saves_its_change_to_claude_md(checkout: Path, tmp_path: Path) -> None:
    (checkout / "CLAUDE.md").write_text("the job's\n")
    path = event_file(tmp_path, {"issue": {"number": 3}})

    assert job_finish.main({**env(), "GITHUB_EVENT_PATH": path}, run=Gh(checkout)) == 0

    assert show(checkout, "origin/wip/3-interrupted-9", "CLAUDE.md") == "the job's\n"


def test_saving_what_was_left_skips_the_repositorys_commit_message_rules() -> None:
    source = (pathlib.Path(job_finish.__file__)).read_text()
    assert source.count('"commit", "--no-verify"') == 2


def other_push(checkout: Path, tmp_path: Path, branch: str, name: str = "theirs.py") -> None:
    """Someone else moves the branch on GitHub while the job works."""
    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", "-b", branch, str(tmp_path / "origin"), str(other)], check=True)
    (other / name).write_text("y = 2\n")
    subprocess.run(["git", "-C", str(other), "add", name], check=True)
    subprocess.run(["git", "-C", str(other), "-c", "user.name=o", "-c", "user.email=o@x", "commit", "-q", "-m", "theirs"], check=True)
    subprocess.run(["git", "-C", str(other), "push", "-q", "origin", branch], check=True)


def on_branch(checkout: Path, branch: str) -> None:
    subprocess.run(["git", "-C", str(checkout), "switch", "-q", "-c", branch], check=True)
    subprocess.run(["git", "-C", str(checkout), "push", "-q", "origin", branch], check=True)


def test_a_branch_that_moved_meanwhile_keeps_both_and_the_pr_says_where(checkout: Path, tmp_path: Path) -> None:
    on_branch(checkout, "feat/3-thing")
    other_push(checkout, tmp_path, "feat/3-thing")
    (checkout / "mine.py").write_text("x = 1\n")
    gh = Gh(checkout, pr=PR_URL)

    assert job_finish.main(env(), run=gh) == 0

    heads = branches(checkout)
    assert "refs/heads/feat/3-thing-job-9" in heads
    theirs = subprocess.run(
        ["git", "-C", str(checkout), "ls-remote", "origin", "feat/3-thing"], check=False, capture_output=True, text=True,
    ).stdout
    mine = subprocess.run(["git", "-C", str(checkout), "rev-parse", "HEAD"], check=False, capture_output=True, text=True).stdout
    assert mine.strip() not in theirs
    (comment,) = gh.called("gh", "pr", "comment", PR_URL)
    assert "feat/3-thing-job-9" in comment[-1]
    assert not gh.called("gh", "pr", "create")


def test_a_branch_that_already_has_the_work_needs_no_rescue(checkout: Path, tmp_path: Path) -> None:
    on_branch(checkout, "feat/3-thing")
    other_push(checkout, tmp_path, "feat/3-thing")
    gh = Gh(checkout, pr=PR_URL)

    assert job_finish.push(lambda *argv: gh("git", *argv), "feat/3-thing", "9") == ""

    assert "feat/3-thing-job-9" not in branches(checkout)


def git_in(checkout: Path, *argv: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(checkout), "-c", "user.name=u", "-c", "user.email=u@x", *argv],
        check=False, capture_output=True, text=True,
    )


def stopped_midway_in_a_rebase(checkout: Path, push_branch: bool = True) -> str:
    """feat/3-thing with two commits, the first of which conflicts with
    what main moved on to, and a rebase onto main stopped at that one.
    """
    git_in(checkout, "switch", "-q", "-c", "feat/3-thing")
    (checkout / "f.txt").write_text("mine\n")
    git_in(checkout, "add", "f.txt")
    git_in(checkout, "commit", "-q", "-m", "mine")
    (checkout / "later.txt").write_text("later\n")
    git_in(checkout, "add", "later.txt")
    git_in(checkout, "commit", "-q", "-m", "later")
    if push_branch:
        git_in(checkout, "push", "-q", "origin", "feat/3-thing")
    git_in(checkout, "switch", "-q", "main")
    (checkout / "f.txt").write_text("main\n")
    git_in(checkout, "add", "f.txt")
    git_in(checkout, "commit", "-q", "-m", "main's")
    git_in(checkout, "push", "-q", "origin", "main")
    git_in(checkout, "switch", "-q", "feat/3-thing")
    assert git_in(checkout, "rebase", "main").returncode != 0
    (checkout / "f.txt").write_text("both\n")
    return git_in(checkout, "rev-parse", "origin/feat/3-thing").stdout.strip()


def test_a_rebase_stopped_midway_is_saved_beside_its_branch_never_over_it(checkout: Path) -> None:
    before = stopped_midway_in_a_rebase(checkout)
    gh = Gh(checkout, pr=PR_URL)

    assert job_finish.main(env(), run=gh) == 0

    heads = branches(checkout)
    assert "refs/heads/HEAD" not in heads
    assert f"{before}\trefs/heads/feat/3-thing" in heads
    assert "refs/heads/feat/3-thing-job-9" in heads
    (list_call,) = gh.called("gh", "pr", "list")
    assert list_call[list_call.index("--head") + 1] == "feat/3-thing"
    (comment,) = gh.called("gh", "pr", "comment", PR_URL)
    assert "midway through a rebase of `feat/3-thing`" in comment[-1]
    assert not gh.called("gh", "pr", "create")


def test_a_rebase_stopped_midway_on_a_branch_github_lacks_pushes_it(checkout: Path) -> None:
    stopped_midway_in_a_rebase(checkout, push_branch=False)
    gh = Gh(checkout)

    assert job_finish.main(env(), run=gh) == 0

    heads = branches(checkout)
    assert "refs/heads/feat/3-thing" in heads
    assert "refs/heads/HEAD" not in heads
    (create,) = gh.called("gh", "pr", "create")
    assert create[create.index("--head") + 1] == "feat/3-thing"


def test_work_on_a_detached_head_goes_to_a_wip_branch(checkout: Path) -> None:
    git_in(checkout, "switch", "-q", "--detach")
    (checkout / "new.py").write_text("x = 1\n")
    gh = Gh(checkout)

    assert job_finish.main(env(), run=gh) == 0

    heads = branches(checkout)
    assert "refs/heads/wip/3-interrupted-9" in heads
    assert "refs/heads/HEAD" not in heads


def test_a_sibling_stopped_midway_in_a_rebase_is_saved_beside_its_branch(checkout: Path, sibling: Path) -> None:
    before = stopped_midway_in_a_rebase(sibling)
    gh = Gh(checkout, pr=PR_URL)

    assert job_finish.main(related_env(checkout), run=gh) == 0

    heads = branches(sibling)
    assert "refs/heads/HEAD" not in heads
    assert f"{before}\trefs/heads/feat/3-thing" in heads
    assert "refs/heads/feat/3-thing-job-9" in heads


def amend(checkout: Path) -> None:
    (checkout / "mine.py").write_text("x = 1\n")
    subprocess.run(["git", "-C", str(checkout), "add", "mine.py"], check=True)
    subprocess.run(
        ["git", "-C", str(checkout), "-c", "user.name=u", "-c", "user.email=u@x", "commit", "-q", "--amend", "--no-edit"],
        check=True,
    )


def test_what_the_job_rewrote_after_its_own_push_goes_over_it(checkout: Path) -> None:
    subprocess.run(["git", "-C", str(checkout), "switch", "-q", "-c", "feat/3-thing"], check=True)
    subprocess.run(
        ["git", "-C", str(checkout), "-c", "user.name=u", "-c", "user.email=u@x", "commit", "-q", "--allow-empty", "-m", "one"],
        check=True,
    )
    subprocess.run(["git", "-C", str(checkout), "push", "-q", "origin", "feat/3-thing"], check=True)
    amend(checkout)
    gh = Gh(checkout, pr=PR_URL)

    assert job_finish.push(lambda *argv: gh("git", *argv), "feat/3-thing", "9") == "feat/3-thing"

    heads = branches(checkout)
    assert "feat/3-thing-job-9" not in heads
    mine = gh("git", "rev-parse", "HEAD")
    assert f"{mine}\trefs/heads/feat/3-thing" in heads


def test_a_branch_moved_after_the_jobs_own_push_is_not_pushed_over(checkout: Path, tmp_path: Path) -> None:
    on_branch(checkout, "feat/3-thing")
    other_push(checkout, tmp_path, "feat/3-thing")
    subprocess.run(["git", "-C", str(checkout), "fetch", "-q", "origin"], check=True)
    amend(checkout)
    gh = Gh(checkout, pr=PR_URL)

    assert job_finish.push(lambda *argv: gh("git", *argv), "feat/3-thing", "9") == "feat/3-thing-job-9"


def test_a_progress_left_working_reads_stopped_when_the_time_ran_out(checkout: Path) -> None:
    gh = Gh(checkout, pr=PR_URL, body="Closes #3\n\nLevel: claude:full · Status: working, reviewer next\nNext: tester")

    job_finish.main(env("success"), run=gh)

    (edit,) = gh.called("gh", "pr", "edit", PR_URL)
    new = edit[edit.index("--body") + 1]
    assert "Status: stopped: the job's time ran out\n" in new
    assert "Next: tester" in new
    (comment,) = gh.called("gh", "pr", "comment", PR_URL)
    assert "@claude continue" in comment[-1]


def test_a_progress_left_working_names_what_cut_the_job_off(checkout: Path) -> None:
    gh = Gh(checkout, pr=PR_URL, body="Level: claude · Status: working\n")

    job_finish.main(env("cancelled"), run=gh)

    (edit,) = gh.called("gh", "pr", "edit", PR_URL)
    assert "Status: stopped: cancelled" in edit[edit.index("--body") + 1]


def test_a_done_progress_is_left_as_it_is(checkout: Path) -> None:
    gh = Gh(checkout, pr=PR_URL, body="Level: claude · Status: done\n")

    job_finish.main(env("success"), run=gh)

    assert not gh.called("gh", "pr", "edit")


def test_a_pr_marked_ready_is_done_whatever_progress_still_says(checkout: Path) -> None:
    gh = Gh(checkout, pr=PR_URL, body="Level: claude · Status: working\n", draft=False)

    job_finish.main(env("success"), run=gh)

    assert not gh.called("gh", "pr", "edit")
    assert not gh.called("gh", "pr", "comment")
    assert not gh.called("gh", "pr", "ready")


ALLOWED, BOT = "11", "99"
WORKING_BODY = "Closes #3\n\nLevel: claude:full · Status: working\nNext: tester"


def going_on(outcome: str = "success", minutes_ago: int = 90, minutes: int = 100) -> dict[str, str]:
    return {
        **env(outcome),
        "STARTED": str(int(time.time()) - minutes_ago * 60),
        "MINUTES": str(minutes),
        "CLAUDE_ALLOWED_IDS": f"[{ALLOWED}]",
        "CLAUDE_APP_BOT_ID": BOT,
        "GITHUB_REPOSITORY": "o/r",
    }


def auto(n: int = 1) -> list[str]:
    return [f"{BOT} true"] * n


@pytest.mark.parametrize("outcome", ["success", "failure"])
def test_work_left_working_at_the_jobs_time_goes_on_by_itself(checkout: Path, outcome: str) -> None:
    gh = Gh(checkout, pr=PR_URL, body=WORKING_BODY, comments=[f"{ALLOWED} false", f"{BOT} false"])

    job_finish.main(going_on(outcome), run=gh)

    assert not gh.called("gh", "pr", "edit")
    note = gh.called("gh", "pr", "comment", PR_URL)[-1][-1]
    assert note.startswith("@claude continue\n")
    assert job_finish.AUTO in note
    assert "1 of 4" in note
    (listed,) = gh.called("gh", "api", "repos/o/r/issues/7/comments")
    assert "--paginate" in listed


def test_the_rounds_count_since_someone_allowed_last_commented(checkout: Path) -> None:
    comments = [*auto(4), f"{ALLOWED} false", *auto(3)]
    gh = Gh(checkout, pr=PR_URL, body=WORKING_BODY, comments=comments)

    job_finish.main(going_on(), run=gh)

    assert "4 of 4" in gh.called("gh", "pr", "comment", PR_URL)[-1][-1]


def test_after_its_rounds_the_work_waits_for_someone(checkout: Path) -> None:
    gh = Gh(checkout, pr=PR_URL, body=WORKING_BODY, comments=[f"{ALLOWED} false", *auto(4)])

    job_finish.main(going_on(), run=gh)

    (edit,) = gh.called("gh", "pr", "edit", PR_URL)
    assert "Status: stopped: not done after 4 jobs in a row" in edit[edit.index("--body") + 1]
    (note,) = gh.called("gh", "pr", "comment", PR_URL)
    assert job_finish.AUTO not in note[-1]
    assert "parts" in note[-1] and "removing the working labels" in note[-1]


def test_a_failed_job_after_its_rounds_says_only_what_the_user_can_do(checkout: Path) -> None:
    (checkout / "new.py").write_text("x = 1\n")
    subprocess.run(["git", "-C", str(checkout), "switch", "-q", "-c", "feat/3-thing"], check=True)
    gh = Gh(checkout, pr=PR_URL, body=WORKING_BODY, comments=[f"{ALLOWED} false", *auto(4)])

    job_finish.main(going_on("failure"), run=gh)

    (note,) = gh.called("gh", "pr", "comment", PR_URL)
    assert note[-1].startswith("Stopped with work left: the work went on by itself 4 times")


def test_anothers_comment_does_not_reset_the_rounds(checkout: Path) -> None:
    gh = Gh(checkout, pr=PR_URL, body=WORKING_BODY, comments=[*auto(3), "12345 false", *auto(1)])

    job_finish.main(going_on(), run=gh)

    assert gh.called("gh", "pr", "edit", PR_URL)


def test_a_last_part_cut_off_at_its_limit_goes_on_by_itself(checkout: Path, tmp_path: Path) -> None:
    settings = {**going_on("cancelled", minutes_ago=110, minutes=120), "RUNNER_TEMP": str(tmp_path)}
    (tmp_path / job_finish.ENDS).write_text(f"{time.time() - 20}\n")
    gh = Gh(checkout, pr=PR_URL, body=WORKING_BODY)

    job_finish.main(settings, run=gh)

    assert any(job_finish.AUTO in call[-1] for call in gh.called("gh", "pr", "comment"))


def test_a_first_part_cut_off_at_its_limit_goes_on_by_itself(checkout: Path) -> None:
    gh = Gh(checkout, pr=PR_URL, body=WORKING_BODY)

    job_finish.main(going_on("cancelled", minutes_ago=52, minutes=60), run=gh)

    assert any(job_finish.AUTO in call[-1] for call in gh.called("gh", "pr", "comment"))


def test_a_run_cancelled_mid_part_does_not_go_on(checkout: Path, tmp_path: Path) -> None:
    settings = {**going_on("cancelled", minutes_ago=80, minutes=120), "RUNNER_TEMP": str(tmp_path), "RUNNER_NAME": "slot-1"}
    (tmp_path / job_finish.ENDS).write_text(f"{time.time() + 20 * 60}\n")
    gh = Gh(checkout, pr=PR_URL, body=WORKING_BODY, notes=["The run was canceled by @someone."])

    job_finish.main(settings, run=gh)

    assert all(job_finish.AUTO not in call[-1] for call in gh.called("gh", "pr", "comment"))


LOST_NOTE = (
    "The self-hosted runner lost communication with the server. Verify the machine is running and has a healthy "
    "network connection."
)


def test_a_run_github_lost_its_runner_in_goes_on_by_itself(checkout: Path, tmp_path: Path) -> None:
    settings = {
        **going_on("cancelled", minutes_ago=80, minutes=120),
        "RUNNER_TEMP": str(tmp_path), "GITHUB_RUN_ID": "9", "GITHUB_RUN_ATTEMPT": "2", "RUNNER_NAME": "slot-1",
    }
    (tmp_path / job_finish.ENDS).write_text(f"{time.time() + 20 * 60}\n")
    gh = Gh(checkout, pr=PR_URL, body=WORKING_BODY, notes=["Node.js 20 is deprecated.", LOST_NOTE])

    job_finish.main(settings, run=gh)

    assert any(job_finish.AUTO in call[-1] for call in gh.called("gh", "pr", "comment"))
    assert gh.called("gh", "api", "repos/o/r/actions/runs/9/attempts/2/jobs")
    assert gh.called("gh", "api", "repos/o/r/check-runs/6/annotations")


def test_a_runner_it_cannot_find_among_the_jobs_is_not_lost(checkout: Path, tmp_path: Path) -> None:
    settings = {**going_on("cancelled", minutes_ago=80, minutes=120), "RUNNER_TEMP": str(tmp_path), "RUNNER_NAME": "gone"}
    (tmp_path / job_finish.ENDS).write_text(f"{time.time() + 20 * 60}\n")
    gh = Gh(checkout, pr=PR_URL, body=WORKING_BODY, notes=[LOST_NOTE])

    job_finish.main(settings, run=gh)

    assert all(job_finish.AUTO not in call[-1] for call in gh.called("gh", "pr", "comment"))
    assert not gh.called("gh", "api", "repos/o/r/check-runs/6/annotations")


@pytest.mark.parametrize(
    "why",
    ["early", "cancelled", "done", "ready", "unset"],
)
def test_the_work_does_not_go_on_by_itself(checkout: Path, why: str) -> None:
    settings = going_on("cancelled" if why == "cancelled" else "failure", minutes_ago=20 if why == "early" else 90)
    if why == "unset":
        del settings["STARTED"]
    body = WORKING_BODY.replace("working", "done") if why == "done" else WORKING_BODY
    gh = Gh(checkout, pr=PR_URL, body=body, draft=why != "ready")

    job_finish.main(settings, run=gh)

    assert all(job_finish.AUTO not in call[-1] for call in gh.called("gh", "pr", "comment"))


def test_a_count_that_fails_leaves_the_work_waiting(checkout: Path) -> None:
    class Failing(Gh):
        @override
        def __call__(self, *argv: str) -> str:
            if argv[1] == "api":
                raise subprocess.CalledProcessError(1, argv)
            return super().__call__(*argv)

    gh = Failing(checkout, pr=PR_URL, body=WORKING_BODY)

    assert job_finish.main(going_on(), run=gh) == 0

    assert gh.called("gh", "pr", "edit", PR_URL)


def test_a_lead_that_ended_well_early_with_work_left_goes_on(checkout: Path) -> None:
    gh = Gh(checkout, pr=PR_URL, body=WORKING_BODY)

    job_finish.main(going_on("success", minutes_ago=23), run=gh)

    assert job_finish.AUTO in gh.called("gh", "pr", "comment", PR_URL)[-1][-1]


def test_a_job_stopped_at_the_usage_limit_stays_working_for_its_restart(checkout: Path) -> None:
    gh = Gh(checkout, pr=PR_URL, body=WORKING_BODY)

    job_finish.main({**going_on("failure", minutes_ago=5), "WAITING": "1762621200"}, run=gh)

    assert gh.called("gh", "pr", "comment") == []
    assert gh.called("gh", "pr", "edit") == []


def test_a_job_stopped_at_the_usage_limit_with_nothing_done_leaves_the_note_to_its_restart(checkout: Path) -> None:
    gh = Gh(checkout)

    job_finish.main({**env("failure"), "WAITING": "1762621200"}, run=gh)

    assert gh.called("gh", "issue", "comment") == []


def node(
    ident: str, body: str, minutes_ago: int = 60, login: str = "claude", kind: str = "Bot", minimized: bool = False,
) -> dict[str, Any]:
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - minutes_ago * 60))
    return {"id": ident, "createdAt": stamp, "isMinimized": minimized, "body": body, "author": {"__typename": kind, "login": login}}


def hidden(gh: Gh) -> list[str]:
    return [call[-1].removeprefix("id=") for call in gh.called("gh", "api", "graphql") if "--paginate" not in call]


def test_the_bots_outdated_comments_from_earlier_jobs_are_hidden(checkout: Path, capsys: pytest.CaptureFixture[str]) -> None:
    graph = [
        node("stopped", "Stopped (cancelled). The work so far is on this PR; comment `@claude continue` to go on."),
        node("ran-out", "Stopped with work left: the job's time ran out."),
        node("auto", "@claude continue\n\n<!-- claude:auto-continue -->\nThe job's time ran out."),
        node("wait", "The job failed before Claude started. <!-- claude:auto-wait at=1 asked= -->"),
        node("empty", "**Claude finished @octo's task in 25m** —— [View job](u)\n\n---\n"),
        node("placeholder", "**Claude finished @octo's task** —— [View job](u)\n\n---\nI'll analyze this and get back to you."),
        node("dead", "Claude Code is working… <img src=x>  I'll analyze this and get back to you."),
        node("summary", "The pages are built, and the reviewer approved them."),
        node("told", "**Claude finished @octo's task** —— [View job](u)\n\n---\nThe branch is rebased and the checks are green."),
        node("cost", "<!-- job-cost {} -->"),
        node("person", "@claude continue", login="octo", kind="User"),
        node("namesake", "Stopped (failure).", kind="User"),
        node("this-job", "**Claude finished @octo's task** —— [View job](u)\n\n---\n", minutes_ago=1),
        node("already", "Stopped (failure).", minimized=True),
    ]
    gh = Gh(checkout, graph=graph)

    assert job_finish.main(going_on(minutes_ago=30), run=gh) == 0

    assert hidden(gh) == ["stopped", "ran-out", "auto", "wait", "empty", "placeholder", "dead"]
    assert all("classifier: OUTDATED" in call[4] for call in gh.called("gh", "api", "graphql") if "--paginate" not in call)
    assert "Hid 7 outdated comment(s) on #3." in capsys.readouterr().out


def test_the_issue_and_its_pr_are_both_tidied(checkout: Path) -> None:
    subprocess.run(["git", "-C", str(checkout), "switch", "-q", "-c", "feat/3-thing"], check=True)
    gh = Gh(checkout, pr=PR_URL, body="Level: claude · Status: done\n", graph=[node("stopped", "Stopped (failure).")])

    job_finish.main(going_on(minutes_ago=30), run=gh)

    listed = [call for call in gh.called("gh", "api", "graphql") if "--paginate" in call]
    assert [next(a for a in call if a.startswith("number=")) for call in listed] == ["number=3", "number=7"]
    assert listed[0][4:6] == ("-F", "owner=o")


def test_comments_it_cannot_read_leave_the_job_as_it_was(checkout: Path, capsys: pytest.CaptureFixture[str]) -> None:
    gh = Gh(checkout, graph=None)

    assert job_finish.main(going_on(minutes_ago=30), run=gh) == 0
    assert "Could not hide the outdated comments on #3: HTTP 502" in capsys.readouterr().out


def test_a_job_with_no_start_time_hides_nothing(checkout: Path) -> None:
    gh = Gh(checkout, graph=[node("stopped", "Stopped (failure).")])

    job_finish.main(env("failure"), run=gh)

    assert not gh.called("gh", "api", "graphql")


def test_a_job_that_goes_on_by_itself_says_only_that(checkout: Path) -> None:
    subprocess.run(["git", "-C", str(checkout), "switch", "-q", "-c", "feat/3-thing"], check=True)
    (checkout / "new.py").write_text("x = 1\n")
    gh = Gh(checkout, pr=PR_URL, body=WORKING_BODY)

    job_finish.main(going_on("failure"), run=gh)

    (note,) = [call[-1] for call in gh.called("gh", "pr", "comment")]
    assert job_finish.AUTO in note


def test_a_job_that_goes_on_having_changed_nothing_says_only_that(checkout: Path) -> None:
    subprocess.run(["git", "-C", str(checkout), "switch", "-q", "-c", "feat/3-thing"], check=True)
    gh = Gh(checkout, pr=PR_URL, body=WORKING_BODY)

    job_finish.main(going_on("failure"), run=gh)

    (note,) = [call[-1] for call in gh.called("gh", "pr", "comment")]
    assert job_finish.AUTO in note


def test_a_job_that_waits_says_it_stopped(checkout: Path) -> None:
    subprocess.run(["git", "-C", str(checkout), "switch", "-q", "-c", "feat/3-thing"], check=True)
    (checkout / "new.py").write_text("x = 1\n")
    gh = Gh(checkout, pr=PR_URL, body=WORKING_BODY)

    job_finish.main(going_on("failure", minutes_ago=10), run=gh)

    (note,) = [call[-1] for call in gh.called("gh", "pr", "comment")]
    assert note.startswith("Stopped (failure). The work so far is on this PR")
