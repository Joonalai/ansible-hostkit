import json
import os
from pathlib import Path

import job_part

HOUR = 3600
LEAD = "9425aa81-ab23-4bd7-9328-c2b47f96fc91"


def transcript(tmp_path: Path, name: str, mtime: int) -> Path:
    projects = tmp_path / "home" / ".claude" / "projects" / "-home-runner-_work-x-x"
    projects.mkdir(parents=True, exist_ok=True)
    path = projects / f"{name}.jsonl"
    path.write_text("{}\n")
    os.utime(path, (mtime, mtime))
    return path


def env(tmp_path: Path, outcome: str = "success", minutes: int = 120, part: int = 2) -> dict[str, str]:
    if not (tmp_path / "home").exists():
        transcript(tmp_path, LEAD, 100)
    return {
        "HOME": str(tmp_path / "home"),
        "RUNNER_TEMP": str(tmp_path),
        "STARTED": "0",
        "MINUTES": str(minutes),
        "OUTCOME": outcome,
        "PART": str(part),
        "GITHUB_OUTPUT": str(tmp_path / "output"),
    }


def outputs(tmp_path: Path) -> dict[str, str]:
    return dict(line.split("=", 1) for line in (tmp_path / "output").read_text().splitlines())


def test_the_lead_asking_and_time_left_give_another_part(tmp_path: Path) -> None:
    job_part.main(["continue"], env(tmp_path))

    assert job_part.main(["next"], env(tmp_path), now=46 * 60) == 0

    got = outputs(tmp_path)
    assert got["continue"] == "true"
    assert got["part-minutes"] == "52"
    assert got["stop-by"] == "01:31"
    assert "01:50" in got["stop"]
    assert got["session"] == LEAD
    assert not (tmp_path / job_part.MARKER).exists()


def test_without_asking_there_is_no_next_part(tmp_path: Path) -> None:
    job_part.main(["next"], env(tmp_path), now=46 * 60)

    assert outputs(tmp_path) == {"continue": "false"}


def test_a_part_that_failed_before_its_limit_is_not_resumed(tmp_path: Path) -> None:
    job_part.main(["continue"], env(tmp_path))

    job_part.main(["next"], env(tmp_path, outcome="failure"), now=30 * 60)

    assert outputs(tmp_path) == {"continue": "false"}


def test_a_first_part_cut_off_at_its_limit_goes_on_unasked(tmp_path: Path) -> None:
    job_part.main(["next"], env(tmp_path, outcome="failure"), now=52 * 60)

    got = outputs(tmp_path)
    assert got["continue"] == "true"
    assert got["session"] == LEAD


def test_a_part_github_cancelled_at_its_limit_goes_on_unasked(tmp_path: Path) -> None:
    job_part.main(["next"], env(tmp_path, outcome="cancelled"), now=52 * 60)

    assert outputs(tmp_path)["continue"] == "true"


def test_a_part_cancelled_before_its_limit_is_not_resumed(tmp_path: Path) -> None:
    job_part.main(["next"], env(tmp_path, outcome="cancelled"), now=30 * 60)

    assert outputs(tmp_path) == {"continue": "false"}


def test_a_later_part_cut_off_at_its_recorded_limit_goes_on(tmp_path: Path) -> None:
    job_part.main(["continue"], env(tmp_path))
    job_part.main(["next"], env(tmp_path), now=38 * 60)
    assert float((tmp_path / job_part.ENDS).read_text()) == 38 * 60 + 52 * 60
    (tmp_path / "output").unlink()

    job_part.main(["next"], env(tmp_path, outcome="failure", part=3), now=90 * 60)

    got = outputs(tmp_path)
    assert got["continue"] == "true"
    assert got["part-minutes"] == "30"


def test_a_later_part_that_failed_early_is_not_taken_for_cut_off(tmp_path: Path) -> None:
    job_part.main(["continue"], env(tmp_path))
    job_part.main(["next"], env(tmp_path), now=38 * 60)
    (tmp_path / "output").unlink()

    job_part.main(["next"], env(tmp_path, outcome="failure", part=3), now=60 * 60)

    assert outputs(tmp_path) == {"continue": "false"}


def test_without_a_transcript_there_is_nothing_to_resume(tmp_path: Path) -> None:
    (tmp_path / "home").mkdir()
    job_part.main(["continue"], env(tmp_path))

    job_part.main(["next"], env(tmp_path), now=46 * 60)

    assert outputs(tmp_path) == {"continue": "false"}


def test_the_newest_lead_transcript_is_resumed_not_a_subagents(tmp_path: Path) -> None:
    newer = "0e5c2a10-1111-4222-8333-444455556666"
    transcript(tmp_path, LEAD, 100)
    transcript(tmp_path, newer, 200)
    transcript(tmp_path, "agent-0e5c2a10", 300)
    job_part.main(["continue"], env(tmp_path))

    job_part.main(["next"], env(tmp_path), now=46 * 60)

    assert outputs(tmp_path)["session"] == newer


def test_too_little_time_left_ends_the_job(tmp_path: Path) -> None:
    job_part.main(["continue"], env(tmp_path))

    job_part.main(["next"], env(tmp_path), now=HOUR + 45 * 60)

    assert outputs(tmp_path) == {"continue": "false"}


def test_the_last_part_stops_by_the_jobs_time(tmp_path: Path) -> None:
    job_part.main(["continue"], env(tmp_path, part=3))

    job_part.main(["next"], env(tmp_path, part=3), now=HOUR + 32 * 60)

    got = outputs(tmp_path)
    assert got["continue"] == "true"
    assert got["stop-by"] == "01:50"
    assert got["stop"] == "Stop by 01:50 UTC."
    assert got["part-minutes"] == "28"


def test_the_parts_execution_file_is_kept_for_the_report(tmp_path: Path) -> None:
    (tmp_path / job_part.EXECUTION).write_text(json.dumps([{"type": "result"}]))
    job_part.main(["continue"], env(tmp_path))

    job_part.main(["next"], env(tmp_path, part=2), now=46 * 60)

    assert json.loads((tmp_path / "claude-execution-output.part-1.json").read_text()) == [{"type": "result"}]
    assert not (tmp_path / job_part.EXECUTION).exists()


def test_a_cut_off_part_with_no_file_does_not_count_the_one_before_twice(tmp_path: Path) -> None:
    (tmp_path / job_part.EXECUTION).write_text("[]")
    job_part.main(["continue"], env(tmp_path))
    job_part.main(["next"], env(tmp_path), now=38 * 60)

    job_part.main(["next"], env(tmp_path, outcome="failure", part=3), now=90 * 60)

    assert sorted(p.name for p in tmp_path.glob("claude-execution-output*")) == ["claude-execution-output.part-1.json"]


def test_the_last_parts_file_is_not_copied_to_be_counted_twice(tmp_path: Path) -> None:
    (tmp_path / job_part.EXECUTION).write_text("[]")

    job_part.main(["next"], env(tmp_path, part=2), now=46 * 60)

    assert not (tmp_path / "claude-execution-output.part-1.json").exists()


def test_anything_else_is_refused(tmp_path: Path) -> None:
    assert job_part.main(["now"], env(tmp_path)) == 2
