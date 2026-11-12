import io
import json
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

import job_checks
import pytest

REPO = "o/r"
ENV = {"GITHUB_REPOSITORY": REPO, "GITHUB_WORKFLOW": "Claude", "GITHUB_RUN_ID": "9", "CLAUDE_ALLOWED_IDS": "[5]"}
CREATED = "2026-11-12T04:37:42Z"
LINK = "https://github.com/o/r/actions/runs/11/job/22"
LOG = "lint\tstep\t2026-11-06T15:39:23.8563182Z 1: CT1 Title does not start\n"


def check(name: str = "unit", bucket: str = "pass", workflow: str = "Tests", link: str = LINK) -> dict[str, str]:
    return {"name": name, "bucket": bucket, "workflow": workflow, "link": link}


def done(stdout: str = "", stderr: str = "", code: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], code, stdout, stderr)


class GitHub:
    """gh, with one answer to `gh pr checks` per call, the last one repeating."""

    def __init__(
        self,
        *answers: list[dict[str, str]] | subprocess.CompletedProcess[str],
        base: Sequence[str] = (),
        behind: int | subprocess.CompletedProcess[str] = 0,
        asked: dict[str, list[dict[str, Any]] | subprocess.CompletedProcess[str]] | None = None,
        created: str | subprocess.CompletedProcess[str] = CREATED,
    ) -> None:
        self.answers, self.base, self.behind = list(answers), base, behind
        self.calls: list[tuple[str, ...]] = []
        self.asked, self.created = asked or {}, created

    def __call__(self, *argv: str) -> subprocess.CompletedProcess[str]:  # noqa: C901, PLR0911  (a fake that answers each gh call)
        self.calls.append(argv)
        if argv[:3] == ("gh", "pr", "checks"):
            answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
            if isinstance(answer, subprocess.CompletedProcess):
                return cast("subprocess.CompletedProcess[str]", answer)
            return done(json.dumps(answer))
        if argv[:3] == ("gh", "run", "view"):
            return done(LOG)
        if argv[:3] == ("gh", "pr", "view") and "baseRefName,headRefOid" in argv:
            return done(json.dumps({"baseRefName": "main", "headRefOid": "abc"}))
        if argv[:3] == ("gh", "pr", "view"):
            return done("abc\n")
        if argv[:2] == ("gh", "api") and argv[2] == "repos/o/r/actions/runs/9":
            if isinstance(self.created, subprocess.CompletedProcess):
                return cast("subprocess.CompletedProcess[str]", self.created)
            return done(self.created + "\n")
        if argv[:2] == ("gh", "api") and "--paginate" in argv:
            _, _, _, where, _, what = argv[2].split("?")[0].split("/")
            answer = self.asked.get(f"{where}/{what}", [])
            if isinstance(answer, subprocess.CompletedProcess):
                return cast("subprocess.CompletedProcess[str]", answer)
            return done("\n".join(json.dumps(each) for each in answer))
        if argv[:2] == ("gh", "api") and "/compare/" in argv[2]:
            if isinstance(self.behind, subprocess.CompletedProcess):
                return cast("subprocess.CompletedProcess[str]", self.behind)
            return done(f"{self.behind}\n")
        if argv[:2] == ("gh", "api"):
            return done(json.dumps(list(self.base)))
        raise AssertionError(argv)


class Clock:
    def __init__(self) -> None:
        self.now: float = 0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def run_wait(github: GitHub) -> tuple[list[dict[str, Any]], float]:
    clock = Clock()
    return job_checks.wait("3", REPO, ENV, github, clock, clock.sleep), clock.now


def test_a_passing_check_is_not_believed_before_the_others_can_appear() -> None:
    checks, waited = run_wait(GitHub([check()], [check(), check("style", "pending")], [check(), check("style")]))
    assert waited >= job_checks.SETTLE
    assert [c["name"] for c in checks] == ["unit", "style"]


def test_waiting_stops_at_the_limit() -> None:
    checks, waited = run_wait(GitHub([check(bucket="pending")]))
    assert waited >= job_checks.LIMIT
    assert job_checks.result("3", REPO, checks, GitHub([])) == 3


def test_the_jobs_own_run_is_left_out() -> None:
    github = GitHub([check(), check("claude", "pending", "Claude")])
    assert [c["name"] for c in job_checks.read("3", REPO, ENV, github)] == ["unit"]


def test_a_pr_with_no_checks_has_nothing_to_wait_for(capsys: pytest.CaptureFixture[str]) -> None:
    github = GitHub(done(stderr="no checks reported on the 'x' branch", code=1))
    assert job_checks.read("3", REPO, ENV, github) == []
    assert job_checks.result("3", REPO, [], github) == 0
    assert "no checks" in capsys.readouterr().out


def test_checks_all_skipped_on_a_draft_say_ci_tried_nothing(capsys: pytest.CaptureFixture[str]) -> None:
    assert job_checks.result("3", REPO, [check(bucket="skipping"), check("lint", "skipping")], GitHub([])) == 0
    out = capsys.readouterr().out
    assert "Skipped: all 2 checks" in out
    assert "after `gh pr ready`, run claude-job-checks again" in out


def test_one_check_run_among_the_skipped_ones_passes(capsys: pytest.CaptureFixture[str]) -> None:
    assert job_checks.result("3", REPO, [check(bucket="skipping"), check("lint")], GitHub([])) == 0
    assert "Passed: all 2 checks." in capsys.readouterr().out


def test_a_failure_comes_with_its_log_without_the_timestamps(capsys: pytest.CaptureFixture[str]) -> None:
    github = GitHub([])
    assert job_checks.result("3", REPO, [check("lint", "fail", "Prek"), check()], github) == 1
    out = capsys.readouterr().out
    assert "FAILED: Prek / lint\n" in out
    assert "1: CT1 Title does not start" in out
    assert "2026-11-06" not in out
    assert ("gh", "run", "view", "11", "--job", "22", "--log-failed", "-R", REPO) in github.calls


def test_a_failure_the_base_shares_says_so(capsys: pytest.CaptureFixture[str]) -> None:
    job_checks.result("3", REPO, [check("lint", "fail")], GitHub([], base=["lint"]))
    assert "FAILED: Tests / lint (also failing on the base)" in capsys.readouterr().out


def test_a_cancelled_check_is_no_pass() -> None:
    assert job_checks.result("3", REPO, [check(bucket="cancel")], GitHub([])) == 1


def test_a_log_it_cannot_read_still_reports_the_failure(capsys: pytest.CaptureFixture[str]) -> None:
    def run(*argv: str) -> subprocess.CompletedProcess[str]:
        return done(stderr="HTTP 403", code=1)

    assert job_checks.result("3", REPO, [check(bucket="fail")], run) == 1
    assert "log unreadable: HTTP 403" in capsys.readouterr().out


def test_checks_it_cannot_read_are_an_error(capsys: pytest.CaptureFixture[str]) -> None:
    github = GitHub(done(stderr="Resource not accessible by integration", code=1))
    assert job_checks.main(["3"], ENV, run=github) == 2
    assert "Resource not accessible" in capsys.readouterr().err


def hook(command: str, github: GitHub) -> int:
    return job_checks.hook(ENV, io.StringIO(json.dumps({"tool_input": {"command": command}})), github)


@pytest.mark.parametrize(
    "command",
    [
        "git status",
        "gh pr view 3",
        'gh pr comment 3 --body "run gh pr ready 3 when you like"',
        "gh pr ready --undo 3",
    ],
)
def test_the_hook_lets_other_commands_through_without_asking_github(command: str) -> None:
    github = GitHub([check(bucket="fail")])
    assert hook(command, github) == 0
    assert github.calls == []


def test_the_hook_passes_a_green_pr() -> None:
    assert hook("gh pr ready 3", GitHub([check()])) == 0


@pytest.mark.parametrize("bucket", ["fail", "cancel", "pending"])
def test_the_hook_blocks_a_pr_not_yet_green(bucket: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert hook("gh pr ready 3", GitHub([check("lint", bucket)])) == 2
    assert "Blocked" in capsys.readouterr().err


def test_the_hook_blocks_when_it_cannot_read() -> None:
    assert hook("gh pr ready 3", GitHub(done(stderr="HTTP 403", code=1))) == 2


def test_the_hook_blocks_a_ready_that_names_no_pr(capsys: pytest.CaptureFixture[str]) -> None:
    github = GitHub([check()])
    assert hook("cd ../other && gh pr ready", github) == 2
    assert github.calls == []
    assert "name the PR" in capsys.readouterr().err


@pytest.mark.parametrize(
    "command",
    [
        "gh pr ready 4 -R o/other",
        "gh pr ready --repo o/other 4",
        "date && gh pr ready https://github.com/o/other/pull/4",
    ],
)
def test_the_hook_reads_the_pr_it_names(command: str) -> None:
    github = GitHub([check()])
    assert hook(command, github) == 0
    asked = [call[2] for call in github.calls if "--paginate" in call]
    assert asked == [f"repos/o/other/{path}?per_page=100" for path in ("issues/4/comments", "pulls/4/reviews", "pulls/4/comments")]
    view = next(call for call in github.calls if call[:3] == ("gh", "pr", "view"))
    assert view[3:6] == ("4", "-R", "o/other")
    assert ("gh", "api", "repos/o/other/compare/main...abc", "--jq", ".behind_by") in github.calls


def test_the_hook_blocks_a_pr_behind_its_base(capsys: pytest.CaptureFixture[str]) -> None:
    github = GitHub([check()], behind=3)
    assert hook("gh pr ready 3", github) == 2
    err = capsys.readouterr().err
    assert "3 commit(s) behind `main`" in err
    assert "git rebase origin/main" in err
    assert not [call for call in github.calls if call[:3] == ("gh", "pr", "checks")]


def test_the_hook_blocks_when_it_cannot_tell_whether_the_pr_is_behind(capsys: pytest.CaptureFixture[str]) -> None:
    github = GitHub([check()], behind=done(stderr="HTTP 404", code=1))
    assert hook("gh pr ready 3", github) == 2
    assert "HTTP 404" in capsys.readouterr().err


def test_the_undo_asks_nothing_about_the_base() -> None:
    github = GitHub([check()], behind=3)
    assert hook("gh pr ready --undo 3", github) == 0
    assert github.calls == []


def said(
    at: str, user: int = 5, body: str | None = "@claude continue", url: str = "https://github.com/o/r/pull/3#c1",
) -> dict[str, Any]:
    return {"user": user, "at": at, "url": url, "body": body}


LATER = "2026-11-12T07:10:57Z"


@pytest.mark.parametrize("where", ["issues/comments", "pulls/reviews", "pulls/comments"])
def test_the_hook_blocks_a_ready_while_the_user_asked_since_the_job_started(where: str, capsys: pytest.CaptureFixture[str]) -> None:
    github = GitHub([check()], asked={where: [said(LATER)]})
    assert hook("gh pr ready 3", github) == 2
    err = capsys.readouterr().err
    assert "the user asked for more on o/r#3: https://github.com/o/r/pull/3#c1" in err
    assert "leave the PR a draft" in err
    assert "What stays red" not in err


@pytest.mark.parametrize(
    "comment",
    [
        said("2026-11-12T04:30:00Z"),
        said(CREATED),
        said(LATER, user=77),
        said(LATER, body="looks good, thanks"),
        said(LATER, body=None),
    ],
    ids=["before the run", "the trigger itself", "not on the allow-list", "no mention", "empty review"],
)
def test_the_hook_passes_what_asked_nothing_since(comment: dict[str, Any]) -> None:
    assert hook("gh pr ready 3", GitHub([check()], asked={"issues/comments": [comment]})) == 0


def test_the_hook_blocks_when_it_cannot_read_what_was_asked(capsys: pytest.CaptureFixture[str]) -> None:
    github = GitHub([check()], asked={"pulls/reviews": done(stderr="HTTP 403", code=1)})
    assert hook("gh pr ready 3", github) == 2
    assert "since this job started could not be read (HTTP 403)" in capsys.readouterr().err


def test_the_hook_blocks_without_the_run_to_compare_with(capsys: pytest.CaptureFixture[str]) -> None:
    env = {key: value for key, value in ENV.items() if key != "GITHUB_RUN_ID"}
    github = GitHub([check()])
    assert job_checks.hook(env, io.StringIO(json.dumps({"tool_input": {"command": "gh pr ready 3"}})), github) == 2
    assert "GITHUB_RUN_ID" in capsys.readouterr().err


def test_a_ready_blocked_for_its_checks_still_says_how_to_fix_them(capsys: pytest.CaptureFixture[str]) -> None:
    assert hook("gh pr ready 3", GitHub([check("lint", "fail")])) == 2
    assert "What stays red stays a draft" in capsys.readouterr().err


def test_waiting_says_true_when_the_user_asked_since(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    output = tmp_path / "output"
    github = GitHub([check()], asked={"issues/comments": [said(LATER)]})
    assert job_checks.main(["--waiting", "3"], {**ENV, "GITHUB_OUTPUT": str(output)}, run=github) == 0
    assert output.read_text() == "waiting=true\n"
    assert "::notice::" in capsys.readouterr().out


def test_waiting_says_false_when_nobody_did(tmp_path: Path) -> None:
    output = tmp_path / "output"
    assert job_checks.main(["--waiting", "3"], {**ENV, "GITHUB_OUTPUT": str(output)}, run=GitHub([check()])) == 0
    assert output.read_text() == "waiting=false\n"


def test_waiting_lets_the_rebase_run_when_it_cannot_read(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    output = tmp_path / "output"
    github = GitHub([check()], created=done(stderr="HTTP 502", code=1))
    assert job_checks.main(["--waiting", "3"], {**ENV, "GITHUB_OUTPUT": str(output)}, run=github) == 0
    assert output.read_text() == "waiting=false\n"
    assert "::warning::" in capsys.readouterr().out


def test_a_bot_named_in_capitals_is_still_found_in_a_lowered_body(monkeypatch: pytest.MonkeyPatch) -> None:
    import importlib

    monkeypatch.setenv("CLAUDE_BOT", "MyBot")
    try:
        assert importlib.reload(job_checks).MENTION == "@mybot"
    finally:
        monkeypatch.delenv("CLAUDE_BOT")
        importlib.reload(job_checks)
