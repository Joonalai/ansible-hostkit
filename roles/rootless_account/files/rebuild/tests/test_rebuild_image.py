import datetime as dt
import json
import subprocess
from pathlib import Path

import pytest
import rebuild_image

MONDAY = dt.date(2026, 10, 26)


class Podman:
    """The podman commands the script runs, answered and recorded."""

    def __init__(self, fail_build: bool = False) -> None:
        self.fail_build = fail_build
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(argv)
        if argv[:2] == ["podman", "build"] and self.fail_build:
            raise subprocess.CalledProcessError(1, argv, "", "no space left")
        out = "sha256:new\n" if argv[:3] == ["podman", "image", "inspect"] else ""
        return subprocess.CompletedProcess(argv, 0, out, "")


@pytest.fixture
def config(tmp_path: Path) -> str:
    path = tmp_path / "build.json"
    path.write_text(json.dumps({
        "image": "localhost/workbench:latest",
        "context": "/usr/local/share/workbench/image",
        "options": ["--userns-uid-map=0:0:1"],
        "args": {"UID": "2002", "PACKAGES": "ffmpeg"},
        "containers": ["workbench-app"],
    }))
    return str(path)


def test_builds_with_the_plays_arguments_and_the_week(config: str) -> None:
    podman = Podman()
    assert rebuild_image.main(["--config", config], podman, MONDAY) == 0
    argv = podman.calls[0]
    assert argv[:3] == ["podman", "build", "--pull=newer"]
    assert "--userns-uid-map=0:0:1" in argv
    assert "--build-arg=PACKAGES=ffmpeg" in argv
    assert "--build-arg=REFRESH=2026-W44" in argv
    assert argv[-3:] == ["--tag", "localhost/workbench:latest", "/usr/local/share/workbench/image"]


def test_the_week_is_the_plays_to_not_override(config: str, tmp_path: Path) -> None:
    path = tmp_path / "stale.json"
    with open(config) as f:
        path.write_text(json.dumps({**json.load(f), "args": {"REFRESH": "pinned"}}))
    podman = Podman()
    rebuild_image.main(["--config", str(path)], podman, MONDAY)
    assert "--build-arg=REFRESH=2026-W44" in podman.calls[0]
    assert "--build-arg=REFRESH=pinned" not in podman.calls[0]


def test_the_week_turns_on_monday() -> None:
    assert rebuild_image.week(dt.date(2026, 11, 1)) == "2026-W44"
    assert rebuild_image.week(dt.date(2026, 11, 2)) == "2026-W45"
    assert rebuild_image.week(dt.date(2027, 1, 1)) == "2026-W53"


def test_prints_the_images_id(config: str, capsys: pytest.CaptureFixture[str]) -> None:
    rebuild_image.main(["--config", config], Podman(), MONDAY)
    assert capsys.readouterr().out == "sha256:new\n"


def test_a_failed_build_says_why(config: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert rebuild_image.main(["--config", config], Podman(fail_build=True), MONDAY) == 1
    assert "no space left" in capsys.readouterr().err
