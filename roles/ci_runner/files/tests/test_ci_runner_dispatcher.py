"""Tests for the CI dispatcher: who gets a runner, in which slot, and what is
stopped or removed.
"""

import errno
import io
import json
import os
import urllib.error
import urllib.request
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from email.message import Message
from pathlib import Path
from typing import Any, ClassVar, Never, Self, cast, override

import ci_runner_dispatcher as dispatcher
import pytest

NOW = datetime(2026, 11, 12, 10, 0, tzinfo=UTC)
LABEL = "ci-runner"
PR = "ci-runner-pr"
APP = "example/app"
CORPUS = "example/corpus"


def runner(name: str, repo: str = APP, slot: int = 1, pool: str = "main", age: timedelta = timedelta(minutes=1)) -> dispatcher.Runner:
    return dispatcher.Runner(
        container=dispatcher.slot_unit(pool, slot), name=name, repo=repo, pool=pool, slot=slot,
        started=NOW - age,
    )


class FakeGitHub:
    def __init__(
        self,
        *,
        private: dict[str, bool] | None = None,
        queued: dict[tuple[str, str], int] | None = None,
        registered: dict[str, list[dict[str, Any]]] | None = None,
        listed: dict[str, list[dict[str, Any]]] | None = None,
    ) -> None:
        self.private = private or {}
        self.queued = queued or {}
        # Jobs listed besides the queued ones, by repository, and the answers
        # for one job by id, which raise when an error is given.
        self.listed = listed or {}
        self.answers: dict[int, dict[str, Any] | Exception] = {}
        self.asked: list[int] = []
        self.registered = registered or {}
        self.removed: list[tuple[str, int]] = []
        self.variables: dict[str, str] = {}
        self.made: list[tuple[str, str, str]] = []
        self.token_expiry: str | None = None
        self.rate_remaining: int | None = None

    def is_private(self, repo: str) -> bool:
        return self.private.get(repo, True)

    def jobs(self, repo: str, labels: list[str]) -> list[dict[str, Any]]:
        assert labels == [LABEL, PR]
        queued = [
            {"id": 1000 * (labels.index(label) + 1) + n, "status": "queued", "labels": [label]}
            for label in labels
            for n in range(self.queued.get((repo, label), 0))
        ]
        return queued + self.listed.get(repo, [])

    def job(self, repo: str, job_id: int) -> dict[str, Any]:
        self.asked.append(job_id)
        answer = self.answers[job_id]
        if isinstance(answer, Exception):
            raise answer
        return answer

    def runners(self, repo: str, labels: list[str]) -> list[dict[str, Any]]:
        return [r for r in self.registered.get(repo, []) if (repo, r["id"]) not in self.removed]

    def remove_runner(self, repo: str, runner_id: int) -> None:
        self.removed.append((repo, runner_id))

    def set_variable(self, repo: str, name: str, value: str) -> None:
        assert name == "CI_RUNNER"
        self.variables[repo] = value

    def delete_variable(self, repo: str, name: str) -> bool:
        assert name == "CI_RUNNER"
        return self.variables.pop(repo, None) is not None

    def jit_config(self, repo: str, name: str, label: str) -> str:
        self.made.append((repo, name, label))
        return f"jit-{name}"


class FakeAccount:
    uid = 1000

    def __init__(self, running: Iterable[dispatcher.Runner] = ()) -> None:
        self.running = list(running)
        self.started: list[tuple[list[str], str]] = []
        self.stopped: list[str] = []

    def runners(self) -> list[dispatcher.Runner]:
        return list(self.running)

    def start(self, command: list[str], unit: str, max_seconds: int) -> None:
        self.started.append((command, unit))

    def stop(self, container: str) -> None:
        self.stopped.append(container)


@pytest.fixture
def config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    monkeypatch.setattr(dispatcher.os, "chown", lambda *_args: None)
    monkeypatch.setattr(dispatcher, "sandbox_held", lambda config: True)
    return {
        "account": "ci-runner",
        "sandbox_unit": "sandbox.service",
        "repos": [APP, CORPUS],
        "label": LABEL,
        "variable": "CI_RUNNER",
        "slots": 2,
        "image": "localhost/ci-runner-runner:latest",
        "env_dir": str(tmp_path),
        "pause_file": str(tmp_path / "paused"),
        "images_dir": "/home/ci-runner/images",
        "images_staging": "/home/ci-runner/staging",
        "job_max_seconds": 3600,
        "poll_seconds": 20,
    }


def dispatcher_for(config: dict[str, Any], github: FakeGitHub, account: FakeAccount) -> dispatcher.Dispatcher:
    """A dispatcher over the stand-ins, which are not the classes it names."""
    return dispatcher.Dispatcher(config, cast("dispatcher.GitHub", github), cast("dispatcher.Account", account))


def poll(config: dict[str, Any], github: FakeGitHub, account: FakeAccount) -> dispatcher.Dispatcher:
    d = dispatcher_for(config, github, account)
    d.poll(NOW)
    return d


def started(account: FakeAccount) -> list[tuple[str, str, int]]:
    """(unit, pool, slot) of each runner started."""
    out: list[tuple[str, str, int]] = []
    for command, unit in account.started:
        labels = dict(a.split("=", 1) for a in command if a.startswith("ci-runner."))
        out.append((unit, labels["ci-runner.pool"], int(labels["ci-runner.slot"])))
    return out


def test_a_queued_job_gets_a_runner_with_its_one_time_config(config: dict[str, Any], tmp_path: Path) -> None:
    github, account = FakeGitHub(queued={(APP, LABEL): 1}), FakeAccount()
    poll(config, github, account)
    assert started(account) == [("ci-runner-main-1", "main", 1)]
    (repo, name, label), = github.made
    assert (repo, label) == (APP, LABEL)
    env_file = tmp_path / f"{name}.env"
    assert env_file.read_text() == f"JIT_CONFIG=jit-{name}\n"
    assert env_file.stat().st_mode & 0o777 == 0o400
    command = account.started[0][0]
    assert command[-1] == config["image"]
    assert not any("jit-" in part for part in command)


def test_a_pull_request_job_gets_the_pr_pool_and_its_stores(config: dict[str, Any]) -> None:
    github, account = FakeGitHub(queued={(APP, PR): 1}), FakeAccount()
    poll(config, github, account)
    assert started(account) == [("ci-runner-pr-1", "pr", 1)]
    assert github.made[0][2] == PR
    command = account.started[0][0]
    assert "ci-runner-docker-pr-1:/var/lib/docker" in command
    assert "ci-runner-cache-pr-1:/root/.cache" in command


def test_the_runner_gets_no_device_of_the_host(config: dict[str, Any]) -> None:
    command = dispatcher.run_command(config, "n", APP, "main", 1, "/run/ci-runner/n.env")
    assert "--privileged" not in command
    assert not any(part.startswith("--device") for part in command)
    assert command[command.index("--name") + 1] == "ci-runner-main-1"


def test_a_runner_gets_the_jobs_environment_of_the_config(config: dict[str, Any]) -> None:
    config["job_env"] = {"WORKERS": "2", "CI_HOST": "own"}
    command = dispatcher.run_command(config, "n", APP, "main", 1, "/run/ci-runner/n.env")
    given = [command[i + 1] for i, part in enumerate(command) if part == "--env"]
    assert given == ["CI_HOST=own", "WORKERS=2"]
    assert command[-1] == config["image"]


def test_every_name_follows_the_name_the_script_is_installed_under(config: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dispatcher, "NAME", dispatcher.installed_name("/usr/local/sbin/own-ci-dispatcher"))
    command = dispatcher.run_command(config, "n", APP, "pr", 2, "/run/own-ci/n.env")
    assert dispatcher.slot_unit("pr", 2) == "own-ci-pr-2"
    assert command[command.index("--name") + 1] == "own-ci-pr-2"
    assert "own-ci.runner=1" in command and "own-ci.pool=pr" in command
    assert "own-ci-docker-pr-2:/var/lib/docker" in command
    assert "own-ci-cache-pr-2:/root/.cache" in command
    assert dispatcher.runner_name(APP, "pr").startswith("own-ci-app-pr-")
    assert dispatcher.images_container() == "own-ci-images"
    assert f"{config['images_dir']}:/ci-runner-images:ro" in command


@pytest.mark.parametrize("script", ["/usr/local/sbin/dispatcher", "/tmp/Own_CI-dispatcher", "/x/-dispatcher"])
def test_a_script_installed_under_a_strange_name_refuses_to_run(script: str) -> None:
    with pytest.raises(SystemExit):
        dispatcher.installed_name(script)


class Ran:
    def __init__(self, returncode: int) -> None:
        self.returncode = returncode


def test_the_sandbox_is_held_when_its_unit_is_active_and_the_hosts_check_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    ran: list[list[str]] = []

    def run(command: list[str], **_kwargs: object) -> Ran:
        ran.append(command)
        return Ran(0)

    monkeypatch.setattr(dispatcher.subprocess, "run", run)
    config = {"sandbox_unit": "sandbox.service", "sandbox_check": ["/usr/local/sbin/check", "ci"]}
    assert dispatcher.sandbox_held(config) is True
    assert ran == [[dispatcher.SYSTEMCTL, "is-active", "--quiet", "sandbox.service"],
                   ["/usr/local/sbin/check", "ci"]]


@pytest.mark.parametrize("failing", [dispatcher.SYSTEMCTL, "/usr/local/sbin/check"])
def test_the_sandbox_is_not_held_when_the_unit_or_the_check_fails(monkeypatch: pytest.MonkeyPatch, failing: str) -> None:
    monkeypatch.setattr(dispatcher.subprocess, "run",
                        lambda command, **_kwargs: Ran(3 if command[0] == failing else 0))
    config = {"sandbox_unit": "sandbox.service", "sandbox_check": ["/usr/local/sbin/check"]}
    assert dispatcher.sandbox_held(config) is False


def test_a_check_that_cannot_run_or_hangs_counts_as_not_held(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(command: list[str], **_kwargs: object) -> Never:
        raise FileNotFoundError(command[0])

    def hanging(command: list[str], **_kwargs: object) -> Never:
        raise dispatcher.subprocess.TimeoutExpired(command, 30)

    for run in (missing, hanging):
        monkeypatch.setattr(dispatcher.subprocess, "run", run)
        assert dispatcher.sandbox_held({"sandbox_unit": "sandbox.service"}) is False


def test_a_runner_mounts_the_shared_image_files_read_only(config: dict[str, Any]) -> None:
    command = dispatcher.run_command(config, "n", APP, "pr", 1, "/run/ci-runner/n.env")
    mounts = [command[i + 1] for i, part in enumerate(command) if part == "--volume"]
    shared = [m for m in mounts if m.startswith("/home/ci-runner/images:")]
    assert shared == ["/home/ci-runner/images:/ci-runner-images:ro"]


def test_jobs_take_only_the_free_slots_across_repositories_and_pools(config: dict[str, Any]) -> None:
    github = FakeGitHub(queued={(APP, LABEL): 3, (APP, PR): 1, (CORPUS, PR): 2})
    account = FakeAccount()
    poll(config, github, account)
    assert sorted(slot for _, _, slot in started(account)) == [1, 2]


def test_a_used_slot_is_not_given_again(config: dict[str, Any]) -> None:
    github = FakeGitHub(
        queued={(CORPUS, LABEL): 1},
        registered={APP: [{"id": 1, "name": "a", "busy": True, "status": "online"}]},
    )
    account = FakeAccount([runner("a", slot=1)])
    poll(config, github, account)
    assert started(account) == [("ci-runner-main-2", "main", 2)]


def test_a_slot_held_by_the_play_warming_it_is_not_given(config: dict[str, Any]) -> None:
    warming = dispatcher.Runner("ci-runner-pr-2", "", "", "pr", 2, NOW)
    account = FakeAccount([runner("a", slot=1), warming])
    poll(config, FakeGitHub(queued={(APP, LABEL): 1}), account)
    assert account.started == []
    assert account.stopped == []


def test_a_runner_waiting_for_a_job_counts_against_the_queue(config: dict[str, Any]) -> None:
    github = FakeGitHub(queued={(APP, LABEL): 1}, registered={APP: [{"id": 1, "name": "a"}]})
    account = FakeAccount([runner("a")])
    poll(config, github, account)
    assert account.started == []


def test_a_waiting_runner_of_the_other_pool_does_not_count(config: dict[str, Any]) -> None:
    github = FakeGitHub(queued={(APP, PR): 1}, registered={APP: [{"id": 1, "name": "a"}]})
    account = FakeAccount([runner("a", pool="main", slot=1)])
    poll(config, github, account)
    assert started(account) == [("ci-runner-pr-2", "pr", 2)]


def test_a_runner_idle_too_long_is_stopped(config: dict[str, Any]) -> None:
    github = FakeGitHub(registered={APP: [{"id": 1, "name": "a", "busy": False}]})
    account = FakeAccount([runner("a", age=dispatcher.IDLE_MAX)])
    poll(config, github, account)
    assert account.stopped == ["ci-runner-main-1"]


def test_a_busy_runner_is_not_stopped_however_long(config: dict[str, Any]) -> None:
    github = FakeGitHub(registered={APP: [{"id": 1, "name": "a", "busy": True}]})
    account = FakeAccount([runner("a", age=timedelta(minutes=50))])
    poll(config, github, account)
    assert account.stopped == []


def test_a_public_repository_gets_no_runner_and_loses_its_own(config: dict[str, Any]) -> None:
    github = FakeGitHub(
        private={CORPUS: False},
        queued={(CORPUS, LABEL): 2},
        registered={CORPUS: [{"id": 7, "name": "c", "busy": True}, {"id": 8, "name": "d"}]},
    )
    account = FakeAccount([runner("c", repo=CORPUS)])
    poll(config, github, account)
    assert github.made == []
    assert account.stopped == ["ci-runner-main-1"]
    assert sorted(github.removed) == [(CORPUS, 7), (CORPUS, 8)]


def test_a_public_repository_loses_its_variable(config: dict[str, Any]) -> None:
    github = FakeGitHub(private={CORPUS: False})
    github.variables = {APP: LABEL, CORPUS: LABEL}
    poll(config, github, FakeAccount())
    assert github.variables == {APP: LABEL}


def test_enable_sets_the_variable_of_private_repositories_only(config: dict[str, Any]) -> None:
    github = FakeGitHub(private={CORPUS: False})
    assert dispatcher_for(config, github, FakeAccount()).enable() == 0
    assert github.variables == {APP: LABEL}


def test_disable_removes_every_variable(config: dict[str, Any]) -> None:
    github = FakeGitHub()
    github.variables = {APP: LABEL, CORPUS: LABEL}
    assert dispatcher_for(config, github, FakeAccount()).disable() == 0
    assert github.variables == {}


def test_a_public_repository_is_retired_even_when_paused_or_unfiltered(
    config: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    (tmp_path / "paused").touch()
    monkeypatch.setattr(dispatcher, "sandbox_held", lambda config: False)
    github = FakeGitHub(private={APP: False}, registered={APP: [{"id": 9, "name": "x"}]})
    poll(config, github, FakeAccount())
    assert github.removed == [(APP, 9)]


def test_a_failing_repository_does_not_keep_a_later_one_from_being_retired(config: dict[str, Any]) -> None:
    class Failing(FakeGitHub):
        @override
        def is_private(self, repo: str) -> bool:
            if repo == APP:
                raise urllib.error.URLError("down")
            return super().is_private(repo)

    github = Failing(private={CORPUS: False}, registered={CORPUS: [{"id": 3, "name": "z"}]})
    poll(config, github, FakeAccount())
    assert github.removed == [(CORPUS, 3)]


def test_paused_starts_nothing(config: dict[str, Any], tmp_path: Path) -> None:
    (tmp_path / "paused").touch()
    account = FakeAccount()
    poll(config, FakeGitHub(queued={(APP, LABEL): 1}), account)
    assert account.started == []


def test_without_the_sandbox_nothing_starts(config: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dispatcher, "sandbox_held", lambda config: False)
    account = FakeAccount()
    poll(config, FakeGitHub(queued={(APP, LABEL): 1}), account)
    assert account.started == []


def test_without_the_sandbox_the_running_runners_are_stopped(
    config: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setattr(dispatcher, "sandbox_held", lambda config: False)
    busy = runner("ci-runner-app-main-1")
    github = FakeGitHub(registered={APP: [{"id": 1, "name": busy.name, "busy": True, "status": "online"}]})
    account = FakeAccount([busy])
    poll(config, github, account)
    assert account.stopped == [busy.container]
    (tmp_path / "paused").touch()
    account = FakeAccount([busy])
    poll(config, github, account)
    assert account.stopped == [busy.container]


def test_paused_leaves_the_running_runners_alone(config: dict[str, Any], tmp_path: Path) -> None:
    (tmp_path / "paused").touch()
    busy = runner("ci-runner-app-main-1")
    github = FakeGitHub(registered={APP: [{"id": 1, "name": busy.name, "busy": True, "status": "online"}]})
    account = FakeAccount([busy])
    poll(config, github, account)
    assert account.stopped == []


def test_visibility_other_than_private_true_is_public(monkeypatch: pytest.MonkeyPatch) -> None:
    github = dispatcher.GitHub("t")
    for answer, expected in [({"private": True}, True), ({"private": False}, False), ({}, False),
                             ({"private": "true"}, False)]:
        monkeypatch.setattr(github, "get", lambda path, a=answer: a)
        assert github.is_private(APP) is expected


def test_offline_runners_without_a_container_are_removed_after_a_grace(config: dict[str, Any]) -> None:
    github = FakeGitHub(
        registered={APP: [{"id": 3, "name": "gone", "status": "offline"},
                          {"id": 4, "name": "a", "status": "offline"},
                          {"id": 5, "name": "new", "status": "offline"}]},
    )
    d = dispatcher_for(config, github, FakeAccount([runner("a")]))
    d.recent["new"] = NOW - timedelta(minutes=1)
    d.poll(NOW)
    assert github.removed == [(APP, 3)]


def test_a_runner_over_its_time_is_stopped(config: dict[str, Any]) -> None:
    account = FakeAccount([runner("old", age=timedelta(hours=2))])
    poll(config, FakeGitHub(registered={APP: [{"id": 1, "name": "old", "busy": True}]}), account)
    assert account.stopped == ["ci-runner-main-1"]


def test_old_env_files_are_removed(config: dict[str, Any], tmp_path: Path) -> None:
    old = tmp_path / "old.env"
    old.write_text("x")
    stamp = (NOW - timedelta(minutes=5)).timestamp()
    os.utime(old, (stamp, stamp))
    fresh = tmp_path / "fresh.env"
    fresh.write_text("x")
    os.utime(fresh, (NOW.timestamp(), NOW.timestamp()))
    poll(config, FakeGitHub(), FakeAccount())
    assert not old.exists() and fresh.exists()


def test_one_repository_failing_does_not_stop_the_others(config: dict[str, Any]) -> None:
    class Failing(FakeGitHub):
        @override
        def jobs(self, repo: str, labels: list[str]) -> list[dict[str, Any]]:
            if repo == APP:
                raise urllib.error.URLError("down")
            return super().jobs(repo, labels)

    github, account = Failing(queued={(CORPUS, LABEL): 1}), FakeAccount()
    poll(config, github, account)
    assert [repo for repo, _, _ in github.made] == [CORPUS]


def test_only_queued_jobs_are_counted_by_label(monkeypatch: pytest.MonkeyPatch) -> None:
    github = dispatcher.GitHub("t")
    answers = {
        "/repos/r/x/actions/runs": {"workflow_runs": [{"id": 1}]},
        "/repos/r/x/actions/runs/1/jobs": {
            "jobs": [
                {"status": "queued", "labels": [LABEL]},
                {"status": "queued", "labels": [PR]},
                {"status": "queued", "labels": ["ubuntu-latest"]},
                {"status": "in_progress", "labels": [LABEL]},
            ],
        },
    }
    in_progress = {"workflow_runs": []}
    monkeypatch.setattr(github, "get", lambda path, **q: in_progress if q.get("status") == "in_progress" else answers[path])
    jobs = github.jobs("r/x", [LABEL, PR])
    assert len(jobs) == 3
    assert dispatcher.queued_counts(jobs, [LABEL, PR]) == {LABEL: 1, PR: 1}


def test_an_unchanged_answer_comes_from_the_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    github = dispatcher.GitHub("t")
    sent: list[str | None] = []

    class Response(io.BytesIO):
        headers: ClassVar[dict[str, str]] = {"ETag": '"e1"'}

        @override
        def __enter__(self) -> Self:
            return self

        @override
        def __exit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
            return None

    def urlopen(request: urllib.request.Request, timeout: float) -> Response:
        sent.append(request.get_header("If-none-match"))
        if len(sent) == 1:
            return Response(b'{"private": true}')
        raise urllib.error.HTTPError(request.full_url, 304, "Not Modified", Message(), None)

    monkeypatch.setattr(dispatcher.urllib.request, "urlopen", urlopen)
    assert github.get("/repos/a/b") == {"private": True}
    assert github.get("/repos/a/b") == {"private": True}
    assert sent == [None, '"e1"']


def test_the_config_refuses_a_strange_repository_or_label(tmp_path: Path) -> None:
    path = tmp_path / "c.json"
    good = {"repos": [APP], "label": LABEL, "variable": "CI_RUNNER", "slots": 2,
            "sandbox_unit": "sandbox.service"}
    path.write_text(json.dumps(good))
    assert dispatcher.load_config(path) == good
    for change in ({"repos": ["a/b; rm"]}, {"label": "x y"}, {"variable": "x/../y"}, {"slots": 9},
                   {"job_env": {"A B": "1"}}, {"sandbox_check": "nft list ruleset"}):
        path.write_text(json.dumps(good | change))
        with pytest.raises(SystemExit):
            dispatcher.load_config(path)


def test_the_config_refuses_to_run_without_a_sandbox_unit(tmp_path: Path) -> None:
    path = tmp_path / "c.json"
    for unit in ({}, {"sandbox_unit": ""}):
        path.write_text(json.dumps({"repos": [APP], "label": LABEL, "variable": "CI_RUNNER", "slots": 2} | unit))
        with pytest.raises(SystemExit, match="sandbox_unit"):
            dispatcher.load_config(path)


@pytest.mark.parametrize(
    ("expiry", "soon"),
    [(None, False), ("2026-11-20 10:00:00 UTC", True), ("2027-01-31 12:00:00 +0200", False)],
)
def test_token_expiry(expiry: str | None, soon: bool) -> None:
    assert dispatcher.token_expires_soon(expiry, NOW) is soon


def test_warming_pulls_once_into_its_own_store_and_writes_the_files(config: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    config["warm_images"] = ["docker.io/library/postgres:18@sha256:" + "a" * 64]
    monkeypatch.setattr(dispatcher, "publish_images", lambda staging, public: None)
    calls: list[tuple[str, ...]] = []

    class Account(FakeAccount):
        def podman(self, *args: str, check: bool = True, timeout: int = 120) -> str:
            calls.append(args)
            return ""

    account = Account([runner("a", pool="main", slot=1)])
    assert dispatcher_for(config, FakeGitHub(), account).warm() == 0
    assert len(calls) == 1
    args = calls[0]
    mounts = [args[i + 1] for i, part in enumerate(args) if part == "--volume"]
    assert mounts == ["/home/ci-runner/staging:/ci-runner-images", "ci-runner-docker-images:/var/lib/docker"]
    assert "--replace" in args
    assert args[args.index("--name") + 1] == "ci-runner-images"
    assert f"{dispatcher.runner_label()}=1" not in args
    assert "--privileged" not in args and args[-1] == config["warm_images"][0]


def test_a_container_not_started_yet_takes_its_age_from_its_creation(monkeypatch: pytest.MonkeyPatch) -> None:
    account = dispatcher.Account.__new__(dispatcher.Account)
    created = int(NOW.timestamp())
    listed = [{"Names": ["ci-runner-main-1"], "StartedAt": -62135596800, "Created": created,
               "Labels": {"ci-runner.pool": "main", "ci-runner.slot": "1", "ci-runner.name": "a"}}]
    monkeypatch.setattr(account, "podman", lambda *_args, **_kwargs: json.dumps(listed), raising=False)
    (found,) = account.runners()
    assert found.started == NOW
    assert (found.container, found.pool, found.slot) == ("ci-runner-main-1", "main", 1)


def test_a_runner_that_ended_its_job_is_left_to_tidy_and_does_not_wait(config: dict[str, Any]) -> None:
    github = FakeGitHub(queued={(APP, LABEL): 1})
    account = FakeAccount([runner("done", age=timedelta(minutes=12))])
    poll(config, github, account)
    assert account.stopped == []
    assert started(account) == [("ci-runner-main-2", "main", 2)]


def test_a_surplus_runner_is_stopped_after_a_few_minutes(config: dict[str, Any]) -> None:
    github = FakeGitHub(registered={APP: [{"id": 1, "name": "a"}, {"id": 2, "name": "b"}]},
                        queued={(APP, LABEL): 1})
    account = FakeAccount([runner("a", slot=1, age=dispatcher.SURPLUS_AFTER),
                           runner("b", slot=2, age=timedelta(minutes=1))])
    poll(config, github, account)
    assert account.stopped == ["ci-runner-main-1"]
    assert account.started == []


def test_a_runner_not_beyond_the_queue_is_kept(config: dict[str, Any]) -> None:
    github = FakeGitHub(registered={APP: [{"id": 1, "name": "a"}]}, queued={(APP, LABEL): 1})
    account = FakeAccount([runner("a", age=timedelta(minutes=5))])
    poll(config, github, account)
    assert account.stopped == []


def test_idle_runners_are_stopped_while_paused(config: dict[str, Any], tmp_path: Path) -> None:
    (tmp_path / "paused").touch()
    github = FakeGitHub(registered={APP: [{"id": 1, "name": "a"}]})
    account = FakeAccount([runner("a", age=dispatcher.SURPLUS_AFTER)])
    poll(config, github, account)
    assert account.stopped == ["ci-runner-main-1"]


def test_a_nameless_container_over_the_time_is_stopped(config: dict[str, Any]) -> None:
    left = dispatcher.Runner("ci-runner-main-2", "", "", "main", 2, NOW - timedelta(hours=2))
    account = FakeAccount([left])
    poll(config, FakeGitHub(), account)
    assert account.stopped == ["ci-runner-main-2"]


def test_main_jobs_of_every_repository_come_before_pull_requests(config: dict[str, Any]) -> None:
    config["slots"] = 3
    github = FakeGitHub(queued={(APP, PR): 5, (CORPUS, LABEL): 1, (APP, LABEL): 1})
    account = FakeAccount()
    poll(config, github, account)
    pools = [pool for _, pool, _ in started(account)]
    assert pools == ["main", "main", "pr"]


def test_pull_requests_never_take_the_last_slot(config: dict[str, Any]) -> None:
    config["slots"] = 4
    github = FakeGitHub(queued={(APP, PR): 9})
    account = FakeAccount()
    poll(config, github, account)
    assert sorted(slot for _, _, slot in started(account)) == [1, 2, 3]


def test_repositories_take_turns_within_a_pool(config: dict[str, Any]) -> None:
    config["slots"] = 4
    github = FakeGitHub(queued={(APP, LABEL): 5, (CORPUS, LABEL): 5})
    poll(config, github, FakeAccount())
    assert [repo for repo, _, _ in github.made] == [APP, CORPUS, APP, CORPUS]


def test_a_slot_just_given_is_not_given_again_before_its_container_shows(config: dict[str, Any]) -> None:
    github, account = FakeGitHub(queued={(APP, LABEL): 1}), FakeAccount()
    d = dispatcher_for(config, github, account)
    d.poll(NOW)
    d.poll(NOW + timedelta(seconds=20))
    assert [slot for _, _, slot in started(account)] == [1, 2]


def test_a_failed_start_frees_its_slot_and_does_not_stop_the_others(config: dict[str, Any]) -> None:
    class Failing(FakeAccount):
        @override
        def start(self, command: list[str], unit: str, max_seconds: int) -> None:
            if unit == "ci-runner-main-1":
                raise dispatcher.subprocess.CalledProcessError(1, "systemd-run")
            super().start(command, unit, max_seconds)

    account = Failing()
    d = dispatcher_for(config, FakeGitHub(queued={(APP, LABEL): 2}), account)
    d.poll(NOW)
    assert [slot for _, _, slot in started(account)] == [2]
    assert set(d.claims) == {2}


def test_a_public_repository_is_retired_when_the_runners_cannot_be_listed(config: dict[str, Any]) -> None:
    class Broken(FakeAccount):
        @override
        def runners(self) -> Never:
            raise dispatcher.subprocess.CalledProcessError(1, "podman")

    github = FakeGitHub(private={CORPUS: False}, registered={CORPUS: [{"id": 4, "name": "x"}]})
    github.variables = {CORPUS: LABEL}
    poll(config, github, Broken())
    assert github.removed == [(CORPUS, 4)] and github.variables == {}
    assert github.made == []


ID = "b" * 64


def staged(tmp_path: Path, entries: list[tuple[str, str, bytes]]) -> Path:
    staging = tmp_path / "staging"
    staging.mkdir()
    for _, name, content in entries:
        (staging / name).write_bytes(content)
    (staging / "index").write_text("".join(f"{image}\t{name}\n" for image, name, _ in entries))
    return staging


def test_image_files_are_copied_to_roots_directory_readable_by_all(tmp_path: Path) -> None:
    staging = staged(tmp_path, [("docker.io/library/postgres:18@sha256:" + "a" * 64, f"{ID}.tar", b"x")])
    public = tmp_path / "public"
    public.mkdir()
    (public / "old.tar").write_bytes(b"gone")
    dispatcher.publish_images(staging, public)
    assert sorted(p.name for p in public.iterdir()) == [f"{ID}.tar", "index"]
    assert (public / f"{ID}.tar").read_bytes() == b"x"
    assert (public / f"{ID}.tar").stat().st_mode & 0o777 == 0o644
    assert (public / "index").read_text().endswith(f"\t{ID}.tar\n")


def test_an_image_file_that_is_a_link_is_refused(tmp_path: Path) -> None:
    staging = staged(tmp_path, [])
    (staging / f"{ID}.tar").symlink_to("/etc/hostname")
    (staging / "index").write_text(f"docker.io/library/x:1\t{ID}.tar\n")
    with pytest.raises(OSError, match=rf"\[Errno {errno.ELOOP}\]"):
        dispatcher.publish_images(staging, tmp_path / "public")
    assert not (tmp_path / "public" / f"{ID}.tar").exists()


@pytest.mark.parametrize("line", ["x:1\t../../etc/passwd", "x:1\tindex", "x y\t" + ID + ".tar"])
def test_a_strange_index_entry_is_refused(tmp_path: Path, line: str) -> None:
    staging = staged(tmp_path, [])
    (staging / "index").write_text(line + "\n")
    with pytest.raises(ValueError, match="not an image file entry"):
        dispatcher.publish_images(staging, tmp_path / "public")


def test_a_warming_that_runs_too_long_stops_its_container(config: dict[str, Any]) -> None:
    calls: list[tuple[str, ...]] = []

    class Slow(FakeAccount):
        def podman(self, *args: str, check: bool = True, timeout: int = 120) -> str:
            calls.append(args)
            if args[0] == "run":
                raise dispatcher.subprocess.TimeoutExpired("podman", timeout)
            return ""

    config["warm_images"] = ["docker.io/library/x:1"]
    assert dispatcher_for(config, FakeGitHub(), Slow()).warm() == 1
    assert calls[-1][0] == "stop" and calls[-1][-1] == dispatcher.images_container()


def test_the_answer_cache_keeps_only_what_the_last_poll_asked() -> None:
    github = dispatcher.GitHub("t")
    github.cache = {"a": ("e", 1), "b": ("e", 2)}
    github.used = {"a"}
    github.forget_unused()
    assert github.cache == {"a": ("e", 1)} and github.used == set()


def test_a_slot_just_given_to_a_pull_request_counts_toward_its_limit(config: dict[str, Any]) -> None:
    config["slots"] = 4
    github, account = FakeGitHub(queued={(APP, PR): 9}), FakeAccount()
    d = dispatcher_for(config, github, account)
    d.poll(NOW)
    d.poll(NOW + timedelta(seconds=20))
    assert sorted(slot for _, _, slot in started(account)) == [1, 2, 3]


def test_jobs_of_many_runs_are_counted_whatever_order_they_come_in(monkeypatch: pytest.MonkeyPatch) -> None:
    github = dispatcher.GitHub("t")
    runs = {"queued": [{"id": n} for n in range(10)], "in_progress": []}
    answers = {f"/repos/r/x/actions/runs/{n}/jobs": {"jobs": [{"status": "queued", "labels": [PR]}]}
               for n in range(10)}

    def get(path: str, **q: str) -> dict[str, Any]:
        if path.endswith("/actions/runs"):
            return {"workflow_runs": runs[q["status"]]}
        return answers[path]

    monkeypatch.setattr(github, "get", get)
    assert dispatcher.queued_counts(github.jobs("r/x", [LABEL, PR]), [LABEL, PR]) == {LABEL: 0, PR: 10}


def test_a_call_that_gets_no_answer_is_logged_with_its_path(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    github = dispatcher.GitHub("secret-token")

    def urlopen(request: urllib.request.Request, timeout: float) -> Never:
        raise TimeoutError("timed out")

    monkeypatch.setattr(dispatcher.urllib.request, "urlopen", urlopen)
    with pytest.raises(TimeoutError):
        github.get("/repos/a/b/actions/runs", status="queued")
    out = capsys.readouterr().out
    assert "GitHub GET /repos/a/b/actions/runs failed" in out
    assert "secret-token" not in out


def test_a_slow_call_is_logged_with_its_path(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    github = dispatcher.GitHub("t")
    clock = iter([100.0, 107.0])
    monkeypatch.setattr(dispatcher.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(github, "answer", lambda *_args: {"private": True})
    assert github.get("/repos/a/b") == {"private": True}
    assert "GitHub GET /repos/a/b took 7 s" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("took", "left", "pause"),
    [
        (3, None, 17),
        (21, 4000, dispatcher.MIN_PAUSE),
        (3, 100, 60),
    ],
)
def test_the_pause_before_the_next_poll(took: float, left: int | None, pause: float) -> None:
    assert dispatcher.pause_after(took, 20, left) == pause


def test_a_container_gone_wakes_the_next_poll() -> None:
    watcher = dispatcher.ExitWatcher(["printf", "ci-runner-pr-2\\n"])
    assert not watcher.wake.is_set()
    watcher.watch()
    assert watcher.wake.is_set()


def test_a_watcher_that_cannot_start_only_logs(capsys: pytest.CaptureFixture[str]) -> None:
    watcher = dispatcher.ExitWatcher(["/nonexistent/podman", "events"])
    watcher.watch()
    assert not watcher.wake.is_set()
    assert "could not watch for runners ending" in capsys.readouterr().out


def test_the_watcher_follows_only_the_runners_being_removed() -> None:
    account = dispatcher.Account.__new__(dispatcher.Account)
    account.user, account.uid, account.home = "ci-runner", 1005, "/home/ci-runner"
    command = dispatcher.exit_watcher(account).command
    assert command[:4] == [dispatcher.RUNUSER, "-u", "ci-runner", "--"]
    assert f"label={dispatcher.runner_label()}=1" in command
    assert "event=remove" in command


# Where a job's time went: the timing records.


class Clock:
    """The time the records take, moved by the test."""

    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    clock = Clock()
    monkeypatch.setattr(dispatcher, "utc_now", clock)
    return clock


@pytest.fixture
def timed(config: dict[str, Any], tmp_path: Path, clock: Clock) -> dict[str, Any]:
    timing = tmp_path / "timing"
    timing.mkdir(mode=0o700)
    return config | {"timing_dir": str(timing), "timing_keep_days": 90}


def records(config: dict[str, Any], kind: str = "job") -> list[dict[str, Any]]:
    found = []
    for path in sorted(Path(config["timing_dir"]).glob("*.jsonl")):
        found += [json.loads(line) for line in path.read_text().splitlines()]
    return [record for record in found if record["kind"] == kind]


def done(job_id: int, runner_name: str | None, conclusion: str = "success") -> dict[str, Any]:
    return {
        "id": job_id, "run_id": 7, "run_attempt": 1, "status": "completed", "conclusion": conclusion,
        "labels": [LABEL], "workflow_name": "checks", "name": "test", "runner_name": runner_name,
        "created_at": "2026-11-12T09:59:50Z", "started_at": "2026-11-12T10:01:00Z",
        "completed_at": "2026-11-12T10:05:00Z",
        "steps": [{"name": "Set up job", "started_at": "2026-11-12T10:01:05Z"}],
    }


def test_a_queued_job_is_noted_when_first_seen(timed: dict[str, Any]) -> None:
    poll(timed, FakeGitHub(queued={(APP, LABEL): 1}), FakeAccount())
    pending = json.loads((Path(timed["timing_dir"]) / "pending.json").read_text())
    assert pending["jobs"]["1000"]["seen"] == "2026-11-12T10:00:00Z"
    assert pending["jobs"]["1000"]["pool"] == "main"


def test_a_job_gets_the_times_of_the_runner_that_ran_it_not_of_the_one_started_for_it(
    timed: dict[str, Any], clock: Clock,
) -> None:
    running = done(5, None) | {"status": "in_progress", "conclusion": None}
    github, account = FakeGitHub(queued={(APP, LABEL): 1}, listed={APP: [running]}), FakeAccount()
    d = dispatcher_for(timed, github, account)
    d.poll(NOW)
    (_, name, _), = github.made
    # The runner started for job 1000 took job 5, and 1000 was cancelled.
    clock.now = NOW + timedelta(minutes=6)
    github.queued, github.listed = {}, {APP: [done(5, name)]}
    github.answers[1000] = done(1000, None, "cancelled") | {"steps": [], "started_at": None}
    d.poll(clock.now)
    by_id = {record["job_id"]: record for record in records(timed)}
    ran = by_id[5]
    assert (ran["runner"], ran["slot"], ran["conclusion"]) == (name, 1, "success")
    assert ran["slot_given"] == ran["registered"] == ran["unit_started"] == "2026-11-12T10:00:00Z"
    assert ran["job_started"] == "2026-11-12T10:01:05Z"
    assert ran["seen"] is None
    cancelled = by_id[1000]
    assert cancelled["seen"] == "2026-11-12T10:00:00Z"
    assert (cancelled["runner"], cancelled["slot"], cancelled["slot_given"], cancelled["job_started"]) == (None,) * 4
    assert cancelled["conclusion"] == "cancelled"
    # Once, after the runners were started.
    assert github.asked == [1000]
    assert json.loads((Path(timed["timing_dir"]) / "pending.json").read_text()) == {"jobs": {}, "runners": {}}


def test_what_is_pending_survives_a_new_dispatcher(timed: dict[str, Any], clock: Clock) -> None:
    poll(timed, FakeGitHub(queued={(APP, LABEL): 1}), FakeAccount())
    clock.now = NOW + timedelta(minutes=5)
    poll(timed, FakeGitHub(listed={APP: [done(1000, None)]}), FakeAccount())
    record, = records(timed)
    assert record["seen"] == "2026-11-12T10:00:00Z"


def test_a_job_github_no_longer_has_is_written_with_what_is_known(timed: dict[str, Any]) -> None:
    github = FakeGitHub(queued={(APP, LABEL): 1})
    d = dispatcher_for(timed, github, FakeAccount())
    d.poll(NOW)
    github.queued = {}
    github.answers[1000] = urllib.error.HTTPError("u", 404, "Not Found", Message(), None)
    d.poll(NOW)
    record, = records(timed)
    assert (record["job_id"], record["conclusion"], record["seen"]) == (1000, None, "2026-11-12T10:00:00Z")


def test_a_failed_look_up_keeps_the_job_pending(timed: dict[str, Any]) -> None:
    github = FakeGitHub(queued={(APP, LABEL): 1})
    d = dispatcher_for(timed, github, FakeAccount())
    d.poll(NOW)
    github.queued = {}
    github.answers[1000] = urllib.error.URLError("down")
    d.poll(NOW)
    assert records(timed) == []
    assert "1000" in d.timing.jobs


def test_a_job_pending_too_long_is_written_and_dropped(timed: dict[str, Any], clock: Clock) -> None:
    github = FakeGitHub(queued={(APP, LABEL): 1})
    d = dispatcher_for(timed, github, FakeAccount([runner("busy-1"), runner("busy-2", slot=2)]))
    d.poll(NOW)
    clock.now = NOW + timedelta(seconds=timed["job_max_seconds"]) + timedelta(hours=1, seconds=1)
    d.poll(clock.now)
    record, = records(timed)
    assert record["job_id"] == 1000
    assert d.timing.jobs == {}


def test_a_runner_stopped_unused_is_recorded_with_why(timed: dict[str, Any]) -> None:
    registered = {APP: [{"id": 1, "name": "r1", "busy": False}, {"id": 2, "name": "r2", "busy": False}]}
    account = FakeAccount([runner("r1", age=timedelta(minutes=11)), runner("r2", slot=2, age=timedelta(minutes=4))])
    poll(timed, FakeGitHub(registered=registered), account)
    idle = {record["runner"]: record for record in records(timed, "idle")}
    assert idle["r1"]["why"] == "idle"
    assert idle["r2"]["why"] == "surplus"
    assert idle["r2"]["started"] == "2026-11-12T09:56:00Z"
    assert idle["r2"]["stopped"] == "2026-11-12T10:00:00Z"


def test_a_poll_sample_is_written_on_change_and_every_few_minutes(timed: dict[str, Any], clock: Clock) -> None:
    github = FakeGitHub(queued={(APP, LABEL): 3})
    d = dispatcher_for(timed, github, FakeAccount())
    d.poll(NOW)
    d.sample(1.23)
    d.sample(2.0)
    clock.now = NOW + timedelta(minutes=5)
    d.sample(2.0)
    github.queued = {}
    d.poll(clock.now)
    d.sample(0.5)
    samples = records(timed, "poll")
    assert [sample["t"] for sample in samples] == ["2026-11-12T10:00:00Z", "2026-11-12T10:05:00Z", "2026-11-12T10:05:00Z"]
    first = samples[0]
    assert first == {"v": 1, "kind": "poll", "t": "2026-11-12T10:00:00Z", "took_s": 1.2, "queued": {"main": 3, "pr": 0},
                     "busy": 0, "slots": 2, "waiting": 1, "sandbox": True, "paused": False, "rate_low": False,
                     "rate_left": None}
    assert samples[2]["queued"] == {"main": 0, "pr": 0}


def test_days_past_the_retention_are_removed(timed: dict[str, Any]) -> None:
    directory = Path(timed["timing_dir"])
    for name in ("2026-08-14.jsonl", "2026-08-15.jsonl", "notes.txt"):
        (directory / name).write_text("")
    poll(timed, FakeGitHub(), FakeAccount())
    assert sorted(path.name for path in directory.iterdir()) == ["2026-08-15.jsonl", "notes.txt"]


def test_a_write_that_fails_does_not_stop_the_poll(
    timed: dict[str, Any], tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    config = timed | {"timing_dir": str(tmp_path / "missing")}
    account = FakeAccount()
    d = dispatcher_for(config, FakeGitHub(queued={(APP, LABEL): 2}), account)
    d.poll(NOW)
    d.sample(1.0)
    d.poll(NOW)
    assert len(account.started) == 2
    assert capsys.readouterr().out.count("timing:") == 1


def test_keep_days_zero_records_nothing(timed: dict[str, Any]) -> None:
    config = timed | {"timing_keep_days": 0}
    d = poll(config, FakeGitHub(queued={(APP, LABEL): 1}), FakeAccount())
    d.sample(1.0)
    assert list(Path(timed["timing_dir"]).iterdir()) == []


def test_the_timing_files_are_roots_alone(timed: dict[str, Any]) -> None:
    d = poll(timed, FakeGitHub(queued={(APP, LABEL): 1}), FakeAccount())
    d.sample(1.0)
    paths = list(Path(timed["timing_dir"]).iterdir())
    assert {path.name for path in paths} == {"pending.json", "2026-11-12.jsonl"}
    assert {path.stat().st_mode & 0o777 for path in paths} == {0o600}


@pytest.mark.parametrize(
    "content",
    [
        "[1, 2",
        "[]",
        '{"jobs": {"1": {"job_id": 2, "noted": "x"}, "3": {"job_id": 3}, "4": 4}, "runners": {"r": {"slot": 1}}}',
    ],
)
def test_a_strange_pending_file_is_ignored(timed: dict[str, Any], content: str) -> None:
    (Path(timed["timing_dir"]) / "pending.json").write_text(content)
    d = poll(timed, FakeGitHub(queued={(APP, LABEL): 1}), FakeAccount())
    assert list(d.timing.jobs) == ["1000"]
    assert "r" not in d.timing.runners


@pytest.mark.parametrize(
    ("value", "kept"),
    [
        ("2026-11-12T10:00:00Z", "2026-11-12T10:00:00Z"),
        ("2026-11-12T12:00:00+02:00", "2026-11-12T10:00:00Z"),
        ("yesterday", None),
        (12, None),
        (None, None),
    ],
)
def test_only_times_are_taken_from_github(value: object, kept: str | None) -> None:
    assert dispatcher.github_time(value) == kept


def test_names_are_cut_and_strange_fields_left_out() -> None:
    fields = dispatcher.job_fields({"id": True, "run_id": "7", "name": "x" * 500, "workflow_name": ["a"]})
    assert fields == {"job": "x" * dispatcher.NAME_MAX}


def test_calls_left_make_a_sample_new_only_when_they_run_low(timed: dict[str, Any], clock: Clock) -> None:
    github = FakeGitHub()
    d = dispatcher_for(timed, github, FakeAccount())
    d.poll(NOW)
    for left in (4000, 3990, 3000, 400):
        github.rate_remaining = left
        d.sample(1.0)
    assert [sample["rate_left"] for sample in records(timed, "poll")] == [4000, 400]


def test_a_paused_poll_says_so_in_its_sample(timed: dict[str, Any], tmp_path: Path) -> None:
    (tmp_path / "paused").touch()
    d = poll(timed, FakeGitHub(queued={(APP, LABEL): 1}), FakeAccount())
    d.sample(1.0)
    sample, = records(timed, "poll")
    assert (sample["paused"], sample["queued"]["main"], sample["waiting"]) == (True, 1, 0)


def ended(timed: dict[str, Any], count: int) -> tuple[dispatcher.Dispatcher, FakeGitHub]:
    """A dispatcher with count pending jobs whose runs have ended."""
    github = FakeGitHub(listed={APP: [done(n, None) | {"status": "queued"} for n in range(count)]})
    d = dispatcher_for(timed, github, FakeAccount([runner(f"busy-{n}", slot=n) for n in (1, 2)]))
    d.poll(NOW)
    github.listed = {}
    github.answers = {n: done(n, None) for n in range(count)}
    return d, github


def test_ended_jobs_are_looked_up_a_few_at_each_poll(timed: dict[str, Any]) -> None:
    d, github = ended(timed, dispatcher.LOOK_UPS + 5)
    d.poll(NOW)
    assert len(github.asked) == dispatcher.LOOK_UPS
    d.poll(NOW)
    assert sorted(github.asked) == list(range(dispatcher.LOOK_UPS + 5))
    assert len(records(timed)) == dispatcher.LOOK_UPS + 5


def test_ended_jobs_are_not_looked_up_while_calls_are_few(timed: dict[str, Any]) -> None:
    d, github = ended(timed, 2)
    github.rate_remaining = dispatcher.RATE_LOW - 1
    d.poll(NOW)
    assert github.asked == []
    assert len(d.timing.jobs) == 2
