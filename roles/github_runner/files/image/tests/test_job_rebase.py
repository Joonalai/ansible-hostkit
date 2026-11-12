import json
import subprocess
from collections.abc import Iterable, Mapping
from typing import Any

import job_rebase
import pytest

BOT = "77"
ME = "5"


def pr(
    number: int, title: str = "Fix it", draft: bool = False, user: str = BOT, head: str = "o/r", body: str = "Closes #3",
) -> dict[str, Any]:
    return {"number": number, "title": title, "draft": draft, "body": body, "user": int(user), "head": head}


class Gh(list[tuple[str, ...]]):
    """A recorded gh over a repository's PRs, their mergeability, runs and comments."""

    def __init__(
        self,
        prs: Iterable[dict[str, Any]] = (),
        mergeable: dict[int, str | list[str]] | None = None,
        running: Iterable[str] = (),
        comments: Mapping[int, Iterable[str]] | None = None,
        issues: dict[int, str] | None = None,
        fails: Iterable[str] = (),
        merged: Iterable[dict[str, Any]] = (),
        states: dict[int, dict[str, Any]] | None = None,
        nexts: Mapping[int, Iterable[str]] | None = None,
    ) -> None:
        super().__init__()
        self.merged = list(merged)
        self.states = states or {}
        self.nexts = nexts or {}
        self.prs = list(prs)
        self.mergeable = mergeable or {}
        self.running = list(running)
        self.comments = comments or {}
        self.issues = issues or {3: "The issue"}
        self.fails = set(fails)

    def __call__(self, *argv: str) -> str:  # noqa: C901, PLR0911  (a fake that answers each gh call)
        self.append(argv)
        path = argv[2]
        if any(fail in path for fail in self.fails):
            raise subprocess.CalledProcessError(1, argv)
        if path == "repos/o/r/commits/abcdef1234567890/pulls":
            return "\n".join(json.dumps(each) for each in self.merged)
        if "/pulls?" in path:
            return "\n".join(json.dumps(each) for each in self.prs)
        if path.startswith("repos/o/r/pulls/"):
            said = self.mergeable.get(int(path.rsplit("/", 1)[1]), "true")
            return said.pop(0) if isinstance(said, list) else said
        if path == "repos/o/r/actions/runs/9":
            return "123"
        if path.startswith("repos/o/r/actions/workflows/123/runs"):
            return "\n".join(self.running)
        if path.startswith("repos/o/r/issues/") and path.endswith("/comments"):
            if "-f" in argv:
                return ""
            number = int(path.split("/")[4])
            if "select(.user.id == 77)" in argv[-1]:
                return "\n".join(self.nexts.get(number, ()))
            return "\n".join(self.comments.get(number, ()))
        if path.startswith("repos/o/r/issues/"):
            number = int(path.rsplit("/", 1)[1])
            if argv[-1] == ".title":
                return self.issues[number]
            return json.dumps(self.states[number])
        raise AssertionError(argv)

    def asked(self) -> list[tuple[str, ...]]:
        return [call for call in self if "-f" in call]


@pytest.fixture
def env() -> dict[str, str]:
    return {
        "GITHUB_REPOSITORY": "o/r",
        "GITHUB_SHA": "abcdef1234567890",
        "GITHUB_RUN_ID": "9",
        "DEFAULT_BRANCH": "main",
        "CLAUDE_ALLOWED_IDS": f"[{ME}]",
        "CLAUDE_APP_BOT_ID": BOT,
    }


def no_sleep(seconds: float) -> None:
    pass


def test_a_conflicting_pr_gets_a_marked_rebase_from_the_bot(env: dict[str, str]) -> None:
    gh = Gh([pr(7)], mergeable={7: "false"})

    assert job_rebase.main(env, run=gh, sleep=no_sleep) == 0

    [call] = gh.asked()
    assert call[2] == "repos/o/r/issues/7/comments"
    body = call[4].removeprefix("body=")
    assert body.startswith("@claude quick: rebase\n")
    assert "<!-- claude:auto-rebase abcdef1234567890 -->" in body
    assert "1 of 3" in body


def test_a_pr_that_merges_as_it_is_is_left_alone(env: dict[str, str]) -> None:
    gh = Gh([pr(7)], mergeable={7: "true"})

    assert job_rebase.main(env, run=gh, sleep=no_sleep) == 0

    assert gh.asked() == []


def test_it_asks_again_while_github_has_not_said(env: dict[str, str]) -> None:
    slept: list[float] = []
    gh = Gh([pr(7)], mergeable={7: ["null", "null", "false"]})

    assert job_rebase.main(env, run=gh, sleep=slept.append) == 0

    assert len(gh.asked()) == 1
    assert slept == [job_rebase.WAIT, job_rebase.WAIT]


def test_a_pr_github_never_says_of_is_left_alone(env: dict[str, str]) -> None:
    gh = Gh([pr(7)], mergeable={7: "null"})

    assert job_rebase.main(env, run=gh, sleep=no_sleep) == 0

    assert gh.asked() == []


@pytest.mark.parametrize(
    "other",
    [pr(7, draft=True), pr(7, user=ME), pr(7, head="fork/r")],
    ids=["draft", "someone else's", "from a fork"],
)
def test_only_the_bots_own_ready_prs_from_the_repository(env: dict[str, str], other: dict[str, Any]) -> None:
    gh = Gh([other], mergeable={7: "false"})

    assert job_rebase.main(env, run=gh, sleep=no_sleep) == 0

    assert gh.asked() == []


@pytest.mark.parametrize("title", ["Fix it", "The issue"], ids=["on the PR", "on its issue"])
def test_a_pr_with_a_job_running_or_waiting_is_left_alone(env: dict[str, str], title: str) -> None:
    gh = Gh([pr(7)], mergeable={7: "false"}, running=[title])

    assert job_rebase.main(env, run=gh, sleep=no_sleep) == 0

    assert gh.asked() == []


def test_another_prs_job_does_not_hold_it_back(env: dict[str, str]) -> None:
    gh = Gh([pr(7)], mergeable={7: "false"}, running=["Something else"])

    assert job_rebase.main(env, run=gh, sleep=no_sleep) == 0

    assert len(gh.asked()) == 1


def test_it_stops_after_rounds_in_a_row(env: dict[str, str]) -> None:
    gh = Gh([pr(7)], mergeable={7: "false"}, comments={7: [f"{BOT} true"] * job_rebase.ROUNDS})

    assert job_rebase.main(env, run=gh, sleep=no_sleep) == 0

    assert gh.asked() == []


def test_a_comment_from_someone_allowed_starts_the_rounds_again(env: dict[str, str]) -> None:
    gh = Gh([pr(7)], mergeable={7: "false"}, comments={7: [f"{BOT} true"] * 3 + [f"{ME} false", f"{BOT} true"]})

    assert job_rebase.main(env, run=gh, sleep=no_sleep) == 0

    assert "2 of 3" in gh.asked()[0][4]


def test_the_bots_other_comments_do_not_count(env: dict[str, str]) -> None:
    gh = Gh([pr(7)], mergeable={7: "false"}, comments={7: [f"{BOT} false"] * 5 + ["99 false"]})

    assert job_rebase.main(env, run=gh, sleep=no_sleep) == 0

    assert "1 of 3" in gh.asked()[0][4]


def test_one_pr_failing_stops_none_of_the_others(env: dict[str, str]) -> None:
    gh = Gh([pr(7), pr(8, title="Other", body="")], mergeable={8: "false"}, fails=["pulls/7"])

    assert job_rebase.main(env, run=gh, sleep=no_sleep) == 1

    [call] = gh.asked()
    assert call[2] == "repos/o/r/issues/8/comments"


def test_no_open_prs_reads_no_runs(env: dict[str, str]) -> None:
    gh = Gh([])

    assert job_rebase.main(env, run=gh, sleep=no_sleep) == 0

    assert [call for call in gh if "actions" in call[2]] == []


def test_a_failed_listing_fails_the_step(env: dict[str, str]) -> None:
    gh = Gh([pr(7)], fails=["/pulls?"])

    assert job_rebase.main(env, run=gh, sleep=no_sleep) == 1


def merged(
    number: int = 401,
    first: str = "Part 1/3 of #287",
    user: str = BOT,
    at: str | None = "2026-11-10T18:00:00Z",
    base: str = "main",
    head: str = "o/r",
) -> dict[str, Any]:
    return {"number": number, "merged_at": at, "body": f"{first}\r\n\nThe API.", "user": int(user), "base": base, "head": head}


def issue(
    state: str = "open", labels: Iterable[str] = ("claude:full", "claude:ui"), title: str = "Board tasks", pr: bool = False,
) -> dict[str, Any]:
    return {"state": state, "title": title, "pr": pr, "labels": list(labels)}


def test_a_merged_part_starts_the_next_on_its_issue(env: dict[str, str]) -> None:
    gh = Gh(merged=[merged()], states={287: issue()})

    assert job_rebase.main(env, run=gh, sleep=no_sleep) == 0

    [call] = gh.asked()
    assert call[2] == "repos/o/r/issues/287/comments"
    body = call[4].removeprefix("body=")
    assert body.startswith("@claude continue\n")
    assert "<!-- claude:auto-next #401 -->" in body
    assert "part 2 starts by itself" in body


@pytest.mark.parametrize(
    "part",
    [
        merged(first="Part 3/3 of #287"),
        merged(first="Closes #287"),
        merged(first="Part of #287"),
        merged(at=None),
        merged(user=ME),
        merged(base="release"),
        merged(head="fork/r"),
    ],
    ids=["the last part", "the whole issue", "a part not yet numbered", "not merged", "someone else's", "to another branch", "from a fork"],
)
def test_only_a_merged_part_but_the_last_of_the_bots_own_starts_one(env: dict[str, str], part: dict[str, Any]) -> None:
    gh = Gh(merged=[part], states={287: issue()})

    assert job_rebase.main(env, run=gh, sleep=no_sleep) == 0

    assert gh.asked() == []


@pytest.mark.parametrize(
    "state",
    [issue(state="closed"), issue(labels=("claude:plan",)), issue(labels=()), issue(pr=True)],
    ids=["closed", "only planned", "no label", "a PR"],
)
def test_the_next_part_waits_without_an_open_labelled_issue(env: dict[str, str], state: dict[str, Any]) -> None:
    gh = Gh(merged=[merged()], states={287: state})

    assert job_rebase.main(env, run=gh, sleep=no_sleep) == 0

    assert gh.asked() == []


def test_the_next_part_waits_for_a_job_on_its_issue(env: dict[str, str]) -> None:
    gh = Gh(merged=[merged()], states={287: issue()}, running=["Board tasks"])

    assert job_rebase.main(env, run=gh, sleep=no_sleep) == 0

    assert gh.asked() == []


@pytest.mark.parametrize("first", ["Part 2/3 of #287", "Closes #287"])
def test_a_part_already_under_way_is_not_started_again(env: dict[str, str], first: str) -> None:
    gh = Gh([pr(9, draft=True, body=f"{first}\n\nThe page.")], merged=[merged()], states={287: issue()})

    assert job_rebase.main(env, run=gh, sleep=no_sleep) == 0

    assert gh.asked() == []


def test_each_merged_part_is_asked_for_once(env: dict[str, str]) -> None:
    gh = Gh(merged=[merged()], states={287: issue()}, nexts={287: ["false", "true"]})

    assert job_rebase.main(env, run=gh, sleep=no_sleep) == 0

    assert gh.asked() == []


def test_a_merged_part_needs_no_open_prs_to_be_seen(env: dict[str, str]) -> None:
    gh = Gh([], merged=[merged()], states={287: issue()})

    assert job_rebase.main(env, run=gh, sleep=no_sleep) == 0

    assert len(gh.asked()) == 1


def test_a_part_that_cannot_be_read_fails_the_step_after_the_rebases(env: dict[str, str]) -> None:
    gh = Gh([pr(7)], mergeable={7: "false"}, merged=[merged()], fails=["issues/287"])

    assert job_rebase.main(env, run=gh, sleep=no_sleep) == 1

    [call] = gh.asked()
    assert call[2] == "repos/o/r/issues/7/comments"


def test_a_conflicting_part_is_found_by_its_issues_title(env: dict[str, str]) -> None:
    gh = Gh([pr(7, body="Part 2/3 of #3")], mergeable={7: "false"}, running=["The issue"])

    assert job_rebase.main(env, run=gh, sleep=no_sleep) == 0

    assert gh.asked() == []
