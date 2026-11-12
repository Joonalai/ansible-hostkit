import json
import subprocess
from typing import Any

import pytest
import refresh_runners

IMAGE = "sha-new"
STARTED = "2026-10-22T05:24:33.123456789Z"
STARTED_S = 1792646673.123456


def done(stdout: str = "", returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


def container(image: str = IMAGE, started: str = STARTED) -> str:
    return json.dumps([{"Image": image, "State": {"StartedAt": started}}])


class Podman:
    """Containers by unit, each with its image, start and processes."""

    def __init__(self, containers: dict[str, dict[str, Any]], image_ok: bool = True) -> None:
        self.containers = containers
        self.image_ok = image_ok

    def __call__(self, *args: str) -> subprocess.CompletedProcess[str]:
        if args[:2] == ("image", "inspect"):
            return done(IMAGE + "\n") if self.image_ok else done(returncode=125, stderr="no such image")
        if args[:2] == ("container", "inspect"):
            found = self.containers.get(args[-1])
            return done(found["inspect"]) if found is not None else done("[]", 125, "no such container")
        if args[0] == "top":
            return done("COMMAND\nrun-helper.sh\nRunner.Listener\n" + ("Runner.Worker\n" if self.containers[args[1]]["busy"] else ""))
        raise AssertionError(args)


class Restarts(list[tuple[str, ...]]):
    def __call__(self, *args: str) -> subprocess.CompletedProcess[str]:
        self.append(args)
        return done()


def run(podman: Podman, argv: list[str], mtimes: dict[str, float] | None = None) -> tuple[int, Restarts]:
    restarts = Restarts()
    code = refresh_runners.main(argv, run=podman, restart=restarts, mtime=lambda path: (mtimes or {}).get(path, 0))
    return code, restarts


def test_podman_start_times_parse_with_nanoseconds_and_offsets() -> None:
    assert refresh_runners.started(STARTED) == STARTED_S
    assert refresh_runners.started("2026-10-22T08:24:33.123456789+03:00") == STARTED_S
    assert refresh_runners.started("2026-10-22T05:24:33Z") == 1792646673


def test_a_container_is_stale_on_another_image_or_older_than_a_file() -> None:
    inspected = json.loads(container())[0]
    assert refresh_runners.why_stale(inspected, IMAGE, {"/c/CLAUDE.md": STARTED_S - 60}) is None
    assert refresh_runners.why_stale(inspected, "sha-newer", {}) == "an older image"
    assert refresh_runners.why_stale(inspected, IMAGE, {"/c/CLAUDE.md": STARTED_S + 60}) == "an older CLAUDE.md"


def test_only_idle_stale_runners_are_restarted(capsys: pytest.CaptureFixture[str]) -> None:
    podman = Podman(
        {
            "r-1": {"inspect": container(image="sha-old"), "busy": False},
            "r-2": {"inspect": container(image="sha-old"), "busy": True},
            "r-3": {"inspect": container(), "busy": False},
        },
    )
    code, restarts = run(podman, ["--image", "img", "r-1", "r-2", "r-3", "r-4"])

    assert code == 0
    assert restarts == [("restart", "r-1.service")]
    out = capsys.readouterr().out
    assert "r-1: on an older image and idle, restarted" in out
    assert "r-2: on an older image, but working a job" in out
    assert "r-3: current" in out
    assert "r-4: between jobs" in out


def test_a_rewritten_rules_file_restarts_an_idle_runner() -> None:
    podman = Podman({"r-1": {"inspect": container(), "busy": False}})
    _, restarts = run(podman, ["--image", "img", "r-1=/c/CLAUDE.md,/q/r-1.container"], {"/c/CLAUDE.md": STARTED_S + 1})

    assert restarts == [("restart", "r-1.service")]


def test_a_dry_run_restarts_nothing(capsys: pytest.CaptureFixture[str]) -> None:
    podman = Podman({"r-1": {"inspect": container(image="sha-old"), "busy": False}})
    _, restarts = run(podman, ["--image", "img", "--dry-run", "r-1"])

    assert restarts == []
    assert "would restart" in capsys.readouterr().out


def test_a_missing_image_fails_before_touching_anything() -> None:
    podman = Podman({"r-1": {"inspect": container(image="sha-old"), "busy": False}}, image_ok=False)
    code, restarts = run(podman, ["--image", "img", "r-1"])

    assert code == 1
    assert restarts == []


def test_a_failed_restart_stops_none_of_the_others(capsys: pytest.CaptureFixture[str]) -> None:
    podman = Podman({f"r-{n}": {"inspect": container(image="sha-old"), "busy": False} for n in (1, 2)})
    restarts: list[tuple[str, ...]] = []

    def restart(*args: str) -> subprocess.CompletedProcess[str]:
        restarts.append(args)
        return done(returncode=1, stderr="the control process exited") if len(restarts) == 1 else done()

    code = refresh_runners.main(["--image", "img", "r-1", "r-2"], run=podman, restart=restart, mtime=lambda path: 0)

    assert code == 0
    assert restarts == [("restart", "r-1.service"), ("restart", "r-2.service")]
    out = capsys.readouterr().out
    assert "r-1: restart failed, and systemd tries again: the control process exited" in out
    assert "r-2: on an older image and idle, restarted" in out
