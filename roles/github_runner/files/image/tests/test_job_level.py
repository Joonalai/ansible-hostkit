import json
from pathlib import Path
from typing import Any

import job_level
import pytest


def on(labels: list[str], body: str | None = None, review: str | None = None, pr: bool = False) -> dict[str, Any]:
    item = {"labels": [{"name": name} for name in labels]}
    event: dict[str, Any] = {"pull_request" if pr else "issue": item}
    if body is not None:
        event["comment"] = {"body": body}
    if review is not None:
        event["review"] = {"body": review}
    return event


def test_the_highest_label_wins() -> None:
    assert job_level.levels(on(["claude:plan", "claude:full", "bug"])) == {
        "level": "claude:full",
        "label-level": "claude:full",
        "asked": "",
        "note": "",
        "minutes": "100",
    }
    assert job_level.levels(on(["claude:plan", "claude"]))["level"] == "claude"
    assert job_level.levels(on(["claude:plan"]))["minutes"] == "20"


def test_no_label_is_the_plain_level() -> None:
    assert job_level.levels(on([], body="@claude why?")) == {
        "level": "claude",
        "label-level": "claude",
        "asked": "",
        "note": "",
        "minutes": "40",
    }


def test_a_plain_comment_keeps_the_labels_level() -> None:
    assert job_level.levels(on(["claude:full"], body="@claude why X?"))["level"] == "claude:full"


def test_quick_runs_the_lead_alone_for_that_comment() -> None:
    assert job_level.levels(on(["claude:full"], body="hey\n@Claude  QUICK: rename it")) == {
        "level": "claude",
        "label-level": "claude:full",
        "asked": "quick",
        "note": ", asked for with quick: in this comment, while the labels say claude:full",
        "minutes": "30",
    }


def test_full_brings_in_the_team_on_a_lighter_pr() -> None:
    out = job_level.levels(on(["claude"], body="@claude full: continue", pr=True))
    assert (out["level"], out["label-level"], out["minutes"]) == ("claude:full", "claude", "100")


def test_a_review_body_can_ask_too() -> None:
    assert job_level.levels(on(["claude:review"], review="@claude quick: squash", pr=True))["level"] == "claude"


def test_ui_adds_the_walkthrough_and_its_time_to_a_team_level() -> None:
    assert job_level.levels(on(["claude:full", "claude:ui"])) == {
        "level": "claude:full",
        "label-level": "claude:full",
        "asked": "",
        "note": ", with the UI walkthrough: the labels include claude:ui",
        "minutes": "120",
    }
    assert job_level.levels(on(["claude:ui", "claude:review"]))["minutes"] == "80"


def test_ui_alone_or_with_the_lead_alone_changes_nothing() -> None:
    assert job_level.levels(on(["claude:ui"])) == job_level.levels(on([]))
    assert job_level.levels(on(["claude", "claude:ui"]))["minutes"] == "40"
    assert job_level.levels(on(["claude:plan", "claude:ui"]))["note"] == ""


def test_ui_follows_the_level_the_comment_chose() -> None:
    quick = job_level.levels(on(["claude:full", "claude:ui"], body="@claude quick: rename it"))
    assert (quick["level"], quick["minutes"]) == ("claude", "30")
    assert "walkthrough" not in quick["note"]
    full = job_level.levels(on(["claude", "claude:ui"], body="@claude full: continue", pr=True))
    assert (full["level"], full["minutes"]) == ("claude:full", "120")
    assert full["note"].endswith("with the UI walkthrough: the labels include claude:ui")


def test_the_word_must_follow_the_mention() -> None:
    assert job_level.levels(on(["claude:full"], body="@claude do it quick: please"))["level"] == "claude:full"


def test_outputs_are_appended_with_the_stop_time(tmp_path: Path) -> None:
    event, output = tmp_path / "event.json", tmp_path / "output"
    event.write_text(json.dumps(on(["claude"], body="@claude quick: $(touch pwned)")))
    output.write_text("earlier=1\n")
    env = {"GITHUB_EVENT_PATH": str(event), "GITHUB_OUTPUT": str(output)}

    assert job_level.main(env, now=0) == 0

    assert output.read_text().splitlines() == [
        "earlier=1",
        "level=claude",
        "label-level=claude",
        "asked=quick",
        "note=, asked for with quick: in this comment, while the labels say claude",
        "minutes=30",
        "started=0",
        "job-stop-by=00:20",
        "part-minutes=30",
        "stop-by=00:20",
        "stop=Stop by 00:20 UTC.",
    ]
    assert not (tmp_path / "pwned").exists()


def test_the_related_repositories_go_in_the_note() -> None:
    assert job_level.levels(on(["claude"]), ["blog", "car-taker"])["note"] == (
        ", and this repository's related repositories are blog, car-taker"
    )


def test_no_related_repositories_add_nothing() -> None:
    assert job_level.levels(on(["claude"]), [])["note"] == ""


def test_what_is_no_name_never_reaches_the_lead_s_prompt() -> None:
    assert job_level.related({"CLAUDE_RELATED_REPOS": 'a, b"; rm -rf x, ../c,..'}) == ["a"]
    assert job_level.related({}) == []


def test_full_in_a_comment_tells_the_lead_to_call_every_role() -> None:
    assert "call every role" in job_level.levels(on(["claude"], body="@claude full: adopt it"))["note"]


def test_a_level_longer_than_a_token_comes_in_parts() -> None:
    outputs = job_level.parts(120, now=0)

    assert outputs["part-minutes"] == "52"
    assert outputs["stop-by"] == "00:45"
    assert outputs["job-stop-by"] == "01:50"
    assert "claude-job-part continue" in outputs["stop"]
    assert "00:45" in outputs["stop"] and "01:50" in outputs["stop"]


def test_a_level_inside_a_token_is_one_part() -> None:
    outputs = job_level.parts(40, now=0)

    assert outputs["part-minutes"] == "40"
    assert outputs["stop-by"] == outputs["job-stop-by"] == "00:30"
    assert outputs["stop"] == "Stop by 00:30 UTC."


def test_review_runs_the_engineer_and_reviewer_for_a_handed_on_rebase() -> None:
    found = job_level.levels(on(["claude"], body="@claude review: rebase\n\n<!-- claude:auto-escalate abc -->"))

    assert (found["level"], found["asked"], found["minutes"]) == ("claude:review", "review", "60")
    assert found["label-level"] == "claude"


def test_the_bots_name_is_the_units(monkeypatch: pytest.MonkeyPatch) -> None:
    import importlib

    monkeypatch.setenv("CLAUDE_BOT", "helper")
    try:
        named = importlib.reload(job_level)
        assert named.UI == "helper:ui"
        assert named.ASKING.search("@helper full: go on")
        assert not named.ASKING.search("@claude full: go on")
        monkeypatch.setenv("CLAUDE_BOT", "not a name")
        with pytest.raises(SystemExit):
            importlib.reload(job_level)
    finally:
        monkeypatch.delenv("CLAUDE_BOT")
        importlib.reload(job_level)
