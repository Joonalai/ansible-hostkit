import io
import json
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast, override

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
        pr: dict[str, Any] | subprocess.CompletedProcess[str] | None = None,
        since: Sequence[str] | subprocess.CompletedProcess[str] = (),
    ) -> None:
        self.answers, self.base, self.behind = list(answers), base, behind
        self.calls: list[tuple[str, ...]] = []
        self.asked, self.created = asked or {}, created
        self.pr = pr if pr is not None else {"labels": [], "body": "", "headRefOid": "abc"}
        self.since = since

    def __call__(self, *argv: str) -> subprocess.CompletedProcess[str]:  # noqa: C901, PLR0911, PLR0912  (a fake that answers each gh call)
        self.calls.append(argv)
        if argv[:3] == ("gh", "pr", "checks"):
            answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
            if isinstance(answer, subprocess.CompletedProcess):
                return cast("subprocess.CompletedProcess[str]", answer)
            return done(json.dumps(answer))
        if argv[:3] == ("gh", "run", "view"):
            return done(LOG)
        if argv[:3] == ("gh", "pr", "view") and "labels,body,headRefOid" in argv:
            if isinstance(self.pr, subprocess.CompletedProcess):
                return cast("subprocess.CompletedProcess[str]", self.pr)
            return done(json.dumps(self.pr))
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
        if argv[:2] == ("gh", "api") and "/compare/" in argv[2] and ".behind_by" not in argv:
            if isinstance(self.since, subprocess.CompletedProcess):
                return cast("subprocess.CompletedProcess[str]", self.since)
            return done(json.dumps(list(self.since)))
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


SHA = "0123abc"
PASSED = f"Closes #1\n\n<details><summary>Tried: ✅ passed, at {SHA}, profile try</summary>\n\nall lines\n</details>\n"


def pr(*labels: str, body: str = PASSED) -> dict[str, Any]:
    return {"labels": [{"name": name} for name in labels], "body": body, "headRefOid": "abc"}


@pytest.mark.parametrize("labels", [(), ("claude",), ("claude:review",), ("claude:ui",), ("claude:plan", "claude:ui")])
def test_a_level_with_no_tester_needs_no_tried_section(labels: tuple[str, ...]) -> None:
    github = GitHub([check()], pr=pr(*labels, body="Closes #1"))
    assert hook("gh pr ready 3", github) == 0
    assert not [call for call in github.calls if "/compare/0123abc" in " ".join(call)]


@pytest.mark.parametrize("labels", [("claude:full",), ("claude:review", "claude:ui"), ("claude:full", "claude:ui")])
@pytest.mark.parametrize(
    "body",
    [
        "Closes #1",
        "<details><summary>Tester: ✅ passed</summary></details>",
        "<details><summary>Tried: not tried: no preview</summary></details>",
        "<details><summary>Tried: ❌ failed, at 0123abc</summary></details>",
        "<details><summary>Progress</summary>\nTried: ✅ passed, at 0123abc\n</details>",
    ],
)
def test_a_tested_level_with_no_passed_tried_section_is_refused(
    labels: tuple[str, ...], body: str, capsys: pytest.CaptureFixture[str],
) -> None:
    assert hook("gh pr ready 3", GitHub([check()], pr=pr(*labels, body=body))) == 2
    err = capsys.readouterr().err
    assert 'no "Tried" section' in err
    assert "call `tester`" in err
    assert "Status: asked" in err


@pytest.mark.parametrize("since", [[], ["docs/guide.md"], ["README.md", "docs/shots/board.png", "roles/x/CLAUDE.md"]])
def test_a_pass_with_only_docs_changed_since_lets_it_through(since: list[str]) -> None:
    github = GitHub([check()], pr=pr("claude:full"), since=since)
    assert hook("gh pr ready 3", github) == 0
    assert ("gh", "api", f"repos/o/r/compare/{SHA}...abc") in [call[:3] for call in github.calls]


def test_a_pass_says_rebased_unchanged_beside_its_sha() -> None:
    body = f"<details><summary>Tried: ✅ passed, at {SHA}, rebased, unchanged, profile default</summary></details>"
    assert hook("gh pr ready 3", GitHub([check()], pr=pr("claude:review", "claude:ui", body=body))) == 0


def test_code_changed_since_the_pass_is_refused(capsys: pytest.CaptureFixture[str]) -> None:
    since = ["docs/guide.md", "src/board.ts", "tests/test_board.py"]
    assert hook("gh pr ready 3", GitHub([check()], pr=pr("claude:full"), since=since)) == 2
    err = capsys.readouterr().err
    assert f"since the tester's pass at {SHA}: src/board.ts, tests/test_board.py." in err
    assert "docs/guide.md" not in err
    assert "`tester-check`" in err
    assert "rebased, unchanged" in err


def test_a_rename_out_of_the_code_counts_as_code() -> None:
    assert hook("gh pr ready 3", GitHub([check()], pr=pr("claude:full"), since=["docs/old.md", "src/old.ts"])) == 2


def test_a_list_as_long_as_github_gives_is_never_taken_for_docs_alone(capsys: pytest.CaptureFixture[str]) -> None:
    since = [f"docs/{n}.md" for n in range(job_checks.COMPARE_FILES)]
    assert hook("gh pr ready 3", GitHub([check()], pr=pr("claude:full"), since=since)) == 2
    assert "300 files or more" in capsys.readouterr().err


def test_many_changed_paths_are_named_a_few(capsys: pytest.CaptureFixture[str]) -> None:
    since = [f"src/{n}.ts" for n in range(8)]
    assert hook("gh pr ready 3", GitHub([check()], pr=pr("claude:full"), since=since)) == 2
    assert "src/4.ts, …." in capsys.readouterr().err


@pytest.mark.parametrize("answer", [done(stderr="HTTP 404: No commit found", code=1), done("not json")])
def test_a_compare_it_cannot_read_is_a_refusal(answer: subprocess.CompletedProcess[str], capsys: pytest.CaptureFixture[str]) -> None:
    assert hook("gh pr ready 3", GitHub([check()], pr=pr("claude:full"), since=answer)) == 2
    assert f"since the tester's pass at {SHA} could not be read" in capsys.readouterr().err


@pytest.mark.parametrize("answer", [done(stderr="HTTP 502", code=1), done(json.dumps({"labels": None}))])
def test_labels_it_cannot_read_are_a_refusal(answer: subprocess.CompletedProcess[str], capsys: pytest.CaptureFixture[str]) -> None:
    assert hook("gh pr ready 3", GitHub([check()], pr=answer)) == 2
    assert "needs the tester's pass could not be read" in capsys.readouterr().err


def test_red_checks_are_said_before_a_missing_pass(capsys: pytest.CaptureFixture[str]) -> None:
    assert hook("gh pr ready 3", GitHub([check("lint", "fail")], pr=pr("claude:full", body=""))) == 2
    err = capsys.readouterr().err
    assert "failing checks" in err
    assert "Tried" not in err


def test_the_pass_is_read_from_the_linked_pr_it_names() -> None:
    github = GitHub([check()], pr=pr("claude:full"))
    assert hook("gh pr ready 4 -R o/other", github) == 0
    assert ("gh", "pr", "view", "4", "-R", "o/other", "--json", "labels,body,headRefOid") in github.calls
    assert ("gh", "api", f"repos/o/other/compare/{SHA}...abc") in [call[:3] for call in github.calls]


HEAD = "f" * 40
OLD = "e" * 40
BOT_ID = "77"
RUNS = "https://github.com/o/r/actions/runs"


def comment(body: str, user: str = BOT_ID, n: int = 1) -> str:
    return json.dumps({"user": int(user), "url": f"https://github.com/o/r/pull/3#issuecomment-{n}", "body": body})


def ask(sha: str = HEAD, runs: Sequence[str] = ("11",)) -> str:
    return f"<!-- claude:rerun-asked {sha} -->\nAsking. Runs: " + " ".join(f"{RUNS}/{each}" for each in runs)


class Rerun:
    """gh for --rerun and --rerun-asked, at the head HEAD."""

    def __init__(
        self,
        comments: Sequence[str] = (),
        runs: dict[str, dict[str, str]] | None = None,
        rerun_code: int = 0,
    ) -> None:
        self.comments, self.rerun_code = list(comments), rerun_code
        self.runs = runs if runs is not None else {"11": run_info()}
        self.calls: list[tuple[str, ...]] = []
        self.tokens: list[str | None] = []

    def __call__(self, *argv: str, token: str | None = None) -> subprocess.CompletedProcess[str]:  # noqa: PLR0911  (a fake that answers each gh call)
        self.calls.append(argv)
        if argv[:3] == ("gh", "pr", "checks"):
            return done(json.dumps([check("unit", "fail"), check("lint")]))
        if argv[:3] == ("gh", "pr", "view"):
            return done(HEAD + "\n")
        if argv[:3] == ("gh", "run", "rerun"):
            self.tokens.append(token)
            return done(stderr="HTTP 403" if self.rerun_code else "", code=self.rerun_code)
        if argv[:3] == ("gh", "run", "view"):
            return done(LOG)
        if argv[:2] == ("gh", "api") and "/comments?" in argv[2]:
            return done("\n".join(self.comments))
        if argv[:2] == ("gh", "api") and "/actions/runs/" in argv[2]:
            info = self.runs.get(argv[2].rsplit("/", 1)[-1])
            return done(stderr="HTTP 404", code=1) if info is None else done(json.dumps(info))
        if argv[:2] == ("gh", "api") and argv[-1].startswith("body="):
            return done()
        if argv[:2] == ("gh", "api"):
            return done("[]")
        raise AssertionError(argv)

    def posted(self) -> list[str]:
        return [call[-1].removeprefix("body=") for call in self.calls if call[-1].startswith("body=")]

    def reran(self) -> list[str]:
        return [call[3] for call in self.calls if call[:3] == ("gh", "run", "rerun")]


def run_info(sha: str = HEAD, event: str = "pull_request", conclusion: str = "failure") -> dict[str, str]:
    return {"head_sha": sha, "event": event, "conclusion": conclusion, "html_url": f"{RUNS}/11"}


def checks_main(github: Rerun, monkeypatch: pytest.MonkeyPatch, *argv: str) -> int:
    """Main, with the checks read once for each wait: test_job_checks's others time the wait."""
    read = job_checks.read
    monkeypatch.setattr(job_checks, "wait", lambda number, repo, env, run, *_: read(number, repo, env, run))
    return job_checks.main(list(argv), ENV, run=github)


def test_rerun_asks_once_and_reruns_nothing_itself(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    github = Rerun()
    assert checks_main(github, monkeypatch, "3", "--rerun") == 1
    assert github.reran() == []
    [body] = github.posted()
    assert body.startswith(f"<!-- claude:rerun-asked {HEAD} -->\n")
    assert "Tests / unit" in body and f"{RUNS}/11" in body
    out = capsys.readouterr().out
    assert "FAILED: Tests / unit" in out
    assert "`Status: stopped: the failed checks are rerun once`" in out


@pytest.mark.parametrize("mark", ["rerun-asked", "rerun-done"])
def test_rerun_asks_nothing_at_a_head_marked_already(
    mark: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    github = Rerun([comment(f"<!-- claude:{mark} {HEAD} -->\nRuns")])
    assert checks_main(github, monkeypatch, "3", "--rerun") == 1
    assert github.posted() == []
    assert "it is the owner's" in capsys.readouterr().out


def test_without_the_flag_nothing_is_asked(monkeypatch: pytest.MonkeyPatch) -> None:
    github = Rerun()
    assert checks_main(github, monkeypatch, "3") == 1
    assert github.posted() == []


ASKED_ENV = ENV | {"CLAUDE_APP_BOT_ID": BOT_ID, "RERUN_TOKEN": "ghs_workflow"}


def test_the_rerun_job_reruns_what_the_bot_asked_at_the_head_with_its_own_token() -> None:
    github = Rerun([comment(ask())])
    assert job_checks.main(["--rerun-asked", "3"], ASKED_ENV, run=github) == 0
    assert ("gh", "run", "rerun", "11", "--failed", "-R", REPO) in github.calls
    assert github.tokens == ["ghs_workflow"]
    [body] = github.posted()
    assert body.startswith(f"<!-- claude:rerun-done {HEAD} -->\n") and f"{RUNS}/11" in body


@pytest.mark.parametrize(
    "info",
    [run_info(sha=OLD), run_info(event="push"), run_info(event="workflow_dispatch"), run_info(conclusion="success"), None],
)
def test_the_rerun_job_reruns_only_a_failed_pull_request_run_at_the_head(info: dict[str, str] | None) -> None:
    github = Rerun([comment(ask())], runs={} if info is None else {"11": info})
    assert job_checks.main(["--rerun-asked", "3"], ASKED_ENV, run=github) == 0
    assert github.reran() == []
    [body] = github.posted()
    assert "none could be" in body


@pytest.mark.parametrize(
    "comments",
    [
        [],
        [comment(ask(), user="5")],
        [comment(ask(sha=OLD))],
        [comment(ask()), comment(ask(), user="5", n=2)],
        [comment(ask()), comment(f"<!-- claude:rerun-done {HEAD} -->\nReran", n=2)],
    ],
)
def test_the_rerun_job_does_nothing_without_a_fresh_ask_of_the_bots(comments: list[str]) -> None:
    github = Rerun(comments)
    assert job_checks.main(["--rerun-asked", "3"], ASKED_ENV, run=github) == 0
    assert github.reran() == []
    assert github.posted() == []


def test_the_rerun_job_exits_0_when_a_rerun_fails(capsys: pytest.CaptureFixture[str]) -> None:
    github = Rerun([comment(ask())], rerun_code=1)
    assert job_checks.main(["--rerun-asked", "3"], ASKED_ENV, run=github) == 0
    assert "HTTP 403" in capsys.readouterr().out
    assert len(github.posted()) == 1


def test_the_rerun_job_exits_0_without_its_token(capsys: pytest.CaptureFixture[str]) -> None:
    github = Rerun([comment(ask())])
    assert job_checks.main(["--rerun-asked", "3"], ENV | {"CLAUDE_APP_BOT_ID": BOT_ID}, run=github) == 0
    assert github.reran() == []
    assert "RERUN_TOKEN" in capsys.readouterr().out
class Pulls(Rerun):
    """Rerun's gh over several PRs of the repository, each with its own comments."""

    def __init__(self, threads: dict[str, Sequence[str] | subprocess.CompletedProcess[str]], users: dict[str, str]) -> None:
        super().__init__()
        self.threads, self.users = threads, users

    @override
    def __call__(self, *argv: str, token: str | None = None) -> subprocess.CompletedProcess[str]:
        if argv[:2] == ("gh", "api") and argv[2].startswith("repos/o/r/pulls?"):
            self.calls.append(argv)
            return done("".join(f"{number}\t{user}\n" for number, user in self.users.items()))
        if argv[:2] == ("gh", "api") and "/comments?" in argv[2]:
            self.calls.append(argv)
            thread = self.threads[argv[2].split("/")[4]]
            if isinstance(thread, subprocess.CompletedProcess):
                return cast("subprocess.CompletedProcess[str]", thread)
            return done("\n".join(thread))
        return super().__call__(*argv, token=token)


def test_the_rerun_job_with_no_number_reruns_each_bot_pr_asked_at_its_head() -> None:
    github = Pulls({"3": [comment(ask())], "4": [comment("a review")]}, {"3": BOT_ID, "4": BOT_ID, "5": "5"})
    assert job_checks.main(["--rerun-asked"], ASKED_ENV, run=github) == 0
    assert github.reran() == ["11"]
    [body] = github.posted()
    assert body.startswith(f"<!-- claude:rerun-done {HEAD} -->")
    assert [call[2] for call in github.calls if call[-1].startswith("body=")] == ["repos/o/r/issues/3/comments"]
    assert not [call for call in github.calls if "issues/5/" in call[2]]


def test_the_rerun_job_goes_on_past_a_pr_it_cannot_read(capsys: pytest.CaptureFixture[str]) -> None:
    github = Pulls({"3": done(stderr="HTTP 502", code=1), "4": [comment(ask())]}, {"3": BOT_ID, "4": BOT_ID})
    assert job_checks.main(["--rerun-asked"], ASKED_ENV, run=github) == 0
    assert "#3 could not be read" in capsys.readouterr().out
    assert github.reran() == ["11"]


class Marked(Rerun):
    """Rerun's gh where the rerun job marks its rerun done after `after` seconds of the clock."""

    def __init__(self, clock: Clock, after: float | None) -> None:
        super().__init__([comment(ask())])
        self.clock, self.after = clock, after

    @override
    def __call__(self, *argv: str, token: str | None = None) -> subprocess.CompletedProcess[str]:
        made = self.after is not None and self.clock.now >= self.after
        if argv[:2] == ("gh", "api") and "/comments?" in argv[2] and made:
            self.calls.append(argv)
            return done("\n".join([comment(ask()), comment(f"<!-- claude:rerun-done {HEAD} -->\nReran", n=2)]))
        if argv[:3] == ("gh", "pr", "checks") and made:
            self.calls.append(argv)
            return done(json.dumps([check("unit"), check("lint")]))
        return super().__call__(*argv, token=token)


def test_waiting_waits_for_a_rerun_asked_at_the_head_and_reads_its_checks(capsys: pytest.CaptureFixture[str]) -> None:
    clock = Clock()
    checks = job_checks.wait("3", REPO, ENV, Marked(clock, after=120), clock, clock.sleep)
    assert [each["bucket"] for each in checks] == ["pass", "pass"]
    assert clock.now >= 120 + job_checks.SETTLE
    assert "was not made" not in capsys.readouterr().out


def test_waiting_for_a_rerun_never_made_says_so_at_the_limit(capsys: pytest.CaptureFixture[str]) -> None:
    clock = Clock()
    checks = job_checks.wait("3", REPO, ENV, Marked(clock, after=None), clock, clock.sleep)
    assert [each["bucket"] for each in checks] == ["fail", "pass"]
    assert clock.now >= job_checks.LIMIT
    assert f"The rerun asked for at {HEAD[:7]} was not made" in capsys.readouterr().out
