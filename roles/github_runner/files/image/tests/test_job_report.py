import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import job_report
import pytest


def result(**extra: object) -> dict[str, Any]:
    return {
        "type": "result",
        "subtype": "success",
        "num_turns": 42,
        "duration_ms": 1_080_000,
        "total_cost_usd": 2.5,
        "modelUsage": {
            "claude-sonnet-5-5": {
                "inputTokens": 10_000,
                "cacheReadInputTokens": 700_000,
                "cacheCreationInputTokens": 90_000,
                "outputTokens": 15_000,
                "costUSD": 1.5,
            },
            "claude-opus-5-5": {
                "inputTokens": 2_000,
                "cacheReadInputTokens": 40_000,
                "cacheCreationInputTokens": 8_000,
                "outputTokens": 3_000,
                "costUSD": 1.0,
            },
        },
        **extra,
    }


def call(role: str, name: str = "Agent") -> dict[str, Any]:
    return {
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": "…"}, {"type": "tool_use", "name": name, "input": {"subagent_type": role}}]},
    }


MESSAGES = [
    {"type": "system", "subtype": "init"},
    call("engineer"),
    call("reviewer"),
    call("engineer", name="Task"),
    call("reviewer"),
    {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}]}},
    result(),
]


def test_a_job_is_summed_up_from_its_result_and_its_role_calls() -> None:
    summary = job_report.summarize(MESSAGES)

    assert summary["turns"] == 42
    assert summary["duration_s"] == 1080
    assert summary["cost_usd"] == 2.5
    assert summary["roles"] == {"engineer": 2, "reviewer": 2}
    assert summary["models"]["claude-opus-5-5"] == {
        "in": 2000,
        "cache_read": 40_000,
        "cache_write": 8000,
        "out": 3000,
        "cost_usd": 1.0,
    }


def test_a_job_with_no_execution_file_is_recorded_as_unrecorded(tmp_path: Path) -> None:
    summary = job_report.summarize(job_report.load_messages(str(tmp_path / "missing.json")))

    assert summary == {"turns": None, "duration_s": None, "cost_usd": None, "models": {}, "roles": {}, "refused": {}}


def test_a_garbled_execution_file_counts_as_missing(tmp_path: Path) -> None:
    path = tmp_path / "out.json"
    path.write_text('[{"type": "resu')

    assert job_report.load_messages(str(path)) is None


def run_main(
    tmp_path: Path,
    messages: list[dict[str, Any]] | None,
    comment: str,
    outcome: str = "success",
    url: str = "https://github.com/o/r/actions/runs/1",
    capsys: pytest.CaptureFixture[str] | None = None,
) -> str:
    """The comment a job rebuilds from its execution file and the existing comment."""
    execution = tmp_path / "execution.json"
    if messages is None:
        execution.unlink(missing_ok=True)
    else:
        execution.write_text(json.dumps(messages))
    env = {"RUN_URL": url, "LEVEL": "claude:review", "OUTCOME": outcome}
    return job_report.report(str(execution), comment, env, now=0)


def test_the_first_job_makes_a_folded_report(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = run_main(tmp_path, MESSAGES, "", capsys=capsys)

    assert "<details><summary>Cost: 1 job, $2.50 at API prices, 850k tokens in, 18k out</summary>" in out
    row = (
        "| [1](https://github.com/o/r/actions/runs/1) | claude:review | success | 42 | 18 min | 850k | 18k | $2.50 "
        "| engineer ×2, reviewer ×2 |"
    )
    assert row in out
    assert out.index("| claude-sonnet-5-5 | 800k | 700k | 15k | $1.50 |") < out.index("| claude-opus-5-5 |")
    assert out.rstrip().endswith("</details>")


def test_later_jobs_add_a_row_and_the_totals_follow(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    first = run_main(tmp_path, MESSAGES, "", capsys=capsys)
    second = run_main(tmp_path, None, first, outcome="failure", url="https://github.com/o/r/actions/runs/2", capsys=capsys)

    assert "Cost: 2 jobs, $2.50 at API prices, 850k tokens in, 18k out, 1 unrecorded</summary>" in second
    assert "| [2](https://github.com/o/r/actions/runs/2) | claude:review | failure | – | – | – | – | – | – |" in second
    assert "cut off before Claude Code could write down" in second
    assert len(job_report.earlier_runs(second)) == 2


def test_the_stored_runs_cannot_end_the_html_comment(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = run_main(tmp_path, MESSAGES, "", url="https://example.com/?a=-->", capsys=capsys)

    first_line = out.splitlines()[0]
    assert first_line.endswith(" -->") and first_line.count("-->") == 1
    assert job_report.earlier_runs(out)[0]["run_url"] == "https://example.com/?a=-->"


def test_a_comment_without_a_report_starts_a_new_one() -> None:
    assert job_report.earlier_runs("Some other comment") == []
    assert job_report.earlier_runs("<!-- job-cost not json -->") == []


def test_anything_but_two_arguments_is_a_usage_error() -> None:
    assert job_report.main(["only-one"]) == 2


DENIALS = [
    {"tool_name": "Write", "tool_use_id": "1", "tool_input": {"file_path": "/home/coder/previews/shop-3/compose.yaml"}},
    {"tool_name": "Write", "tool_use_id": "2", "tool_input": {"file_path": "/home/coder/previews/shop-3/.env.test"}},
    {"tool_name": "Bash", "tool_use_id": "3", "tool_input": {"command": "/usr/bin/cat > /home/coder/previews/shop-3/x"}},
    {"tool_name": "Bash", "tool_use_id": "4", "tool_input": {}},
]


def test_refusals_are_counted_by_tool_and_command_without_their_arguments(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert job_report.summarize([*MESSAGES[:-1], result(permission_denials=DENIALS)])["refused"] == {
        "Write": 2,
        "Bash cat": 1,
        "Bash ?": 1,
    }

    out = run_main(tmp_path, [*MESSAGES[:-1], result(permission_denials=DENIALS)], "", capsys=capsys)
    assert "Refused by Claude Code's permissions in job 1: `Bash ?` ×1, `Bash cat` ×1, `Write` ×2." in out
    assert "/home/coder" not in out


@pytest.mark.parametrize(
    ("command", "named"),
    [
        ("cd /box/secret && bun run x", "cd && bun"),
        ('GIT_EDITOR="sed -i x" git rebase', "GIT_EDITOR="),
        ('git commit -m "a; rm b"', "git"),
        ("cat > /tmp/f <<EOF\nhello; world\nEOF", "cat"),
        ("/usr/bin/tsc -p . | tail; a; b; c; d", "tsc && tail && a && b"),
        ("", "?"),
    ],
)
def test_a_refused_command_names_its_programs_and_no_arguments(command: str, named: str) -> None:
    assert job_report.programs(command) == named


def test_a_job_with_nothing_refused_says_nothing_about_it(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert "Refused" not in run_main(tmp_path, MESSAGES, "", capsys=capsys)


class Gh(list[tuple[str, ...]]):
    """A recorded git and gh: a PR or none, and the bot's comments on it."""

    def __init__(self, pr: str = "", comments: Mapping[str, str] | None = None) -> None:
        super().__init__()
        self.pr = pr
        self.comments: dict[str, str] = dict(comments or {})

    def __call__(self, *argv: str) -> str:
        self.append(argv)
        if argv[0] == "git":
            return "feat/3-thing"
        if argv[1:3] == ("pr", "list"):
            return self.pr
        if argv[1] == "api" and argv[2].endswith("/comments") and "-f" not in argv:
            return "\n".join(self.comments)
        if argv[1] == "api" and "--jq" in argv:
            return self.comments[argv[2].rsplit("/", 1)[1]]
        return ""


def post_env(tmp_path: Path, messages: list[dict[str, Any]]) -> dict[str, str]:
    execution = tmp_path / "execution.json"
    execution.write_text(json.dumps(messages))
    return {
        "GITHUB_REPOSITORY": "o/r",
        "NUMBER": "3",
        "BOT_LOGIN": "claude[bot]",
        "EXECUTION_FILE": str(execution),
        "GITHUB_STEP_SUMMARY": str(tmp_path / "summary.md"),
        "RUN_URL": "https://github.com/o/r/actions/runs/2",
        "LEVEL": "claude",
        "OUTCOME": "success",
    }


def test_post_adds_a_comment_to_the_pr_when_there_is_none(tmp_path: Path) -> None:
    gh = Gh(pr="7")

    assert job_report.post(post_env(tmp_path, MESSAGES), now=0, run=gh) == 0

    (created,) = [call for call in gh if "-f" in call]
    assert created[:3] == ("gh", "api", "repos/o/r/issues/7/comments")
    assert created[-1].startswith("body=" + job_report.MARKER)
    summary = (tmp_path / "summary.md").read_text()
    assert summary.startswith("<details>") and job_report.MARKER not in summary


def test_post_rebuilds_the_latest_comment_on_the_issue_without_a_pr(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    earlier = run_main(tmp_path, MESSAGES, "", capsys=capsys)
    gh = Gh(comments={"11": "old", "12": earlier})

    job_report.post(post_env(tmp_path, MESSAGES), now=0, run=gh)

    (listed,) = [call for call in gh if call[2] == "repos/o/r/issues/3/comments"]
    assert 'select(.user.login == "claude[bot]"' in listed[-1]
    (patched,) = [call for call in gh if "PATCH" in call]
    assert patched[4] == "repos/o/r/issues/comments/12"
    assert "Cost: 2 jobs" in patched[-1]


def test_post_is_the_whole_command_line() -> None:
    assert job_report.main([], {}) == 2
    assert job_report.main(["--post", "extra"], {}) == 2


def test_a_job_in_parts_is_one_row_with_the_parts_summed() -> None:
    first, second = job_report.summarize(MESSAGES), job_report.summarize([call("tester"), result(num_turns=8)])

    total = job_report.merge([first, second, job_report.summarize(None)])

    assert total["turns"] == 50
    assert total["cost_usd"] == 5.0
    assert total["duration_s"] == 2160
    assert total["roles"] == {"engineer": 2, "reviewer": 2, "tester": 1}
    assert total["models"]["claude-opus-5-5"]["out"] == 6000


def test_post_reads_the_earlier_parts_beside_the_last(tmp_path: Path) -> None:
    (tmp_path / "claude-execution-output.part-1.json").write_text(json.dumps(MESSAGES))
    env = post_env(tmp_path, MESSAGES) | {"RUNNER_TEMP": str(tmp_path)}
    gh = Gh(pr="7")

    job_report.post(env, now=0, run=gh)

    (created,) = [call for call in gh if "-f" in call]
    assert '"cost_usd": 5.0' in created[-1]


def test_refusals_are_kept_whole_in_the_hosts_log(tmp_path: Path) -> None:
    log = tmp_path / "refused.jsonl"
    log.write_text('{"old": 1}\n')
    env = post_env(tmp_path, [*MESSAGES[:-1], result(permission_denials=DENIALS)]) | {"REFUSED_LOG": str(log)}

    job_report.post(env, now=0, run=Gh(pr="7"))

    old, *kept = [json.loads(line) for line in log.read_text().splitlines()]
    assert old == {"old": 1}
    assert [(k["tool"], k["input"]) for k in kept] == [
        ("Write", "/home/coder/previews/shop-3/compose.yaml"),
        ("Write", "/home/coder/previews/shop-3/.env.test"),
        ("Bash", "/usr/bin/cat > /home/coder/previews/shop-3/x"),
        ("Bash", "{}"),
    ]
    assert {k["run"] for k in kept} == {"https://github.com/o/r/actions/runs/2"}


def test_the_hosts_log_keeps_only_its_last_lines(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(job_report, "REFUSED_KEEP", 3)
    log = tmp_path / "refused.jsonl"
    log.write_text("1\n2\n3\n")
    execution = tmp_path / "execution.json"
    execution.write_text(json.dumps([*MESSAGES[:-1], result(permission_denials=DENIALS[:2])]))

    job_report.keep_refusals([str(execution)], str(log), "url", now=0)

    assert log.read_text().splitlines()[0] == "3"
    assert len(log.read_text().splitlines()) == 3


def test_a_log_it_cannot_write_does_not_stop_the_report(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    messages = [*MESSAGES[:-1], result(permission_denials=DENIALS)]
    env = post_env(tmp_path, messages) | {"REFUSED_LOG": str(tmp_path / "missing" / "x.jsonl")}
    gh = Gh(pr="7")

    assert job_report.post(env, now=0, run=gh) == 0
    assert [call for call in gh if "-f" in call]
    assert "Could not keep the refusals" in capsys.readouterr().err
