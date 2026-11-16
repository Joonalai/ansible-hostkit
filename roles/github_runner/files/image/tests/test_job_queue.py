import datetime as dt
import json
from collections.abc import Iterable
from pathlib import Path
from typing import override

import job_queue
import pytest

BOT = "77"
ME = "5"
NOW = dt.datetime(2026, 11, 16, 12, 0, tzinfo=dt.UTC).timestamp()


def ago(minutes: float) -> str:
    """A GitHub time the given minutes before NOW."""
    return dt.datetime.fromtimestamp(NOW - minutes * 60, dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def pr(
    number: int, status: str = "working", *, draft: bool = True, issue: int = 0, opened: float = 60,
    updated: float = 5, user: str = BOT, labels: Iterable[str] = (),
) -> dict[str, object]:
    """One open PR as the queue's jq gives it."""
    first = f"Closes #{issue or number + 100}"
    return {
        "number": number, "draft": draft, "body": f"{first}\n\nLevel: claude:full · Status: {status}",
        "created_at": ago(opened), "updated_at": ago(updated), "user": user, "labels": list(labels),
    }


class Gh(list[tuple[str, ...]]):
    """A recorded gh over a repository's PRs, issues, comments and events."""

    def __init__(
        self,
        prs: Iterable[dict[str, object]] = (),
        recent: Iterable[tuple[int, Iterable[str]]] = (),
        queued: Iterable[tuple[int, Iterable[str], float]] = (),
        comments: dict[int, list[tuple[str, bool, float]]] | None = None,
    ) -> None:
        super().__init__()
        self.prs = list(prs)
        self.recent = [(number, list(labels)) for number, labels in recent]
        self.queued = [(number, list(labels), minutes) for number, labels, minutes in queued]
        self.comments = comments or {}

    def __call__(self, *argv: str) -> str:  # noqa: PLR0911  (a fake that answers each call)
        self.append(argv)
        if argv[0] == "claude-job-finish":
            return ""
        path = argv[2] if argv[2] != "--method" else argv[4]
        if "-f" in argv or "DELETE" in argv:
            return ""
        if "/pulls?" in path:
            return "\n".join(json.dumps(each) for each in self.prs)
        if "issues?" in path:
            listed = self.recent if "since=" in path else [(n, labels) for n, labels, _ in self.queued]
            return "\n".join(json.dumps({"number": n, "labels": labels}) for n, labels in listed)
        number = int(path.split("/")[4])
        if path.endswith("/events"):
            return "\n".join(ago(minutes) for n, _, minutes in self.queued if n == number)
        if path.endswith("/comments"):
            return "\n".join(
                json.dumps([user, marked, ago(minutes)]) for user, marked, minutes in self.comments.get(number, ())
            )
        return ""

    def posted(self) -> dict[int, str]:
        """The comments made, by the issue or PR they went on."""
        made = [call for call in self if "-f" in call and call[2].endswith("/comments")]
        return {int(call[2].split("/")[4]): call[-1].removeprefix("body=") for call in made}

    def labelled(self) -> list[tuple[str, ...]]:
        return [call for call in self if call[2].endswith("/labels") and "-f" in call]

    def unlabelled(self) -> list[str]:
        return [call[4] for call in self if "DELETE" in call]


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    return {
        "GITHUB_REPOSITORY": "o/r",
        "CLAUDE_APP_BOT_ID": BOT,
        "CLAUDE_ALLOWED_IDS": f"[{ME}]",
        "CLAUDE_WORK_RUNNERS": "2",
        "CLAUDE_PRIORITY_LABELS": "",
        "GITHUB_OUTPUT": str(tmp_path / "output"),
        "GITHUB_EVENT_PATH": str(tmp_path / "event.json"),
        "GITHUB_EVENT_NAME": "issues",
    }


def labelled(env: dict[str, str], number: int = 9, label: str = "claude:full") -> dict[str, str]:
    """The environment of a job started by labelling an issue."""
    event = {"action": "labeled", "label": {"name": label}, "issue": {"number": number, "labels": [{"name": label}]}}
    Path(env["GITHUB_EVENT_PATH"]).write_text(json.dumps(event))
    return env


def output(env: dict[str, str]) -> str:
    path = Path(env["GITHUB_OUTPUT"])
    return path.read_text() if path.exists() else ""


# gate


def test_gate_passes_under_the_cap(env: dict[str, str]) -> None:
    gh = Gh(prs=[pr(1)])

    assert job_queue.main(["", "gate"], labelled(env), run=gh, now=NOW) == 0

    assert output(env) == ""
    assert gh.labelled() == []


def test_gate_at_the_cap_labels_the_issue_and_holds_the_job(env: dict[str, str]) -> None:
    gh = Gh(prs=[pr(1), pr(2)])

    assert job_queue.main(["", "gate"], labelled(env), run=gh, now=NOW) == 0

    assert output(env) == "queued=true\n"
    assert gh.labelled() == [("gh", "api", "repos/o/r/issues/9/labels", "-f", "labels[]=claude:queued")]


def test_gate_counts_an_issue_just_started_but_not_its_own(env: dict[str, str]) -> None:
    gh = Gh(prs=[pr(1)], recent=[(9, ["claude:full"]), (8, ["claude:review"])])

    job_queue.main(["", "gate"], labelled(env), run=gh, now=NOW)

    assert output(env) == "queued=true\n"


def test_gate_passes_a_comment_at_the_cap(env: dict[str, str]) -> None:
    env["GITHUB_EVENT_NAME"] = "issue_comment"
    gh = Gh(prs=[pr(1), pr(2)])

    job_queue.main(["", "gate"], labelled(env), run=gh, now=NOW)

    assert output(env) == ""
    assert gh == []


def test_gate_passes_an_issue_with_an_open_pr(env: dict[str, str]) -> None:
    gh = Gh(prs=[pr(1, issue=9), pr(2)])

    job_queue.main(["", "gate"], labelled(env), run=gh, now=NOW)

    assert output(env) == ""


def test_gate_passes_a_plan(env: dict[str, str]) -> None:
    gh = Gh(prs=[pr(1), pr(2)])

    job_queue.main(["", "gate"], labelled(env, label="claude:plan"), run=gh, now=NOW)

    assert output(env) == ""


@pytest.mark.parametrize("runners", ["", "0"])
def test_gate_does_nothing_without_a_cap(env: dict[str, str], runners: str) -> None:
    env["CLAUDE_WORK_RUNNERS"] = runners
    gh = Gh(prs=[pr(1), pr(2)])

    job_queue.main(["", "gate"], labelled(env), run=gh, now=NOW)

    assert output(env) == ""
    assert gh == []


def test_gate_does_not_count_asked_stale_or_ready_prs_nor_other_authors(env: dict[str, str]) -> None:
    gh = Gh(prs=[
        pr(1, "asked: which way"), pr(2, updated=200), pr(3, draft=False), pr(4, user="6"), pr(5, "done"),
    ])

    job_queue.main(["", "gate"], labelled(env), run=gh, now=NOW)

    assert output(env) == ""


def test_gate_counts_a_draft_waiting_for_the_usage_limit_whatever_its_age(env: dict[str, str]) -> None:
    gh = Gh(prs=[pr(1), pr(2, updated=600, labels=["claude:waiting"])])

    job_queue.main(["", "gate"], labelled(env), run=gh, now=NOW)

    assert output(env) == "queued=true\n"


def test_gate_lets_the_job_run_when_it_cannot_read(env: dict[str, str]) -> None:
    def broken(*argv: str) -> str:
        raise job_queue.subprocess.CalledProcessError(1, argv)

    assert job_queue.main(["", "gate"], labelled(env), run=broken, now=NOW) == 0
    assert output(env) == ""


# next


def test_next_does_nothing_at_the_cap(env: dict[str, str]) -> None:
    gh = Gh(prs=[pr(1), pr(2), pr(3, "stopped: out of turns")], queued=[(9, [], 10)])

    assert job_queue.main(["", "next"], env, run=gh, now=NOW) == 0

    assert gh.posted() == {}
    assert gh.unlabelled() == []


def test_next_continues_the_oldest_stopped_draft_before_a_queued_issue(env: dict[str, str]) -> None:
    env["CLAUDE_WORK_RUNNERS"] = "1"
    gh = Gh(
        prs=[pr(3, "stopped: out of turns", opened=30), pr(4, "stopped: out of context", opened=90)],
        queued=[(9, [], 10)],
    )

    job_queue.main(["", "next"], env, run=gh, now=NOW)

    assert list(gh.posted()) == [4]
    assert gh.posted()[4].startswith("@claude continue\n\n<!-- claude:auto-continue -->")
    assert gh.unlabelled() == []


def test_next_continues_a_working_draft_whose_job_died(env: dict[str, str]) -> None:
    env["CLAUDE_WORK_RUNNERS"] = "1"
    gh = Gh(prs=[pr(3, updated=job_queue.STALE_MINUTES + 1)])

    job_queue.main(["", "next"], env, run=gh, now=NOW)

    assert job_queue.CONTINUE in gh.posted()[3]


def test_next_takes_queued_issues_by_priority_then_by_when_they_were_queued(env: dict[str, str]) -> None:
    env["CLAUDE_WORK_RUNNERS"] = "1"
    env["CLAUDE_PRIORITY_LABELS"] = "urgent, soon"
    gh = Gh(queued=[(7, [], 300), (8, ["soon"], 20), (9, ["soon", "bug"], 40), (6, ["later"], 500)])

    job_queue.main(["", "next"], env, run=gh, now=NOW)

    assert gh.unlabelled() == ["repos/o/r/issues/9/labels/claude:queued"]
    assert gh.posted()[9].startswith("@claude continue\n\n<!-- claude:auto-queue -->")


def test_next_takes_an_unprioritised_queue_in_the_order_it_was_queued(env: dict[str, str]) -> None:
    gh = Gh(queued=[(7, [], 30), (8, [], 300), (9, [], 3)])

    job_queue.main(["", "next"], env, run=gh, now=NOW)

    assert list(gh.posted()) == [8, 7]


def test_next_fills_two_places_in_one_run(env: dict[str, str]) -> None:
    env["CLAUDE_WORK_RUNNERS"] = "3"
    gh = Gh(prs=[pr(1), pr(3, "stopped: out of turns")], queued=[(9, [], 10), (8, [], 5)])

    job_queue.main(["", "next"], env, run=gh, now=NOW)

    assert list(gh.posted()) == [3, 9]


def test_next_never_touches_an_asked_draft_or_a_ready_pr(env: dict[str, str]) -> None:
    gh = Gh(prs=[
        pr(1, "asked: not done after 4 jobs in a row"), pr(2, "stopped: out of turns", draft=False),
        pr(3, "asked: cancelled"), pr(4, "asked: failure"), pr(5, "asked: ended early with nothing committed"),
    ])

    job_queue.main(["", "next"], env, run=gh, now=NOW)

    assert gh.posted() == {}


def test_next_leaves_a_draft_that_went_on_its_rounds_to_the_owner(env: dict[str, str]) -> None:
    env["CLAUDE_WORK_RUNNERS"] = "1"
    rounds = [(ME, False, 600)] + [(BOT, True, 500 - each) for each in range(job_queue.ROUNDS)]
    gh = Gh(prs=[pr(3, "stopped: out of turns")], comments={3: [*rounds, (BOT, False, 100)]}, queued=[(9, [], 10)])

    job_queue.main(["", "next"], env, run=gh, now=NOW)

    assert list(gh.posted()) == [9]
    assert ("claude-job-finish", "spent", "3") in gh


def test_a_draft_past_its_rounds_is_asked_once_and_then_never_touched(env: dict[str, str]) -> None:
    env["CLAUDE_WORK_RUNNERS"] = "1"
    rounds: list[tuple[str, bool, float]] = [(ME, False, 600)] + [(BOT, True, 500 - each) for each in range(job_queue.ROUNDS)]
    first = Gh(prs=[pr(3, "stopped: out of turns")], comments={3: rounds})

    job_queue.main(["", "next"], env, run=first, now=NOW)

    assert [call for call in first if call[0] == "claude-job-finish"] == [("claude-job-finish", "spent", "3")]
    assert first.posted() == {}

    # What spent left: the draft at asked, which no next takes again.
    second = Gh(prs=[pr(3, "asked: not continued after 4 jobs in a row")], comments={3: rounds})

    job_queue.main(["", "next"], env, run=second, now=NOW)

    assert [call for call in second if call[0] == "claude-job-finish"] == []
    assert second.posted() == {}
    assert not [call for call in second if any("/issues/3/comments" in part for part in call)]


def test_next_counts_a_draft_it_just_continued_as_in_work(env: dict[str, str]) -> None:
    env["CLAUDE_WORK_RUNNERS"] = "1"
    gh = Gh(prs=[pr(3, "stopped: out of turns")], comments={3: [(BOT, True, 2)]}, queued=[(9, [], 10)])

    job_queue.main(["", "next"], env, run=gh, now=NOW)

    assert gh.posted() == {}


def test_next_counts_an_issue_just_started_as_in_work(env: dict[str, str]) -> None:
    env["CLAUDE_WORK_RUNNERS"] = "1"
    gh = Gh(recent=[(8, ["claude"]), (7, ["bug"])], queued=[(9, [], 10)])

    job_queue.main(["", "next"], env, run=gh, now=NOW)

    assert gh.posted() == {}


def test_next_warns_and_ends_well_when_it_cannot_start_something(env: dict[str, str]) -> None:
    class Failing(Gh):
        @override
        def __call__(self, *argv: str) -> str:
            if "-f" in argv:
                raise job_queue.subprocess.CalledProcessError(1, argv)
            return super().__call__(*argv)

    gh = Failing(prs=[pr(3, "stopped: out of turns")], queued=[(9, [], 10)])

    assert job_queue.main(["", "next"], env, run=gh, now=NOW) == 0


def test_next_ends_well_when_it_cannot_read(env: dict[str, str]) -> None:
    def broken(*argv: str) -> str:
        raise job_queue.subprocess.CalledProcessError(1, argv)

    assert job_queue.main(["", "next"], env, run=broken, now=NOW) == 0


def test_next_does_nothing_without_a_cap(env: dict[str, str]) -> None:
    env["CLAUDE_WORK_RUNNERS"] = "0"
    gh = Gh(queued=[(9, [], 10)])

    assert job_queue.main(["", "next"], env, run=gh, now=NOW) == 0
    assert gh == []


def test_an_unknown_command_gets_the_usage(env: dict[str, str]) -> None:
    assert job_queue.main(["", "jump"], env, run=Gh(), now=NOW) == 2
