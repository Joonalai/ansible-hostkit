import datetime as dt
import json
import subprocess
from collections.abc import Iterable
from pathlib import Path

import job_retry
import pytest

BOT = "77"
ME = "5"
NOW = dt.datetime(2026, 11, 8, 12, 0, tzinfo=dt.UTC).timestamp()


class Gh(list[tuple[str, ...]]):
    """A recorded gh and git over a repository's threads and labels."""

    def __init__(
        self,
        threads: dict[str, list[tuple[str, str]]] | None = None,
        labelled: Iterable[str] = (),
        branch: str = "main",
        prs: dict[str, str] | None = None,
        fails: Iterable[str] = (),
    ) -> None:
        super().__init__()
        self.threads = threads or {}
        self.labelled = list(labelled)
        self.branch = branch
        self.prs = prs or {}
        self.fails = set(fails)

    def __call__(self, *argv: str) -> str:
        self.append(argv)
        if argv[0] == "git":
            return self.branch
        if argv[1:3] == ("pr", "list"):
            return self.prs.get(argv[4], "")
        path = argv[2] if argv[2] != "--method" else argv[4]
        if any(fail in path for fail in self.fails):
            raise subprocess.CalledProcessError(1, argv)
        if "issues?" in path:
            return "\n".join(self.labelled)
        if path.endswith("/comments") and "-f" not in argv:
            number = path.split("/")[4]
            return "\n".join(json.dumps([user, body]) for user, body in self.threads.get(number, ()))
        return ""

    def posted(self) -> list[tuple[str, ...]]:
        return [call for call in self if "-f" in call and call[2].endswith("/comments")]

    def labels(self) -> list[tuple[str, ...]]:
        return [call for call in self if "labels" in " ".join(call) and ("-f" in call or "DELETE" in call)]


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    return {
        "GITHUB_REPOSITORY": "o/r",
        "CLAUDE_ALLOWED_IDS": f"[{ME}]",
        "CLAUDE_APP_BOT_ID": BOT,
        "NUMBER": "3",
        "RUN_URL": "https://github.com/o/r/actions/runs/9",
        "DEFAULT_BRANCH": "main",
        "RUNNER_TEMP": str(tmp_path),
        "GITHUB_OUTPUT": str(tmp_path / "output"),
    }


def at(text: str) -> dt.datetime:
    return dt.datetime.fromtimestamp(job_retry.reset_time(text, NOW), dt.UTC)


def margin(moment: dt.datetime) -> dt.datetime:
    return moment + dt.timedelta(minutes=job_retry.MARGIN_MINUTES)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Claude AI usage limit reached|1762700000", dt.datetime.fromtimestamp(1762700000 + 300, dt.UTC)),
        ("5-hour limit reached ∙ resets 3pm", margin(dt.datetime(2026, 11, 8, 15, 0, tzinfo=dt.UTC))),
        ("You've hit your limit · resets 9:30am", margin(dt.datetime(2026, 11, 9, 9, 30, tzinfo=dt.UTC))),
        ("Weekly limit reached ∙ resets Tue 10am", margin(dt.datetime(2026, 11, 10, 10, 0, tzinfo=dt.UTC))),
        ("limit reached ∙ resets 12am", margin(dt.datetime(2026, 11, 9, 0, 0, tzinfo=dt.UTC))),
        ("Claude usage limit reached.", dt.datetime(2026, 11, 8, 14, 0, tzinfo=dt.UTC)),
    ],
    ids=["epoch", "later today", "tomorrow", "a weekday", "midnight", "unsaid"],
)
def test_the_reset_time_is_read_from_the_message(text: str, expected: dt.datetime) -> None:
    assert at(text) == expected


def test_a_named_zone_counts_when_the_image_knows_it() -> None:
    pytest.importorskip("zoneinfo")
    try:
        helsinki = job_retry.zone("Europe/Helsinki")
    except Exception:  # noqa: BLE001 (whatever a missing zone raises, it is no zone data)
        pytest.skip("no zone data")
    if helsinki is dt.UTC:
        pytest.skip("no zone data")
    assert at("hit your limit · resets 3pm (Europe/Helsinki)") == margin(
        dt.datetime(2026, 11, 8, 13, 0, tzinfo=dt.UTC),
    )


def test_an_unknown_zone_reads_as_utc() -> None:
    assert at("limit reached ∙ resets 3pm (Nowhere/Else)") == margin(
        dt.datetime(2026, 11, 8, 15, 0, tzinfo=dt.UTC),
    )


def execution(tmp_path: Path, text: str, is_error: bool = True) -> None:
    (tmp_path / "claude-execution-output.json").write_text(
        json.dumps([{"type": "system"}, {"type": "result", "is_error": is_error, "result": text}]),
    )


def test_limit_writes_when_the_job_starts_again(env: dict[str, str], tmp_path: Path) -> None:
    execution(tmp_path, "5-hour limit reached ∙ resets 3pm")

    assert job_retry.main(["x", "limit"], {**env, "OUTCOME": "failure"}, now=NOW) == 0

    expected = int(margin(dt.datetime(2026, 11, 8, 15, 0, tzinfo=dt.UTC)).timestamp())
    assert (tmp_path / "output").read_text() == f"wait-until={expected}\n"


@pytest.mark.parametrize(
    ("outcome", "text", "is_error"),
    [
        ("failure", "Error: something broke", True),
        ("success", "Done. Note on the usage limit: none hit.", False),
        ("cancelled", "5-hour limit reached ∙ resets 3pm", True),
    ],
    ids=["another failure", "a summary that mentions it", "cancelled"],
)
def test_limit_writes_nothing_for_anything_else(
    env: dict[str, str], tmp_path: Path, outcome: str, text: str, is_error: bool,
) -> None:
    execution(tmp_path, text, is_error)

    assert job_retry.main(["x", "limit"], {**env, "OUTCOME": outcome}, now=NOW) == 0

    assert not (tmp_path / "output").exists()


def test_limit_without_an_execution_file_writes_nothing(env: dict[str, str], tmp_path: Path) -> None:
    assert job_retry.main(["x", "limit"], {**env, "OUTCOME": "failure"}, now=NOW) == 0

    assert not (tmp_path / "output").exists()


def test_a_job_that_failed_before_claude_waits_a_few_minutes(env: dict[str, str]) -> None:
    gh = Gh()

    assert job_retry.main(["x", "wait"], {**env, "KIND": "before", "ASKED": "quick"}, run=gh, now=NOW) == 0

    [call] = gh.posted()
    assert call[2] == "repos/o/r/issues/3/comments"
    body = call[4]
    assert "failed before Claude started" in body
    assert not body.removeprefix("body=").startswith("@claude")
    assert f"at={int(NOW) + job_retry.RETRY_MINUTES * 60} asked=quick" in body
    assert "1 of 3" in body
    assert gh.labels()[0][2:] == ("repos/o/r/issues/3/labels", "-f", "labels[]=claude:waiting")


def test_a_job_at_the_limit_waits_on_its_pr(env: dict[str, str]) -> None:
    gh = Gh(branch="feat/3-x", prs={"feat/3-x": "8"})

    job_retry.main(["x", "wait"], {**env, "KIND": "limit", "WAIT_UNTIL": "1762700000"}, run=gh, now=NOW)

    [call] = gh.posted()
    assert call[2] == "repos/o/r/issues/8/comments"
    assert "usage limit" in call[4] and "at=1762700000 asked= " in call[4]


def test_after_rounds_in_a_row_the_work_waits_for_you(env: dict[str, str]) -> None:
    auto = (BOT, f"@claude continue\n\n{job_retry.AUTO}")
    gh = Gh(threads={"3": [(ME, "@claude do it"), auto, auto, auto]})

    job_retry.main(["x", "wait"], {**env, "KIND": "before"}, run=gh, now=NOW)

    [call] = gh.posted()
    assert "3 times in a row" in call[4] and "auto-wait" not in call[4]
    assert gh.labels() == []


def test_your_comment_starts_the_rounds_again(env: dict[str, str]) -> None:
    auto = (BOT, job_retry.AUTO)
    gh = Gh(threads={"3": [auto, auto, auto, (ME, "go on"), auto]})

    job_retry.main(["x", "wait"], {**env, "KIND": "before"}, run=gh, now=NOW)

    assert "2 of 3" in gh.posted()[0][4]


def note(at: float, asked: str = "") -> tuple[str, str]:
    return (BOT, f"Stopped. <!-- claude:auto-wait at={int(at)} asked={asked} -->")


def test_the_sweep_starts_what_is_due_at_the_level_it_was_asked(env: dict[str, str]) -> None:
    gh = Gh(threads={"3": [(ME, "@claude quick: x"), note(NOW - 60, "quick")]}, labelled=["3"])

    assert job_retry.main(["x", "sweep"], env, run=gh, now=NOW) == 0

    [call] = gh.posted()
    body = call[4].removeprefix("body=")
    assert body.startswith("@claude quick: continue\n")
    assert job_retry.AUTO in body
    assert ("gh", "api", "--method", "DELETE", "repos/o/r/issues/3/labels/claude:waiting") in gh


def test_the_sweep_leaves_what_is_not_due(env: dict[str, str]) -> None:
    gh = Gh(threads={"3": [note(NOW + 60)]}, labelled=["3"])

    job_retry.main(["x", "sweep"], env, run=gh, now=NOW)

    assert gh.posted() == [] and gh.labels() == []


@pytest.mark.parametrize(
    "thread", [[note(NOW - 60), (ME, "@claude never mind")], []], ids=["you commented", "no note"],
)
def test_the_sweep_drops_a_wait_that_was_taken_over(env: dict[str, str], thread: list[tuple[str, str]]) -> None:
    gh = Gh(threads={"3": thread}, labelled=["3"])

    job_retry.main(["x", "sweep"], env, run=gh, now=NOW)

    assert gh.posted() == []
    assert ("gh", "api", "--method", "DELETE", "repos/o/r/issues/3/labels/claude:waiting") in gh


def test_the_bots_later_comments_do_not_hide_its_note(env: dict[str, str]) -> None:
    gh = Gh(threads={"3": [note(NOW - 60), (BOT, "cost report")]}, labelled=["3"])

    job_retry.main(["x", "sweep"], env, run=gh, now=NOW)

    assert len(gh.posted()) == 1


def test_one_failing_thread_stops_none_of_the_others(env: dict[str, str]) -> None:
    gh = Gh(threads={"4": [note(NOW - 60)]}, labelled=["3", "4"], fails=["issues/3/"])

    assert job_retry.main(["x", "sweep"], env, run=gh, now=NOW) == 1

    assert gh.posted()[0][2] == "repos/o/r/issues/4/comments"


def test_an_unknown_command_is_refused(env: dict[str, str]) -> None:
    assert job_retry.main(["x", "nope"], env, run=Gh(), now=NOW) == 2
