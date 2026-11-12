#!/usr/bin/python3 -I
"""Keep a running cost report for an issue or PR, one row per Claude job.

The workflow's last step runs this in the job container after Claude has
finished, however it finished:

    claude-job-report --post

finds the comment on the job's PR, or on the issue while there is none,
rebuilds it and posts it back, and adds the new row to the job's summary.
It reads EXECUTION_FILE, NUMBER, BOT_LOGIN, GITHUB_REPOSITORY, RUNNER_TEMP
and GITHUB_STEP_SUMMARY, and LEVEL, OUTCOME and RUN_URL for the row; gh
finds its token in GH_TOKEN.

It reads the action's execution file, the full list of messages Claude Code
produced, for what the job used: tokens and API-price cost per model, turns,
time, and how often the lead called each role. The rows of earlier jobs come
from the existing comment, where they are kept as JSON in an HTML comment,
so the report is rebuilt rather than appended to.

A step killed at its time limit writes no execution file, and its row says
"unrecorded"; the first job has no comment yet, and makes one. A job in
parts, as claude-job-part runs a long one, has one file per part, the
earlier ones kept as claude-execution-output.part-<n>.json in RUNNER_TEMP,
and gets one row for them all.

What Claude Code's permissions refused is counted in the comment by tool
and program only, since the arguments carry paths on the box. The whole of
each refusal goes to REFUSED_LOG on the host instead, /cache being the
repository's dependency cache, which outlives the container, for reading
there as coder: `tail /home/coder/cache/<name>/claude-refused.jsonl`.

The cost is what the tokens would cost at API prices. On a subscription it
is not a bill, but it is the one number that compares runs, levels and
models with each other.
"""

import glob
import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from typing import Any

MARKER = "<!-- job-cost"
AGENT_TOOLS = ("Agent", "Task")
# A shell line in the rough, enough to name its programs.
QUOTED = re.compile(r"""'[^']*'|"[^"]*\"""")
PART = re.compile(r"&&|\|\||[;|\n]")
SETTING = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
PROGRAM = re.compile(r"[\w.+=-]+")
MAX_PROGRAMS = 4
REFUSED_LOG = "/cache/claude-refused.jsonl"
# The log's last lines kept, and of each refusal's input the first characters.
REFUSED_KEEP = 2000
REFUSED_CHARS = 500
# From where a count of tokens is written in thousands, and in millions.
THOUSAND = 1000
MILLION = 1_000_000


def load_messages(path: str) -> list[dict[str, Any]] | None:
    """The messages of an execution file; None when it is missing or not a list."""
    try:
        with open(path) as execution_file:
            messages = json.load(execution_file)
    except (OSError, ValueError):
        return None
    return messages if isinstance(messages, list) else None


def summarize(messages: list[dict[str, Any]] | None) -> dict[str, Any]:
    """What one job used, from its messages; None where nothing was recorded."""
    if messages is None:
        return {"turns": None, "duration_s": None, "cost_usd": None, "models": {}, "roles": {}, "refused": {}}
    roles: dict[str, int] = {}
    for message in messages:
        if message.get("type") != "assistant":
            continue
        for block in message.get("message", {}).get("content", []) or []:
            if block.get("type") == "tool_use" and block.get("name") in AGENT_TOOLS:
                role = (block.get("input") or {}).get("subagent_type") or "general"
                roles[role] = roles.get(role, 0) + 1
    result = next((message for message in reversed(messages) if message.get("type") == "result"), {})
    models = {
        model: {
            "in": usage.get("inputTokens", 0),
            "cache_read": usage.get("cacheReadInputTokens", 0),
            "cache_write": usage.get("cacheCreationInputTokens", 0),
            "out": usage.get("outputTokens", 0),
            "cost_usd": usage.get("costUSD", 0.0),
        }
        for model, usage in (result.get("modelUsage") or {}).items()
    }
    duration = result.get("duration_ms")
    return {
        "refused": refused(result.get("permission_denials") or []),
        "turns": result.get("num_turns"),
        "duration_s": None if duration is None else round(duration / 1000),
        "cost_usd": result.get("total_cost_usd"),
        "models": models,
        "roles": roles,
    }


def merge(parts: list[dict[str, Any]]) -> dict[str, Any]:
    """One job's figures from its parts': summed, and unrecorded only when
    no part was recorded.
    """
    recorded = [part for part in parts if part["cost_usd"] is not None]
    if not recorded:
        return summarize(None)
    if len(parts) == 1:
        return parts[0]
    total = summarize([])
    for part in recorded:
        for key in ("turns", "duration_s", "cost_usd"):
            total[key] = (total[key] or 0) + (part[key] or 0)
        for name in ("roles", "refused"):
            for key, count in part[name].items():
                total[name][key] = total[name].get(key, 0) + count
        for model, usage in part["models"].items():
            mine = total["models"].setdefault(model, dict.fromkeys(usage, 0))
            for key, value in usage.items():
                mine[key] = mine.get(key, 0) + value
    return total


def programs(command: str) -> str:
    """The program of each part of a command line, since `cd x && bun y` is
    refused for the bun and the cd alone says nothing.
    """
    command = QUOTED.sub("''", command.split("<<", maxsplit=1)[0])
    names: list[str] = []
    for part in PART.split(command):
        words = part.split()
        if not words:
            continue
        setting = SETTING.match(words[0])
        name = setting[0] if setting else os.path.basename(words[0])
        if PROGRAM.fullmatch(name) and name not in names:
            names.append(name)
    return " && ".join(names[:MAX_PROGRAMS]) or "?"


def refused(denials: list[dict[str, Any]]) -> dict[str, int]:
    """What Claude Code's permissions refused, by tool, and by the programs
    of the command for Bash. Never the arguments: they carry paths on the
    box, and the report is a comment on GitHub.
    """
    counts: dict[str, int] = {}
    for denial in denials:
        tool = denial.get("tool_name") or "?"
        if tool == "Bash":
            tool = f"Bash {programs(str((denial.get('tool_input') or {}).get('command', '')))}"
        counts[tool] = counts.get(tool, 0) + 1
    return counts


def keep_refusals(files: list[str], path: str, run_url: str, now: float | None = None) -> None:
    """Append each refusal in full to the host's log, keeping its last lines.
    Never fails the report: a log it cannot write is skipped.
    """
    date = time.strftime("%Y-%m-%d %H:%M", time.gmtime(now))
    lines = []
    for file in files:
        messages = load_messages(file) or []
        result = next(
            (message for message in reversed(messages) if isinstance(message, dict) and message.get("type") == "result"),  # ty: ignore[redundant-condition-strict] the file's JSON may hold anything
            {},
        )
        for denial in result.get("permission_denials") or []:
            tool_input = denial.get("tool_input") or {}
            what = tool_input.get("command") or tool_input.get("file_path") or json.dumps(tool_input)
            lines.append(json.dumps({"date": date, "run": run_url, "tool": denial.get("tool_name"), "input": str(what)[:REFUSED_CHARS]}))
    if not lines:
        return
    try:
        try:
            with open(path) as log_file:
                kept = log_file.read().splitlines()
        except FileNotFoundError:
            kept = []
        # Whole or not at all: the repository's other slots write it too.
        with open(f"{path}.{os.getpid()}", "w") as log_file:
            log_file.write("\n".join((kept + lines)[-REFUSED_KEEP:]) + "\n")
        os.replace(f"{path}.{os.getpid()}", path)
    except OSError as error:
        print(f"Could not keep the refusals in {path}: {error}", file=sys.stderr)


def earlier_runs(body: str) -> list[dict[str, Any]]:
    """The earlier jobs' rows, from the JSON the comment carries; none when it has none to read."""
    start = body.find(MARKER)
    if start < 0:
        return []
    end = body.find("-->", start)
    try:
        return json.loads(body[start + len(MARKER) : end])["runs"]
    except (ValueError, KeyError, TypeError):
        return []


def tokens(count: int | None) -> str:
    """A count of tokens in short: 850k, 1.2M, or a dash for none recorded."""
    if count is None:
        return "–"
    if count >= MILLION:
        return f"{count / MILLION:.1f}M"
    if count >= THOUSAND:
        return f"{round(count / THOUSAND)}k"
    return str(count)


def money(usd: float | None) -> str:
    """A cost in dollars, or a dash for none recorded."""
    return "–" if usd is None else f"${usd:.2f}"


def input_total(model: dict[str, int]) -> int:
    """Everything the model read: fresh input and cached, which is most of it."""
    return model["in"] + model["cache_read"] + model["cache_write"]


def render(runs: list[dict[str, Any]]) -> str:
    """The comment for these jobs: their rows as JSON in an HTML comment, then the folded tables."""
    total_cost = sum(run["cost_usd"] or 0 for run in runs)
    total_in = sum(input_total(model) for run in runs for model in run["models"].values())
    total_out = sum(model["out"] for run in runs for model in run["models"].values())
    unrecorded = sum(1 for run in runs if run["cost_usd"] is None)

    # ">" only ever appears inside the JSON's strings, so escaping it keeps
    # the JSON valid and the HTML comment from ending early.
    data = json.dumps({"runs": runs}).replace(">", "\\u003e")
    lines = [
        f"{MARKER} {data} -->",
        "<details><summary>Cost: "
        f"{len(runs)} job{'s' if len(runs) != 1 else ''}, {money(total_cost)} at API prices, "
        f"{tokens(total_in)} tokens in, {tokens(total_out)} out"
        + (f", {unrecorded} unrecorded" if unrecorded else "")
        + "</summary>",
        "",
        "| Job | Level | Outcome | Turns | Time | In | Out | API price | Roles called |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for job_number, run in enumerate(runs, 1):
        roles = ", ".join(f"{role} ×{count}" for role, count in sorted(run["roles"].items())) or "–"
        minutes = "–" if run["duration_s"] is None else f"{round(run['duration_s'] / 60)} min"
        lines.append(
            f"| [{job_number}]({run['run_url']}) | {run['level']} | {run['outcome']} | {run['turns'] if run['turns'] is not None else '–'} "
            f"| {minutes} | {tokens(sum(input_total(model) for model in run['models'].values()) if run['models'] else None)} "
            f"| {tokens(sum(model['out'] for model in run['models'].values()) if run['models'] else None)} "
            f"| {money(run['cost_usd'])} | {roles} |",
        )

    models: dict[str, dict[str, Any]] = {}
    for run in runs:
        for name, model in run["models"].items():
            into = models.setdefault(name, {"in": 0, "cache_read": 0, "cache_write": 0, "out": 0, "cost_usd": 0.0})
            for key in into:
                into[key] += model[key]
    if models:
        lines += [
            "",
            "By model, all jobs:",
            "",
            "| Model | In | Of which cached | Out | API price |",
            "| --- | --- | --- | --- | --- |",
        ]
        for name, model in sorted(models.items(), key=lambda item: -item[1]["cost_usd"]):
            lines.append(
                f"| {name} | {tokens(input_total(model))} | {tokens(model['cache_read'])} | {tokens(model['out'])} "
                f"| {money(model['cost_usd'])} |",
            )
    for job_number, run in enumerate(runs, 1):
        if run.get("refused"):
            listed = ", ".join(f"`{tool}` ×{count}" for tool, count in sorted(run["refused"].items()))
            lines += ["", f"Refused by Claude Code's permissions in job {job_number}: {listed}."]
    if unrecorded:
        lines += ["", "An unrecorded job was cut off before Claude Code could write down what it used."]
    lines += ["", "</details>"]
    return "\n".join(lines) + "\n"


def report(execution_file: str | list[str], body: str, env: Mapping[str, str], now: float | None) -> str:
    """The comment again, with this job's row added to the earlier ones in body."""
    files = [execution_file] if isinstance(execution_file, str) else execution_file
    this_run = {
        "date": time.strftime("%Y-%m-%d %H:%M", time.gmtime(now)),
        "run_url": env.get("RUN_URL", ""),
        "level": env.get("LEVEL", ""),
        "outcome": env.get("OUTCOME", ""),
        **merge([summarize(load_messages(path) if path else None) for path in files or [""]]),
    }
    return render([*earlier_runs(body), this_run])


def part_number(path: str) -> int:
    """Which part an execution file is of, 0 for one that names none."""
    found = re.search(r"part-(\d+)\.json$", path)
    return int(found.group(1)) if found else 0


def sh(*argv: str) -> str:
    """Run a command and give what it printed; a failure raises."""
    return subprocess.run(argv, check=True, capture_output=True, text=True).stdout.strip()


def post(env: Mapping[str, str], now: float | None = None, run: Callable[..., str] = sh) -> int:
    """Write the report over the bot's cost comment on the job's PR or issue, or as a new one."""
    repo = env["GITHUB_REPOSITORY"]
    branch = run("git", "rev-parse", "--abbrev-ref", "HEAD")
    target = run("gh", "pr", "list", "--head", branch, "--state", "all", "--json", "number", "--jq", ".[0].number // empty")
    target = target or env["NUMBER"]
    mine = f'.[] | select(.user.login == "{env["BOT_LOGIN"]}" and (.body | startswith("{MARKER}"))) | .id'
    ids = run("gh", "api", f"repos/{repo}/issues/{target}/comments", "--paginate", "--jq", mine).split()
    comment = ids[-1] if ids else ""
    body = run("gh", "api", f"repos/{repo}/issues/comments/{comment}", "--jq", ".body") if comment else ""
    temp = env.get("RUNNER_TEMP", "")
    execution_file = env.get("EXECUTION_FILE") or os.path.join(temp, "claude-execution-output.json")
    earlier = sorted(glob.glob(os.path.join(glob.escape(temp), "claude-execution-output.part-*.json")) if temp else [], key=part_number)
    new = report([*earlier, execution_file], body, env, now)
    log = env.get("REFUSED_LOG") or (REFUSED_LOG if os.path.isdir(os.path.dirname(REFUSED_LOG)) else "")
    if log:
        keep_refusals([*earlier, execution_file], log, env.get("RUN_URL", ""), now)
    if env.get("GITHUB_STEP_SUMMARY", ""):
        with open(env["GITHUB_STEP_SUMMARY"], "a") as summary_file:
            summary_file.write(new.split("\n", 1)[1])
    if comment:
        run("gh", "api", "--method", "PATCH", f"repos/{repo}/issues/comments/{comment}", "-f", f"body={new}")
    else:
        run("gh", "api", f"repos/{repo}/issues/{target}/comments", "-f", f"body={new}")
    return 0


def main(argv: list[str], env: Mapping[str, str] = os.environ, now: float | None = None) -> int:
    """Post the report for --post; anything else gets the usage and 2."""
    if argv != ["--post"]:
        print("usage: claude-job-report --post", file=sys.stderr)
        return 2
    return post(env, now)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
