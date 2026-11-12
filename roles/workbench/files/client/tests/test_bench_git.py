import json
import socket
import subprocess
import tempfile
import threading
from pathlib import Path

import bench_git
import pytest


def git(cwd: Path, *args: str) -> str:
    env = {
        "GIT_AUTHOR_NAME": "octo",
        "GIT_AUTHOR_EMAIL": "me@example.com",
        "GIT_COMMITTER_NAME": "octo",
        "GIT_COMMITTER_EMAIL": "me@example.com",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "PATH": "/usr/bin:/bin",
    }
    return subprocess.run(["git", "-C", str(cwd), *args], env=env, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """A workbench checkout named shop, of a GitHub played by a bare repository."""
    origin = tmp_path / "origin.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    work = tmp_path / "shop"
    git(tmp_path, "clone", "-q", str(origin), str(work))
    (work / "README").write_text("start")
    git(work, "add", "README")
    git(work, "commit", "-q", "-m", "Start")
    git(work, "push", "-q", "origin", "HEAD:main")
    git(work, "switch", "-q", "-c", "feat/3-runs")
    (work / "a.py").write_text("a")
    git(work, "add", "a.py")
    git(work, "commit", "-q", "-m", "Add a")
    monkeypatch.chdir(work)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    return {"work": work, "origin": origin}


class Gate:
    """Stands in for the gate on the other end of a socket: records the
    request and its input, and for a push does what the gate would, so the
    client's fetch afterwards finds the branch.
    """

    def __init__(self, origin: Path, status: int = 0, say: str = "", hang_up_early: bool = False) -> None:
        self.origin = origin
        self.status = status
        self.say = say
        self.hang_up_early = hang_up_early
        self.calls: list[tuple[list[str], bytes]] = []

    def __call__(self) -> socket.socket:
        client, server = socket.socketpair()
        threading.Thread(target=self.answer, args=(server,), daemon=True).start()
        return client

    def answer(self, server: socket.socket) -> None:
        with server, server.makefile("rb") as conn:
            request = json.loads(conn.readline())
            given = b"" if self.hang_up_early else conn.read()
            self.calls.append((request, given))
            if self.hang_up_early:
                server.sendall(f"{self.say}status {self.status}\n".encode())
                conn.read()
                return
            if request[0] == "push" and self.status == 0:
                with tempfile.NamedTemporaryFile(suffix=".bundle") as bundle:
                    bundle.write(given)
                    bundle.flush()
                    subprocess.run(["git", "-C", str(self.origin), "fetch", "-q", bundle.name, "+refs/heads/*:refs/heads/*"], check=True)
            server.sendall(f"{self.say}status {self.status}\n".encode())


def test_push_hands_the_gate_a_bundle_of_the_branch(checkout: dict[str, Path]) -> None:
    gate = Gate(checkout["origin"])
    assert bench_git.main(["push"], connect=gate) == 0
    request, bundle = gate.calls[0]
    assert request == ["push", "feat/3-runs"]
    assert bundle.startswith((b"# v2 git bundle", b"# v3 git bundle"))
    assert git(checkout["work"], "rev-parse", "origin/feat/3-runs") == git(checkout["work"], "rev-parse", "HEAD")
    assert git(checkout["work"], "rev-parse", "--abbrev-ref", "@{upstream}") == "origin/feat/3-runs"


def test_force_is_only_a_flag(checkout: dict[str, Path]) -> None:
    gate = Gate(checkout["origin"])
    bench_git.main(["push", "--force"], connect=gate)
    assert gate.calls[0][0] == ["push", "feat/3-runs", "--force"]


def test_a_worktree_sends_its_own_branch(checkout: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tree = tmp_path / "elsewhere"
    git(checkout["work"], "worktree", "add", "-q", "-b", "fix/other", str(tree))
    monkeypatch.chdir(tree)
    gate = Gate(checkout["origin"])
    bench_git.main(["pr", "--title", "x"], connect=gate)
    assert gate.calls[0][0] == ["pr", "fix/other"]


def test_the_body_is_sent_unescaped(checkout: dict[str, Path], tmp_path: Path) -> None:
    body = tmp_path / "body.md"
    body.write_text("Päivä")
    gate = Gate(checkout["origin"])
    bench_git.main(["pr", "--title", "x", "--body-file", str(body)], connect=gate)
    assert "Päivä" in gate.calls[0][1].decode()


def test_a_refused_push_fetches_nothing(checkout: dict[str, Path]) -> None:
    gate = Gate(checkout["origin"], status=1)
    assert bench_git.main(["push"], connect=gate) == 1
    assert subprocess.run(["git", "rev-parse", "-q", "--verify", "origin/feat/3-runs"], check=False, capture_output=True).returncode != 0


def test_pr_sends_the_title_and_the_body_file(checkout: dict[str, Path], tmp_path: Path) -> None:
    body = tmp_path / "body.md"
    body.write_text("What and why.")
    gate = Gate(checkout["origin"])
    assert bench_git.main(["pr", "--title", "Adapt the runs", "--body-file", str(body)], connect=gate) == 0
    request, given = gate.calls[0]
    assert request == ["pr", "feat/3-runs"]
    assert json.loads(given) == {"title": "Adapt the runs", "body": "What and why."}


def test_pr_edit_sends_only_what_changes(checkout: dict[str, Path]) -> None:
    gate = Gate(checkout["origin"])
    bench_git.main(["pr-edit", "12", "--title", "Adapt the runs"], connect=gate)
    request, given = gate.calls[0]
    assert request == ["pr-edit", "12"]
    assert json.loads(given) == {"title": "Adapt the runs"}


def test_refuses_a_detached_head(checkout: dict[str, Path]) -> None:
    git(checkout["work"], "switch", "-q", "--detach")
    with pytest.raises(SystemExit, match="no branch"):
        bench_git.main(["push"], connect=Gate(checkout["origin"]))


def test_prints_why_the_gate_refused(checkout: dict[str, Path], capsys: pytest.CaptureFixture[str]) -> None:
    gate = Gate(checkout["origin"], status=1, say="refused: author Someone Else\n", hang_up_early=True)
    assert bench_git.main(["push"], connect=gate) == 1
    assert "author Someone Else" in capsys.readouterr().err


def test_a_gate_that_hangs_up_is_a_failure(checkout: dict[str, Path], capsys: pytest.CaptureFixture[str]) -> None:
    def silent() -> socket.socket:
        client, server = socket.socketpair()
        server.close()
        return client

    assert bench_git.main(["check"], connect=silent) == 1
    assert "hung up" in capsys.readouterr().err


def test_check_sends_only_the_action(checkout: dict[str, Path]) -> None:
    gate = Gate(checkout["origin"], say='{"push": true}\n')
    assert bench_git.main(["check"], connect=gate) == 0
    assert gate.calls[0] == (["check"], b"")
