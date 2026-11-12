import io
import json
import subprocess
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any, override

import job_vouch
import pytest

OWNER, STRANGER, BOT = 33314057, 555, 777
ALLOWED = {OWNER}


def actor(user_id: int | None) -> dict[str, Any] | None:
    return None if user_id is None else {"login": f"u{user_id}", "databaseId": user_id}


def labelled(at: str, by: int | None = OWNER, name: str = "claude") -> dict[str, Any]:
    return {"__typename": "LabeledEvent", "createdAt": at, "actor": actor(by), "label": {"name": name}}


def renamed(at: str, by: int | None) -> dict[str, Any]:
    return {"__typename": "RenamedTitleEvent", "createdAt": at, "actor": actor(by)}


def item(
    by: int | None = OWNER,
    at: str = "2026-10-29T10:00:00Z",
    edits: Iterable[tuple[str, int | None]] = (),
    events: Iterable[dict[str, Any]] = (),
    kind: str = "Issue",
    number: int = 7,
    closes: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    found: dict[str, Any] = {
        "__typename": kind,
        "number": number,
        "createdAt": at,
        "author": actor(by),
        "userContentEdits": {"nodes": [{"editedAt": when, "editor": actor(who)} for when, who in edits]},
        "timelineItems": {"nodes": list(events)},
    }
    if closes is not None:
        found["closingIssuesReferences"] = {"nodes": closes}
    return found


def refusal(found: dict[str, Any]) -> str | None:
    return job_vouch.refusal(found, ALLOWED, BOT)


def test_your_own_issue_needs_no_label() -> None:
    assert refusal(item()) is None


def test_a_strangers_issue_is_worked_once_someone_allowed_labels_it() -> None:
    assert refusal(item(by=STRANGER, events=[labelled("2026-10-29T11:00:00Z")])) is None


def test_a_strangers_issue_with_no_label_is_refused() -> None:
    assert "nobody on it has labelled it" in (refusal(item(by=STRANGER)) or "")


def test_a_label_from_someone_off_the_list_vouches_for_nothing() -> None:
    assert refusal(item(by=STRANGER, events=[labelled("2026-10-29T11:00:00Z", by=STRANGER)])) is not None


def test_claude_proposed_vouches_for_nothing() -> None:
    assert refusal(item(by=STRANGER, events=[labelled("2026-10-29T11:00:00Z", name="claude:proposed")])) is not None


def test_an_edit_after_the_label_is_refused() -> None:
    found = item(by=STRANGER, edits=[("2026-10-29T12:00:00Z", STRANGER)], events=[labelled("2026-10-29T11:00:00Z")])
    assert "changed after it was labelled" in (refusal(found) or "")


def test_a_title_renamed_after_the_label_is_refused() -> None:
    found = item(by=STRANGER, events=[labelled("2026-10-29T11:00:00Z"), renamed("2026-10-29T12:00:00Z", STRANGER)])
    assert "changed after it was labelled" in (refusal(found) or "")


def test_an_edit_before_the_label_is_vouched_for() -> None:
    found = item(by=STRANGER, edits=[("2026-10-29T10:30:00Z", STRANGER)], events=[labelled("2026-10-29T11:00:00Z")])
    assert refusal(found) is None


def test_labelling_again_vouches_for_the_new_text() -> None:
    found = item(
        by=STRANGER,
        edits=[("2026-10-29T12:00:00Z", STRANGER)],
        events=[labelled("2026-10-29T11:00:00Z"), labelled("2026-10-29T13:00:00Z")],
    )
    assert refusal(found) is None


def test_allowed_people_and_the_bot_edit_freely() -> None:
    found = item(
        edits=[("2026-10-29T12:00:00Z", OWNER), ("2026-10-29T12:30:00Z", BOT)],
        events=[renamed("2026-10-29T13:00:00Z", OWNER)],
    )
    assert refusal(found) is None


def test_a_deleted_account_counts_as_off_the_list() -> None:
    found = item(edits=[("2026-10-29T12:00:00Z", None)], events=[labelled("2026-10-29T11:00:00Z")])
    assert refusal(found) is not None


def test_a_strangers_pull_request_is_refused_whatever_its_labels() -> None:
    found = item(by=STRANGER, kind="PullRequest", events=[labelled("2026-10-29T11:00:00Z")])
    assert "pull request from someone not" in (refusal(found) or "")


def test_the_bots_own_pull_request_is_worked() -> None:
    assert refusal(item(by=BOT, kind="PullRequest")) is None


class GitHub:
    """Stands in for gh: answers the query with one item, records the rest."""

    def __init__(self, found: dict[str, Any] | None = None, fails: bool = False) -> None:
        self.found = found if found is not None else item()
        self.fails = fails
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, *argv: str) -> str:
        self.calls.append(argv)
        if self.fails:
            raise subprocess.CalledProcessError(1, argv)
        if argv[:3] == ("gh", "api", "graphql"):
            return json.dumps({"data": {"repository": {"issueOrPullRequest": self.found}}})
        return ""


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"issue": {"number": 7}}))
    return {
        "GITHUB_EVENT_PATH": str(event),
        "GITHUB_REPOSITORY": "octo/shop",
        "CLAUDE_ALLOWED_IDS": json.dumps(sorted(ALLOWED)),
        "CLAUDE_APP_BOT_ID": str(BOT),
    }


def test_the_query_names_the_repository_and_the_number(env: dict[str, str]) -> None:
    github = GitHub()
    assert job_vouch.step(env, run=github) == 0
    argv = github.calls[0]
    assert "owner=octo" in argv and "name=shop" in argv and "number=7" in argv


def test_a_pull_request_is_checked_with_the_issues_it_closes(env: dict[str, str]) -> None:
    edited = item(by=STRANGER, number=3, edits=[("2026-10-29T12:00:00Z", STRANGER)], events=[labelled("2026-10-29T11:00:00Z")])
    github = GitHub(item(by=BOT, kind="PullRequest", closes=[edited]))

    assert job_vouch.step(env, run=github) == 1

    comment = github.calls[-1]
    assert comment[:3] == ("gh", "api", "repos/octo/shop/issues/7/comments")
    assert "#3 was changed after it was labelled" in comment[-1]


class Numbered(GitHub):
    """Answers each query with the item of the number it asks for."""

    def __init__(self, items: dict[int, dict[str, Any]]) -> None:
        super().__init__()
        self.items = items

    @override
    def __call__(self, *argv: str) -> str:
        self.calls.append(argv)
        if argv[:3] == ("gh", "api", "graphql"):
            number = int(next(arg for arg in argv if arg.startswith("number=")).split("=", 1)[1])
            return json.dumps({"data": {"repository": {"issueOrPullRequest": self.items[number]}}})
        return ""


def a_part(first: str = "Part 2/3 of #3") -> dict[str, Any]:
    found = item(by=BOT, kind="PullRequest", closes=[])
    found["body"] = f"{first}\r\n\nThe page."
    return found


EDITED = item(by=STRANGER, number=3, edits=[("2026-10-29T12:00:00Z", STRANGER)], events=[labelled("2026-10-29T11:00:00Z")])


@pytest.mark.parametrize("first", ["Part 2/3 of #3", "Part of #3"])
def test_a_part_is_checked_with_the_issue_it_is_a_part_of(env: dict[str, str], first: str) -> None:
    github = Numbered({7: a_part(first), 3: EDITED})

    assert job_vouch.step(env, run=github) == 1

    assert "#3 was changed after it was labelled" in github.calls[-1][-1]


def test_a_part_of_an_issue_that_still_says_what_was_vouched_for_is_worked(env: dict[str, str]) -> None:
    github = Numbered({7: a_part(), 3: item(number=3)})

    assert job_vouch.step(env, run=github) == 0


def test_a_part_line_anywhere_but_first_names_no_issue(env: dict[str, str]) -> None:
    found = a_part("Fixes the page.\nPart 2/3 of #3")
    github = Numbered({7: found, 3: EDITED})

    assert job_vouch.step(env, run=github) == 0
    assert len(github.calls) == 1


def test_a_step_passes_quietly(env: dict[str, str]) -> None:
    github = GitHub()
    assert job_vouch.step(env, run=github) == 0
    assert len(github.calls) == 1


@pytest.mark.parametrize("broken", ["api", "allow-list"])
def test_a_step_that_cannot_check_refuses(env: dict[str, str], broken: str, capsys: pytest.CaptureFixture[str]) -> None:
    if broken == "allow-list":
        del env["CLAUDE_ALLOWED_IDS"]
    assert job_vouch.step(env, run=GitHub(fails=broken == "api")) == 1
    assert "::error::Could not check" in capsys.readouterr().out


def hook(env: dict[str, str], command: str, github: GitHub) -> int:
    return job_vouch.hook(env, io.StringIO(json.dumps({"tool_input": {"command": command}})), run=github)


@pytest.mark.parametrize("command", ["ls -la", "uv run pytest", "git commit -m 'Add a'", "git status"])
def test_the_hook_lets_other_commands_through_without_asking_github(env: dict[str, str], command: str) -> None:
    github = GitHub(fails=True)
    assert hook(env, command, github) == 0
    assert github.calls == []


@pytest.mark.parametrize(
    "command",
    [
        "git push -u origin 7-x",
        "cd repo && git push",
        "git -C ../blog push origin feat/shop-7-x",
        "gh pr create --draft",
        "gh pr create -R o/blog --head feat/shop-7-x",
        "gh issue view 7",
        "gh api repos/x/pulls/1",
        "claude-job-pr-comments blog 5",
    ],
)
def test_the_hook_blocks_a_push_or_a_read_once_the_issue_changed(
    env: dict[str, str], command: str, capsys: pytest.CaptureFixture[str],
) -> None:
    changed = item(by=STRANGER, edits=[("2026-10-29T12:00:00Z", STRANGER)], events=[labelled("2026-10-29T11:00:00Z")])
    github = GitHub(changed)

    assert hook(env, command, github) == 2

    assert "Stop now" in capsys.readouterr().err
    assert all(call[:3] == ("gh", "api", "graphql") for call in github.calls)


def test_the_hook_lets_a_push_through_while_nothing_changed(env: dict[str, str]) -> None:
    assert hook(env, "git push", GitHub()) == 0


def test_the_hook_blocks_when_it_cannot_check(env: dict[str, str], capsys: pytest.CaptureFixture[str]) -> None:
    assert hook(env, "git push", GitHub(fails=True)) == 2
    assert "check of who changed the issue failed" in capsys.readouterr().err


@pytest.mark.parametrize(
    "command",
    [
        "gh pr comment 7 --body '@claude continue <!-- claude:auto-continue -->'",
        "echo '<!-- claude:auto-continue -->' > note.md",
        "gh pr comment 7 --body '@claude quick: rebase <!-- claude:auto-rebase abc -->'",
        "gh issue comment 3 --body '<!-- claude:auto-wait at=1 asked= -->'",
    ],
)
def test_the_hook_blocks_the_workflows_own_marks_whatever_the_command(
    env: dict[str, str], command: str, capsys: pytest.CaptureFixture[str],
) -> None:
    github = GitHub()

    assert hook(env, command, github) == 2

    assert "the workflow's own" in capsys.readouterr().err
    assert github.calls == []


class Thread(GitHub):
    """And the thread's comments, as the step's --jq prints them."""

    def __init__(self, comments: Sequence[tuple[int, bool]]) -> None:
        super().__init__()
        self.comments = comments

    @override
    def __call__(self, *argv: str) -> str:
        if "--paginate" in argv:
            self.calls.append(argv)
            return "\n".join(f"{user} {'true' if marked else 'false'}" for user, marked in self.comments)
        return super().__call__(*argv)


def started_by_the_bot(env: dict[str, str]) -> None:
    comment = {"user": {"id": BOT}, "body": "@claude continue\n\n<!-- claude:auto-continue -->"}
    with open(env["GITHUB_EVENT_PATH"], "w") as f:
        json.dump({"issue": {"number": 7}, "comment": comment}, f)


def test_the_bot_may_start_jobs_by_itself_up_to_the_cap(env: dict[str, str]) -> None:
    started_by_the_bot(env)
    comments = [(OWNER, False)] + [(BOT, True)] * job_vouch.MAX_UNASKED

    assert job_vouch.step(env, run=Thread(comments)) == 0


def test_past_the_cap_the_bots_own_job_is_refused(env: dict[str, str], capsys: pytest.CaptureFixture[str]) -> None:
    started_by_the_bot(env)
    github = Thread([(BOT, True)] * 5 + [(OWNER, False)] + [(BOT, True), (BOT, False)] * (job_vouch.MAX_UNASKED + 1))

    assert job_vouch.step(env, run=github) == 1
    assert f"started {job_vouch.MAX_UNASKED + 1} jobs on #7 by itself" in capsys.readouterr().out


def test_a_persons_comment_is_never_counted(env: dict[str, str]) -> None:
    github = Thread([(BOT, True)] * 50)

    assert job_vouch.step(env, run=github) == 0
    assert not [c for c in github.calls if "--paginate" in c]


def test_an_edit_past_the_first_hundred_is_still_read() -> None:
    found = item(by=STRANGER, edits=[("2026-10-29T10:30:00Z", STRANGER)], events=[labelled("2026-10-29T11:00:00Z")])
    assert refusal(found) is None
    found["newestEdits"] = {"nodes": [{"editedAt": "2026-10-29T12:00:00Z", "editor": actor(STRANGER)}]}
    assert "changed after it was labelled" in (refusal(found) or "")


def test_the_last_edit_counts_by_its_own_fields() -> None:
    found = item(by=STRANGER, events=[labelled("2026-10-29T11:00:00Z")])
    found |= {"lastEditedAt": "2026-10-29T12:00:00Z", "editor": actor(STRANGER)}
    assert "changed after it was labelled" in (refusal(found) or "")
    found |= {"editor": actor(OWNER)}
    assert refusal(found) is None


def test_a_label_that_only_begins_like_the_bots_vouches_for_nothing() -> None:
    assert refusal(item(by=STRANGER, events=[labelled("2026-10-29T11:00:00Z", name="claude-code")])) is not None
    assert refusal(item(by=STRANGER, events=[labelled("2026-10-29T11:00:00Z", name="claude:review")])) is None
