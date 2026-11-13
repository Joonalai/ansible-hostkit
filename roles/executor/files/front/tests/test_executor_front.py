import hashlib
import io
import json
import shutil
import socket
import ssl
import subprocess
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any, override

import executor_front
import pytest

KEY = "a-key-of-the-runners"


class Agent:
    """Stands in for the agent: records what it is asked, answers as told."""

    def __init__(self, stacks: dict[str, Any] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.bundles: list[bytes] = []
        self.stacks = stacks or {}
        self.up_exit = 0
        self.up_kept = False
        self.start_exit = 0

    def __call__(self, request: dict[str, Any], bundle: str | None) -> Iterator[dict[str, Any]]:
        self.calls.append(request)
        if bundle is not None:
            self.bundles.append(Path(bundle).read_bytes())
        if request["action"] == "up":
            yield {"out": "building"}
            yield {"exit": self.up_exit, "commit": "c" * 40, "page": 2} | ({"kept": True} if self.up_kept else {})
        elif request["action"] == "ps":
            yield {"exit": 0, "stacks": self.stacks}
        elif request["action"] == "stop":
            yield {"exit": 0, "stopped": 1}
        elif request["action"] == "start" and self.start_exit:
            yield {"exit": self.start_exit, "message": "a service did not run"}
        else:
            yield {"out": f"{request['action']} done"}
            yield {"exit": 0}

    def actions(self) -> list[tuple[str, str]]:
        return [(call["action"], call.get("caller", "")) for call in self.calls]


class Clock:
    def __init__(self) -> None:
        self.t = 1_000_000.0

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds


def register(tmp_path: Path, name: str, key: str = KEY, uid: int | None = 2001, waits: bool = True) -> None:
    callers = tmp_path / "callers"
    callers.mkdir(exist_ok=True)
    caller = {"key_sha256": hashlib.sha256(key.encode()).hexdigest(), "uid": uid, "repos": ["example/app"], "waits": waits}
    (callers / f"{name}.json").write_text(json.dumps(caller))


def make_front(tmp_path: Path, agent: Agent | None = None, **changes: object) -> tuple[executor_front.Front, Agent, Clock]:
    (tmp_path / "state").mkdir(exist_ok=True)
    raw = {
        "name": "stacks",
        "host": "stacks.example.ts.net",
        "page_port": 9301,
        "callers_ports_start": 9302,
        "ports_end": 9313,
        "ports_per_caller": 3,
        "max_stacks": 2,
        "queue_timeout_minutes": 10,
        "evict_idle_minutes": 30,
        "idle_hours": 12,
        "max_age_hours": 72,
        "bundle_max_mb": 1,
        "overrides": {"example/app": {"profiles": {"seed": {}}}},
        "callers_dir": str(tmp_path / "callers"),
        "state_dir": str(tmp_path / "state"),
        "agent_socket": str(tmp_path / "agent.sock"),
        "pause_file": str(tmp_path / "paused"),
    } | changes
    agent = agent or Agent()
    clock = Clock()
    return executor_front.Front(executor_front.Instance(raw), agent, now=clock.now, sleep=clock.sleep), agent, clock


def request(action: str, key: str = KEY, body: bytes = b"", **fields: object) -> io.BytesIO:
    header = {"key": key, "action": action, **fields}
    if action == "up":
        header |= {"repo": "example/app", "bundle_size": len(body)}
    return io.BytesIO(json.dumps(header).encode() + b"\n" + body)


def serve(front: executor_front.Front, conn: io.BytesIO, uid: int | None = 2001) -> list[dict[str, Any]]:
    out = io.BytesIO()
    executor_front.serve(front, conn, out, uid)
    return [json.loads(line) for line in out.getvalue().splitlines()]


def state(tmp_path: Path) -> dict[str, Any]:
    return json.loads((tmp_path / "state" / "state.json").read_text())


# ---------------------------------------------------------------------
# Who may ask
# ---------------------------------------------------------------------


def test_a_key_no_caller_has_is_refused(tmp_path: Path) -> None:
    register(tmp_path, "runner-app-1")
    front, agent, _ = make_front(tmp_path)

    answer = serve(front, request("list", key="guess"))

    assert answer == [{"exit": 1, "message": "refused: the key is no caller's"}]
    assert agent.calls == []


def test_a_key_from_another_uid_is_refused(tmp_path: Path) -> None:
    register(tmp_path, "runner-app-1", uid=2001)
    front, agent, _ = make_front(tmp_path)

    answer = serve(front, request("list"), uid=165536)

    assert answer[-1]["message"] == "refused: uid 165536 may not use this key"
    assert agent.calls == []


def test_a_caller_may_bring_up_only_its_repositories(tmp_path: Path) -> None:
    register(tmp_path, "runner-app-1")
    front, agent, _ = make_front(tmp_path)
    conn = io.BytesIO(json.dumps({"key": KEY, "action": "up", "repo": "example/other", "bundle_size": 3}).encode() + b"\nabc")

    answer = serve(front, conn)

    assert answer[-1]["message"] == "refused: runner-app-1 may bring up example/app, not example/other"
    assert agent.calls == []


def test_a_refused_request_is_logged_with_its_caller_and_message(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    register(tmp_path, "runner-app-1")
    front, _, _ = make_front(tmp_path)
    conn = io.BytesIO(json.dumps({"key": KEY, "action": "up", "repo": "example/other", "bundle_size": 3}).encode() + b"\nabc")

    serve(front, conn)

    assert capsys.readouterr().err == 'up for runner-app-1: exit 1: "refused: runner-app-1 may bring up example/app, not example/other"\n'


def test_a_key_no_caller_has_is_logged_by_its_uid_never_the_key(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    register(tmp_path, "runner-app-1")
    front, _, _ = make_front(tmp_path)

    serve(front, request("list", key="guess"), uid=2002)

    logged = capsys.readouterr().err
    assert logged == 'list for uid 2002: exit 1: "refused: the key is no caller\'s"\n'
    assert "guess" not in logged


def test_a_failed_up_is_logged_and_a_good_one_is_not(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    register(tmp_path, "runner-app-1")
    agent = Agent()
    front, _, _ = make_front(tmp_path, agent)

    serve(front, request("up", body=b"B"))
    agent.up_exit = 1
    serve(front, request("up", body=b"B"))

    assert capsys.readouterr().err == 'up for runner-app-1: exit 1: ""\n'


def test_a_request_that_is_not_json_is_refused(tmp_path: Path) -> None:
    front, _, _ = make_front(tmp_path)

    assert serve(front, io.BytesIO(b"rm -rf /\n"))[-1]["exit"] == 1


def test_an_unknown_action_is_refused(tmp_path: Path) -> None:
    register(tmp_path, "runner-app-1")
    front, agent, _ = make_front(tmp_path)

    assert serve(front, request("exec"))[-1]["message"].startswith("refused: the action is one of")
    assert agent.calls == []


def test_a_bundle_past_the_limit_is_refused_unread(tmp_path: Path) -> None:
    register(tmp_path, "runner-app-1")
    front, agent, _ = make_front(tmp_path)
    conn = io.BytesIO(json.dumps({"key": KEY, "action": "up", "repo": "example/app", "bundle_size": 2 * 1024**2}).encode() + b"\n")

    answer = serve(front, conn)

    assert "at most 1 MB" in answer[-1]["message"]
    assert agent.calls == []


# ---------------------------------------------------------------------
# up, and the places
# ---------------------------------------------------------------------


def test_up_hands_the_bundle_unread_to_the_agent_with_the_callers_ports(tmp_path: Path) -> None:
    register(tmp_path, "runner-app-1")
    front, agent, _ = make_front(tmp_path)

    answer = serve(front, request("up", body=b"BUNDLE", profile="seed"))

    assert answer[0] == {"out": "building"}
    assert answer[-1] == {
        "exit": 0,
        "commit": "c" * 40,
        "page": 2,
        "ports": [9302, 9303, 9304],
        "host": "stacks.example.ts.net",
        "control": "http://stacks.example.ts.net:9301/",
    }
    assert agent.bundles == [b"BUNDLE"]
    sent = agent.calls[0]
    assert sent["caller"] == "runner-app-1"
    assert sent["override"] == {"profiles": {"seed": {}}}
    assert sent["profile"] == "seed"
    assert "key" not in sent
    assert state(tmp_path)["stacks"]["runner-app-1"]["state"] == "running"
    assert list((tmp_path / "state" / "spool").iterdir()) == []


SECRETS = {"repositories": {"example/app": {"model_key": "app-key-value"}, "example/other": {"model_key": "other-key-value"}}}


def test_up_sends_only_the_secrets_of_the_requests_repository(tmp_path: Path) -> None:
    register(tmp_path, "runner-app-1")
    (tmp_path / "secrets.json").write_text(json.dumps(SECRETS))
    front, agent, _ = make_front(tmp_path, secrets_file=str(tmp_path / "secrets.json"))

    serve(front, request("up", body=b"BUNDLE"))

    assert agent.calls[0]["secrets"] == {"model_key": "app-key-value"}
    assert "other-key-value" not in json.dumps(agent.calls)


def test_no_action_but_up_carries_secrets(tmp_path: Path) -> None:
    register(tmp_path, "runner-app-1")
    (tmp_path / "secrets.json").write_text(json.dumps(SECRETS))
    front, agent, _ = make_front(tmp_path, secrets_file=str(tmp_path / "secrets.json"))
    serve(front, request("up", body=b"BUNDLE"))

    serve(front, request("run", suite="e2e"))
    for action in ("logs", "list", "down"):
        serve(front, request(action))

    assert [call["action"] for call in agent.calls if "secrets" in call] == ["up"]


@pytest.mark.parametrize("written", [None, '{"repositories": {}}'])
def test_an_instance_without_secrets_sends_none(tmp_path: Path, written: str | None) -> None:
    register(tmp_path, "runner-app-1")
    if written is not None:
        (tmp_path / "secrets.json").write_text(written)
    front, agent, _ = make_front(tmp_path, secrets_file=str(tmp_path / "secrets.json"))

    serve(front, request("up", body=b"BUNDLE"))

    assert agent.calls[0]["secrets"] == {}


@pytest.mark.parametrize("written", ["not json app-key-value", '{"repositories": {"example/app": ["app-key-value"]}}', "[]"])
def test_secrets_that_do_not_read_refuse_the_up_without_their_contents(tmp_path: Path, written: str) -> None:
    register(tmp_path, "runner-app-1")
    (tmp_path / "secrets.json").write_text(written)
    front, agent, _ = make_front(tmp_path, secrets_file=str(tmp_path / "secrets.json"))

    answer = serve(front, request("up", body=b"BUNDLE"))

    assert answer == [{"exit": 1, "message": "the instance's secrets do not read: tell its host"}]
    assert agent.calls == []
    assert not (tmp_path / "state" / "state.json").exists()


class Gone(io.BytesIO):
    """A connection whose caller went away: a TLS one reset, every write fails."""

    def __init__(self) -> None:
        super().__init__()
        self.writes = 0

    @override
    def write(self, data: object) -> int:
        self.writes += 1
        raise ConnectionResetError("reset by peer")


def test_an_up_whose_caller_is_gone_still_reads_the_agent_to_its_end(tmp_path: Path) -> None:
    register(tmp_path, "runner-app-1")
    front, agent, _ = make_front(tmp_path)
    out = Gone()

    assert executor_front.serve(front, request("up", body=b"BUNDLE"), out, 2001) == 0

    assert agent.actions() == [("up", "runner-app-1")]
    assert state(tmp_path)["stacks"]["runner-app-1"]["state"] == "running"
    assert out.writes == 1


def test_a_callers_ports_stay_its_own_across_ups_and_downs(tmp_path: Path) -> None:
    register(tmp_path, "runner-app-1")
    register(tmp_path, "runner-app-2", key="two")
    front, _, _ = make_front(tmp_path)

    serve(front, request("up", body=b"B"))
    second = serve(front, request("up", key="two", body=b"B"))
    serve(front, request("down"))
    again = serve(front, request("up", body=b"B"))

    assert second[-1]["ports"] == [9305, 9306, 9307]
    assert again[-1]["ports"] == [9302, 9303, 9304]


def test_a_failed_up_takes_no_place(tmp_path: Path) -> None:
    register(tmp_path, "runner-app-1")
    agent = Agent()
    agent.up_exit = 1
    front, _, _ = make_front(tmp_path, agent)

    serve(front, request("up", body=b"B"))

    assert state(tmp_path)["stacks"]["runner-app-1"]["state"] == "failed"


def test_a_refused_up_leaves_the_last_stack_recorded_as_it_was(tmp_path: Path) -> None:
    register(tmp_path, "runner-app-1")
    agent = Agent()
    front, _, clock = make_front(tmp_path, agent)
    serve(front, request("up", body=b"A"))
    before = state(tmp_path)["stacks"]["runner-app-1"]
    agent.up_exit, agent.up_kept = 1, True
    clock.t += 60

    answer = serve(front, request("up", body=b"B"))

    assert answer[-1]["exit"] == 1
    assert state(tmp_path)["stacks"]["runner-app-1"] == before


def test_a_refused_first_up_leaves_no_stack(tmp_path: Path) -> None:
    register(tmp_path, "runner-app-1")
    agent = Agent()
    agent.up_exit, agent.up_kept = 1, True
    front, _, _ = make_front(tmp_path, agent)

    serve(front, request("up", body=b"B"))

    assert "runner-app-1" not in state(tmp_path)["stacks"]


def full(tmp_path: Path, used_at: float) -> None:
    (tmp_path / "state").mkdir(exist_ok=True)
    stacks = {name: {"state": "running", "repo": "example/app", "used_at": used_at, "up_at": used_at} for name in ("a", "b")}
    (tmp_path / "state" / "state.json").write_text(json.dumps({"ports": {"a": 9302, "b": 9305}, "stacks": stacks}))


def test_at_the_limit_the_stack_idle_longest_is_stopped_for_room(tmp_path: Path) -> None:
    register(tmp_path, "runner-app-1")
    front, agent, clock = make_front(tmp_path)
    full(tmp_path, clock.t - 3600)
    stacks = state(tmp_path)["stacks"]
    stacks["b"]["used_at"] = clock.t - 7200
    (tmp_path / "state" / "state.json").write_text(json.dumps({"ports": {"a": 9302, "b": 9305}, "stacks": stacks}))

    answer = serve(front, request("up", body=b"B"))

    assert answer[-1]["exit"] == 0
    assert ("stop", "b") in agent.actions()
    assert state(tmp_path)["stacks"]["b"]["state"] == "stopped"


def test_a_stack_in_use_is_never_stopped_and_a_job_waits_then_goes_on(tmp_path: Path) -> None:
    register(tmp_path, "runner-app-1")
    front, agent, clock = make_front(tmp_path)
    full(tmp_path, clock.t)

    answer = serve(front, request("up", body=b"B"))

    assert answer[-1]["exit"] == executor_front.NO_ROOM
    assert "for 10 minutes: go on without a stack" in answer[-1]["message"]
    assert any("Waiting for a place" in event.get("out", "") for event in answer)
    assert all(action != "stop" for action, _ in agent.actions())
    assert clock.t >= 1_000_000 + 600


def test_a_person_is_refused_at_once_with_what_holds_the_room(tmp_path: Path) -> None:
    register(tmp_path, "person-j", waits=False)
    front, _, clock = make_front(tmp_path)
    full(tmp_path, clock.t)

    answer = serve(front, request("up", body=b"B"))

    assert answer[-1]["exit"] == executor_front.NO_ROOM
    assert "a (example/app, used 0 min ago)" in answer[-1]["message"]
    assert clock.t == 1_000_000


def test_at_the_limit_a_persons_stack_is_never_the_one_stopped(tmp_path: Path) -> None:
    register(tmp_path, "runner-app-1")
    register(tmp_path, "b", key="person", waits=False)
    front, agent, clock = make_front(tmp_path)
    full(tmp_path, clock.t - 3600)
    stacks = state(tmp_path)["stacks"]
    stacks["b"]["used_at"] = clock.t - 7200
    (tmp_path / "state" / "state.json").write_text(json.dumps({"ports": {"a": 9302, "b": 9305}, "stacks": stacks}))

    answer = serve(front, request("up", body=b"B"))

    assert answer[-1]["exit"] == 0
    assert ("stop", "a") in agent.actions()
    assert ("stop", "b") not in agent.actions()


def test_at_the_limit_with_only_peoples_stacks_a_job_waits_then_goes_on(tmp_path: Path) -> None:
    register(tmp_path, "runner-app-1")
    register(tmp_path, "a", key="person-a", waits=False)
    register(tmp_path, "b", key="person-b", waits=False)
    front, agent, clock = make_front(tmp_path)
    full(tmp_path, clock.t - 7200)

    answer = serve(front, request("up", body=b"B"))

    assert answer[-1]["exit"] == executor_front.NO_ROOM
    assert all(action != "stop" for action, _ in agent.actions())


@pytest.mark.parametrize(("reserve", "admitted"), [(0, True), (1, False)])
def test_peoples_stacks_leave_the_waiting_reserve_free(tmp_path: Path, reserve: int, admitted: bool) -> None:
    register(tmp_path, "person-j", waits=False)
    register(tmp_path, "a", key="person-a", waits=False)
    front, agent, clock = make_front(tmp_path, waiting_reserve=reserve)
    stacks = {"a": {"state": "running", "repo": "example/app", "used_at": clock.t, "up_at": clock.t}}
    (tmp_path / "state" / "state.json").write_text(json.dumps({"ports": {"a": 9302}, "stacks": stacks}))

    answer = serve(front, request("up", body=b"B"))

    assert (answer[-1]["exit"] == 0) is admitted
    assert (("up", "person-j") in agent.actions()) is admitted
    assert admitted or "of the 1 places for people's stacks are taken" in answer[-1]["message"]


def test_a_job_takes_the_place_the_waiting_reserve_keeps(tmp_path: Path) -> None:
    register(tmp_path, "runner-app-1")
    register(tmp_path, "a", key="person-a", waits=False)
    front, _, clock = make_front(tmp_path, waiting_reserve=1)
    stacks = {"a": {"state": "running", "repo": "example/app", "used_at": clock.t, "up_at": clock.t}}
    (tmp_path / "state" / "state.json").write_text(json.dumps({"ports": {"a": 9302}, "stacks": stacks}))

    answer = serve(front, request("up", body=b"B"))

    assert answer[-1]["exit"] == 0


@pytest.mark.parametrize(("name", "waits", "started"), [("runner-app-1", True, True), ("person-j", False, False)])
def test_the_page_holds_a_stack_to_its_callers_share_of_the_places(tmp_path: Path, name: str, waits: bool, started: bool) -> None:
    register(tmp_path, name, waits=waits)
    register(tmp_path, "a", key="person-a", waits=False)
    front, agent, clock = make_front(tmp_path, waiting_reserve=1)
    stacks = {
        "a": {"state": "running", "repo": "example/app", "used_at": clock.t, "up_at": clock.t},
        name: {"state": "stopped", "repo": "example/app", "used_at": clock.t, "up_at": clock.t},
    }
    (tmp_path / "state" / "state.json").write_text(json.dumps({"ports": {"a": 9302, name: 9305}, "stacks": stacks}))

    code, message = executor_front.page_post(front, f"/stacks/{name}/start", "1")

    assert (code == 200) is started
    assert (("start", name) in agent.actions()) is started
    assert started or "places for people's stacks are taken" in message


def test_replacing_ones_own_stack_needs_no_room(tmp_path: Path) -> None:
    register(tmp_path, "runner-app-1")
    front, _, clock = make_front(tmp_path, max_stacks=1)

    serve(front, request("up", body=b"B"))
    answer = serve(front, request("up", body=b"B"))

    assert answer[-1]["exit"] == 0
    assert clock.t == 1_000_000


def test_a_paused_instance_takes_no_up(tmp_path: Path) -> None:
    register(tmp_path, "person-j", waits=False)
    front, agent, _ = make_front(tmp_path)
    (tmp_path / "paused").write_text("")

    answer = serve(front, request("up", body=b"B"))

    assert answer[-1] == {"exit": executor_front.NO_ROOM, "message": "the instance is paused by its host"}
    assert agent.calls == []


def test_a_waiting_job_goes_on_when_a_place_frees(tmp_path: Path) -> None:
    register(tmp_path, "runner-app-1")
    front, _, clock = make_front(tmp_path)
    full(tmp_path, clock.t)
    sleeps = []

    def free_after_a_while(seconds: float) -> None:
        sleeps.append(seconds)
        clock.t += seconds
        if len(sleeps) == 3:
            data = state(tmp_path)
            data["stacks"]["a"]["state"] = "stopped"
            (tmp_path / "state" / "state.json").write_text(json.dumps(data))

    front.sleep = free_after_a_while

    answer = serve(front, request("up", body=b"B"))

    assert answer[-1]["exit"] == 0
    assert len(sleeps) == 3


# ---------------------------------------------------------------------
# run, logs, down and list
# ---------------------------------------------------------------------


def test_run_wants_a_running_stack(tmp_path: Path) -> None:
    register(tmp_path, "runner-app-1")
    front, agent, _ = make_front(tmp_path)

    assert serve(front, request("run", suite="e2e"))[-1]["message"] == "refused: no stack: up first"
    serve(front, request("up", body=b"B"))
    answer = serve(front, request("run", suite="e2e"))

    assert answer == [{"out": "run done"}, {"exit": 0}]
    assert agent.calls[-1] == {"action": "run", "caller": "runner-app-1", "suite": "e2e"}


def test_logs_pass_the_service_on(tmp_path: Path) -> None:
    register(tmp_path, "runner-app-1")
    front, agent, _ = make_front(tmp_path)
    serve(front, request("up", body=b"B"))

    serve(front, request("logs", service="web"))

    assert agent.calls[-1] == {"action": "logs", "caller": "runner-app-1", "service": "web"}


def test_list_shows_every_stack_and_who_asks(tmp_path: Path) -> None:
    register(tmp_path, "runner-app-1")
    front, agent, _ = make_front(tmp_path)
    serve(front, request("up", body=b"B"))
    agent.stacks = {"runner-app-1": {"state": "running", "memory": 300 * 1024**2}}

    answer = serve(front, request("list"))

    assert answer[-1]["caller"] == "runner-app-1"
    (stack,) = answer[-1]["stacks"]
    assert stack["url"] == "http://stacks.example.ts.net:9303/"
    assert stack["memory_mb"] == 300
    assert stack["state"] == "running"


def test_down_forgets_the_stack(tmp_path: Path) -> None:
    register(tmp_path, "runner-app-1")
    front, agent, _ = make_front(tmp_path)
    serve(front, request("up", body=b"B"))

    serve(front, request("down"))

    assert agent.actions()[-1] == ("down", "runner-app-1")
    assert state(tmp_path)["stacks"] == {}
    assert state(tmp_path)["ports"] == {"runner-app-1": 9302}


# ---------------------------------------------------------------------
# The reaper
# ---------------------------------------------------------------------


def test_the_reaper_stops_idle_stacks_and_removes_old_and_orphaned_ones(tmp_path: Path) -> None:
    register(tmp_path, "idle")
    register(tmp_path, "old", key="o")
    register(tmp_path, "busy", key="b")
    front, agent, clock = make_front(tmp_path)
    now = clock.t
    stacks = {
        "idle": {"state": "running", "up_at": now - 13 * 3600, "used_at": now - 13 * 3600},
        "old": {"state": "stopped", "up_at": now - 73 * 3600, "used_at": now - 73 * 3600},
        "busy": {"state": "running", "up_at": now - 20 * 3600, "used_at": now - 60},
        "gone": {"state": "running", "up_at": now, "used_at": now},
    }
    (tmp_path / "state" / "state.json").write_text(json.dumps({"ports": {"idle": 1, "old": 2, "gone": 3}, "stacks": stacks}))
    agent.stacks = {name: {"state": "running"} for name in ("idle", "busy", "gone", "stray")}

    done = front.reap()

    assert done == ["removed gone", "stopped idle", "removed old", "removed stray"]
    assert state(tmp_path)["ports"] == {"idle": 1, "old": 2}
    assert set(state(tmp_path)["stacks"]) == {"idle", "busy"}
    assert agent.actions()[-1] == ("prune", "")


def test_the_reaper_leaves_an_up_in_progress(tmp_path: Path) -> None:
    register(tmp_path, "new")
    front, agent, clock = make_front(tmp_path)
    (tmp_path / "state" / "state.json").write_text(json.dumps({"stacks": {"new": {"state": "starting", "used_at": clock.t}}}))
    agent.stacks = {"new": {"state": "running"}}

    assert front.reap() == []


# ---------------------------------------------------------------------
# The page
# ---------------------------------------------------------------------


def test_the_page_wants_its_header(tmp_path: Path) -> None:
    front, agent, _ = make_front(tmp_path)

    assert executor_front.page_post(front, "/stacks/a/stop", None)[0] == 403
    assert executor_front.page_post(front, "/stacks/../stop", "1")[0] == 404
    assert agent.calls == []


def test_the_page_stops_and_starts_a_stack(tmp_path: Path) -> None:
    register(tmp_path, "runner-app-1")
    front, agent, _ = make_front(tmp_path)
    serve(front, request("up", body=b"B"))

    assert executor_front.page_post(front, "/stacks/runner-app-1/stop", "1") == (200, "Stopped 1 container(s).")
    assert state(tmp_path)["stacks"]["runner-app-1"]["state"] == "stopped"
    assert executor_front.page_post(front, "/stacks/runner-app-1/start", "1") == (200, "Started.")
    assert agent.actions()[-1] == ("start", "runner-app-1")
    assert state(tmp_path)["stacks"]["runner-app-1"]["state"] == "running"


def test_the_page_starts_a_failed_stack_again_and_logs_a_start_that_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    register(tmp_path, "runner-app-1")
    agent = Agent()
    agent.up_exit = 1
    front, _, _ = make_front(tmp_path, agent)
    serve(front, request("up", body=b"B"))
    capsys.readouterr()
    agent.start_exit = 1

    assert executor_front.page_post(front, "/stacks/runner-app-1/start", "1") == (500, "a service did not run")
    assert state(tmp_path)["stacks"]["runner-app-1"]["state"] == "failed"
    assert capsys.readouterr().err == 'page start for runner-app-1: exit 500: "a service did not run"\n'
    agent.start_exit = 0
    assert executor_front.page_post(front, "/stacks/runner-app-1/start", "1") == (200, "Started.")
    assert state(tmp_path)["stacks"]["runner-app-1"]["state"] == "running"


def test_the_page_starts_nothing_when_the_instance_is_full(tmp_path: Path) -> None:
    front, agent, clock = make_front(tmp_path)
    full(tmp_path, clock.t)
    data = state(tmp_path)
    data["stacks"]["c"] = {"state": "stopped"}
    (tmp_path / "state" / "state.json").write_text(json.dumps(data))

    code, message = executor_front.page_post(front, "/stacks/c/start", "1")

    assert code == 409
    assert "stacks are in use" in message
    assert ("start", "c") not in agent.actions()


def test_the_page_escapes_what_it_shows() -> None:
    assert "esc(s.caller)" in executor_front.PAGE
    assert "onclick=" not in executor_front.PAGE


def test_check_names_the_instances_blocks(tmp_path: Path) -> None:
    front, _, _ = make_front(tmp_path)

    assert front.instance.caller_blocks() == [9302, 9305, 9308, 9311]


def test_a_caller_file_that_does_not_parse_is_skipped(tmp_path: Path) -> None:
    register(tmp_path, "runner-app-1")
    (tmp_path / "callers" / "broken.json").write_text("{")
    front, _, _ = make_front(tmp_path)

    assert list(front.instance.callers()) == ["runner-app-1"]


@pytest.mark.parametrize("uid", [None, ""])
def test_a_caller_without_a_uid_takes_its_key_from_anyone(tmp_path: Path, uid: object) -> None:
    callers = tmp_path / "callers"
    callers.mkdir()
    (callers / "person-j.json").write_text(json.dumps({"key_sha256": hashlib.sha256(KEY.encode()).hexdigest(), "uid": uid}))
    front, _, _ = make_front(tmp_path)

    assert executor_front.authenticate(front.instance, KEY, 4242).name == "person-j"


def test_a_callers_second_request_is_refused_before_its_bundle_is_read(tmp_path: Path) -> None:
    instance = make_front(tmp_path)[0].instance
    with executor_front.caller_lock(instance, "runner-app-1"):
        refused = pytest.raises(executor_front.RefusedError, match="still running")
        with refused, executor_front.caller_lock(instance, "runner-app-1", wait=False):
            pass


@pytest.mark.parametrize(
    ("host", "allowed"),
    [("stacks.example.ts.net:9301", True), ("evil.example.com:9301", False), ("stacks.example.ts.net", False), (None, False)],
)
def test_the_page_answers_only_by_its_own_name(tmp_path: Path, host: str | None, allowed: bool) -> None:
    assert executor_front.page_host(make_front(tmp_path)[0].instance, host) is allowed


def test_a_page_request_by_another_name_gets_nothing(tmp_path: Path) -> None:
    front = make_front(tmp_path)[0]
    stop = "/stacks/runner-app-1/stop"

    assert executor_front.page_request(front, "POST", stop, {"host": "evil.example.com:9301", "x-executor": "1"})[0] == 421
    assert executor_front.page_request(front, "GET", "/", {"host": "stacks.example.ts.net:9301"})[0] == 200
    assert executor_front.page_request(front, "POST", stop, {"host": "stacks.example.ts.net:9301"})[0] == 403


# ---------------------------------------------------------------------
# A remote caller, on the TLS port
# ---------------------------------------------------------------------

REMOTE_KEY = "a-key-of-the-remote-host"


def register_remote(tmp_path: Path, name: str = "remote-app", sources: list[str] | None = None) -> None:
    callers = tmp_path / "callers"
    callers.mkdir(exist_ok=True)
    caller = {
        "key_sha256": hashlib.sha256(REMOTE_KEY.encode()).hexdigest(),
        "repos": ["example/app"],
        "waits": True,
        "from": sources if sources is not None else ["192.0.2.0/24", "2001:db8::/32"],
    }
    (callers / f"{name}.json").write_text(json.dumps(caller))


@pytest.mark.parametrize("address", ["192.0.2.7", "::ffff:192.0.2.7", "2001:db8::5"])
def test_a_remote_caller_is_taken_from_its_networks(tmp_path: Path, address: str) -> None:
    register_remote(tmp_path)
    front, _, _ = make_front(tmp_path)

    assert executor_front.authenticate(front.instance, REMOTE_KEY, None, address).name == "remote-app"


def test_a_remote_caller_from_elsewhere_is_refused(tmp_path: Path) -> None:
    register_remote(tmp_path)
    front, _, _ = make_front(tmp_path)

    with pytest.raises(executor_front.RefusedError, match=r"198\.51\.100\.9 may not use this key"):
        executor_front.authenticate(front.instance, REMOTE_KEY, None, "198.51.100.9")


def test_a_remote_callers_key_is_refused_on_the_socket(tmp_path: Path) -> None:
    register_remote(tmp_path)
    front, agent, _ = make_front(tmp_path)

    answer = serve(front, request("list", key=REMOTE_KEY), uid=2001)

    assert answer == [{"exit": 1, "message": "refused: a remote caller's key is for the instance's TLS port"}]
    assert agent.calls == []


@pytest.mark.parametrize("uid", [2001, None])
def test_a_local_callers_key_is_refused_on_the_tls_port(tmp_path: Path, uid: int | None) -> None:
    register(tmp_path, "runner-app-1", uid=uid)
    front, _, _ = make_front(tmp_path)

    with pytest.raises(executor_front.RefusedError, match="a local caller's key is for the instance's socket"):
        executor_front.authenticate(front.instance, KEY, None, "192.0.2.7")


def test_a_remote_caller_with_a_network_that_is_none_is_not_a_caller(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    register_remote(tmp_path, sources=["192.0.2.0/24", "not a network"])
    front, _, _ = make_front(tmp_path)

    assert "remote-app" not in front.instance.callers()
    assert "Skipping the caller file remote-app.json" in capsys.readouterr().err


def test_a_remote_caller_with_no_network_is_not_a_local_one(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    register_remote(tmp_path, sources=[])
    front, _, _ = make_front(tmp_path)

    assert "remote-app" not in front.instance.callers()
    assert "names no network" in capsys.readouterr().err
    with pytest.raises(executor_front.RefusedError, match="the key is no caller's"):
        executor_front.authenticate(front.instance, REMOTE_KEY, 2001)


@pytest.fixture
def tls_pair(tmp_path: Path) -> tuple[ssl.SSLContext, ssl.SSLContext]:
    """The server's context with a certificate made here, and a client's that pins nothing."""
    if shutil.which("openssl") is None:
        pytest.skip("no openssl to make a certificate with")
    cert, key = tmp_path / "tls.crt", tmp_path / "tls.key"
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes", "-days", "1",
         "-subj", "/CN=stacks.example.ts.net", "-keyout", str(key), "-out", str(cert)],
        check=True,
        capture_output=True,
    )
    front, _, _ = make_front(tmp_path, tls_cert=str(cert), tls_key=str(key))
    client = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    client.check_hostname = False
    client.verify_mode = ssl.CERT_NONE
    return executor_front.tls_context(front.instance), client


def serve_over_tls(
    front: executor_front.Front,
    server: ssl.SSLContext,
    client: ssl.SSLContext,
    address: str,
    send: bytes,
) -> tuple[int, list[dict[str, Any]]]:
    ours, theirs = socket.socketpair()
    status: list[int] = []
    thread = threading.Thread(target=lambda: status.append(executor_front.serve_tls(front, ours, server, address)))
    thread.start()
    with client.wrap_socket(theirs) as conn:
        conn.sendall(send)
        received = b""
        while chunk := conn.recv(65536):
            received += chunk
    thread.join(10)
    return status[0], [json.loads(line) for line in received.splitlines()]


def test_a_remote_up_over_tls_reaches_the_agent(tmp_path: Path, tls_pair: tuple[ssl.SSLContext, ssl.SSLContext]) -> None:
    register_remote(tmp_path)
    front, agent, _ = make_front(tmp_path)
    header = {"key": REMOTE_KEY, "action": "up", "repo": "example/app", "bundle_size": 6}

    status, answer = serve_over_tls(front, *tls_pair, "192.0.2.7", json.dumps(header).encode() + b"\nBUNDLE")

    assert status == 0
    assert answer[-1]["exit"] == 0
    assert agent.calls[0]["caller"] == "remote-app"
    assert agent.bundles == [b"BUNDLE"]


def test_a_remote_request_that_stalls_times_out_and_leaves_no_spool(
    tmp_path: Path,
    tls_pair: tuple[ssl.SSLContext, ssl.SSLContext],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    register_remote(tmp_path)
    front, agent, _ = make_front(tmp_path)
    monkeypatch.setattr(executor_front, "REMOTE_TIMEOUT_S", 0.5)
    header = {"key": REMOTE_KEY, "action": "up", "repo": "example/app", "bundle_size": 1000}
    ours, theirs = socket.socketpair()
    status: list[int] = []
    thread = threading.Thread(target=lambda: status.append(executor_front.serve_tls(front, ours, tls_pair[0], "192.0.2.7")))
    thread.start()
    with tls_pair[1].wrap_socket(theirs) as conn:
        conn.sendall(json.dumps(header).encode() + b"\nBUN")
        thread.join(10)

    assert status == [1]
    assert agent.calls == []
    assert list((tmp_path / "state" / "spool").iterdir()) == []
    assert "up for remote-app: exit 1" in capsys.readouterr().err


def test_a_peer_that_speaks_no_tls_is_logged_and_dropped(
    tmp_path: Path,
    tls_pair: tuple[ssl.SSLContext, ssl.SSLContext],
    capsys: pytest.CaptureFixture[str],
) -> None:
    front, agent, _ = make_front(tmp_path)
    ours, theirs = socket.socketpair()
    with theirs:
        theirs.sendall(b'{"key": "plain"}\n')
        theirs.shutdown(socket.SHUT_WR)
        assert executor_front.serve_tls(front, ours, tls_pair[0], "198.51.100.9") == 1

    assert agent.calls == []
    assert '- for 198.51.100.9: exit 1: "no TLS' in capsys.readouterr().err
