import json
import subprocess
from pathlib import Path

import pytest
import workbench_rebuild as rebuild


class Podman:
    """The build, podman and systemctl commands the script runs, answered and recorded."""

    def __init__(
        self,
        new: str = "sha256:new",
        containers: dict[str, tuple[str | None, int]] | None = None,
        fail_build: bool = False,
    ) -> None:
        self.new = new
        self.containers = containers or {}
        self.fail_build = fail_build
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str], check: bool = True) -> subprocess.CompletedProcess[str]:
        self.calls.append(argv)
        out, code = "", 0
        if argv[0] == rebuild.REBUILD_IMAGE:
            if self.fail_build:
                raise subprocess.CalledProcessError(1, argv, "", "no space left")
            out = self.new + "\n"
        elif argv[:3] == ["podman", "container", "inspect"]:
            image, _ = self.containers.get(argv[-1], (None, 0))
            out, code = (f"true {image}\n", 0) if image is not None else ("", 125)
        elif argv[:2] == ["podman", "top"]:
            _, count = self.containers[argv[2]]
            lines = ["COMMAND", "/home/bench/.local/bin/claude remote-control --name x"]
            lines += [f"/home/bench/.local/share/claude/versions/2.1 --print --sdk-url https://api/{i}" for i in range(count)]
            out = "\n".join(lines) + "\n"
        return subprocess.CompletedProcess(argv, code, out, "")

    def restarted(self) -> list[str]:
        return [argv[-1] for argv in self.calls if argv[:3] == ["systemctl", "--user", "restart"]]


@pytest.fixture
def config(tmp_path: Path) -> str:
    path = tmp_path / "build.json"
    path.write_text(json.dumps({
        "image": "localhost/workbench:latest",
        "context": "/usr/local/share/workbench/image",
        "options": ["--userns-uid-map=0:0:1"],
        "args": {"UID": "2002", "PACKAGES": "ffmpeg"},
        "containers": ["workbench-shop", "workbench-blog"],
    }))
    return str(path)


def test_builds_through_rebuild_image_from_the_same_config(config: str) -> None:
    podman = Podman()
    assert rebuild.main(["--config", config], podman) == 0
    assert podman.calls[0] == [rebuild.REBUILD_IMAGE, "--config", config]


def test_restarts_an_idle_container_on_an_old_image(config: str) -> None:
    podman = Podman(containers={"workbench-shop": ("sha256:old", 0), "workbench-blog": ("sha256:old", 0)})
    assert rebuild.main(["--config", config], podman) == 0
    assert podman.restarted() == ["workbench-shop.service", "workbench-blog.service"]


def test_leaves_a_container_with_sessions_open(config: str, capsys: pytest.CaptureFixture[str]) -> None:
    podman = Podman(containers={"workbench-shop": ("sha256:old", 2), "workbench-blog": ("sha256:old", 0)})
    rebuild.main(["--config", config], podman)
    assert podman.restarted() == ["workbench-blog.service"]
    assert "workbench-shop: 2 session(s) open" in capsys.readouterr().out


def test_leaves_alone_what_is_current_or_stopped(config: str) -> None:
    podman = Podman(containers={"workbench-shop": ("sha256:new", 0)})
    rebuild.main(["--config", config], podman)
    assert podman.restarted() == []


def test_the_server_alone_is_no_session(config: str) -> None:
    podman = Podman(containers={"workbench-shop": ("sha256:old", 0)})
    assert rebuild.sessions("workbench-shop", podman) == 0


def test_prunes_after_restarting(config: str) -> None:
    podman = Podman(containers={"workbench-shop": ("sha256:old", 0)})
    rebuild.main(["--config", config], podman)
    assert podman.calls[-1] == ["podman", "image", "prune", "--force"]


def test_a_failed_build_restarts_nothing(config: str, capsys: pytest.CaptureFixture[str]) -> None:
    podman = Podman(containers={"workbench-shop": ("sha256:old", 0)}, fail_build=True)
    assert rebuild.main(["--config", config], podman) == 1
    assert podman.restarted() == []
    assert "no space left" in capsys.readouterr().err
