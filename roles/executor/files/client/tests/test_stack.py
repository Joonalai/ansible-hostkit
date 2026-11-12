import io
import json
import socket
import subprocess
import threading
from pathlib import Path
from typing import Any

import pytest
import stack

ENV = {"STACK_KEY": "k", "STACK_SOCKET": "/run/stack.sock", "STACK_LOCAL_HOST": "host.containers.internal"}


class Instance:
    """Stands in for the instance at the socket's other end, answering as told."""

    def __init__(self, answer: list[dict[str, Any]]) -> None:
        self.answer = answer
        self.header: dict[str, Any] = {}
        self.body = b""
        self.paths: list[str] = []

    def connect(self, path: str) -> socket.socket:
        self.paths.append(path)
        mine, theirs = socket.socketpair()
        threading.Thread(target=self.serve, args=(theirs,), daemon=True).start()
        return mine

    def serve(self, conn: socket.socket) -> None:
        with conn, conn.makefile("rb") as incoming:
            self.header = json.loads(incoming.readline())
            self.body = incoming.read()
            for event in self.answer:
                conn.sendall(json.dumps(event).encode() + b"\n")


def checkout(tmp_path: Path, origin: str = "git@github.com:example/app.git") -> Path:
    path = tmp_path / "app"
    path.mkdir()
    subprocess.run(["git", "init", "--quiet", str(path)], check=True)
    (path / "a").write_text("a")
    subprocess.run(["git", "-C", str(path), "add", "a"], check=True)
    subprocess.run(["git", "-C", str(path), "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qm", "one"], check=True)
    subprocess.run(["git", "-C", str(path), "remote", "add", "origin", origin], check=True)
    return path


UP = {
    "exit": 0,
    "commit": "c" * 40,
    "page": 2,
    "ports": [9302, 9303, 9304],
    "host": "stacks.example.ts.net",
    "control": "http://stacks.example.ts.net:9301/",
}


def test_up_sends_heads_bundle_and_prints_where_the_stack_is(tmp_path: Path) -> None:
    path = checkout(tmp_path)
    instance = Instance([{"out": "building"}, UP])
    out = io.StringIO()

    status = stack.main(["up", "-C", str(path)], ENV, out, instance.connect)

    assert status == 0
    assert instance.header == {"key": "k", "action": "up", "repo": "example/app", "profile": "default", "bundle_size": len(instance.body)}
    assert instance.body.startswith(b"# v2 git bundle\n")
    assert b" HEAD\n" in instance.body
    assert "For the user: http://stacks.example.ts.net:9303/" in out.getvalue()
    assert "From here:    http://host.containers.internal:9303/" in out.getvalue()
    assert "from the phone at http://stacks.example.ts.net:9301/" in out.getvalue()
    assert instance.paths == ["/run/stack.sock"]


def test_up_as_json(tmp_path: Path) -> None:
    path = checkout(tmp_path)
    out = io.StringIO()

    stack.main(["up", "-C", str(path), "--json", "--profile", "seed"], ENV | {"STACK_REPO": "example/other"}, out, Instance([UP]).connect)

    printed = json.loads(out.getvalue())
    assert printed["repo"] == "example/other"
    assert printed["page"] == "http://stacks.example.ts.net:9303/"
    assert printed["local_ports"]["9304"] == "http://host.containers.internal:9304/"


def test_a_shallow_checkout_sends_heads_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = checkout(tmp_path)
    # As in a container that has no git identity: the parentless commit
    # needs one of its own.
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "no-gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for name in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL", "EMAIL"):
        monkeypatch.delenv(name, raising=False)
    identity = ["-c", "user.name=t", "-c", "user.email=t@example.com"]
    subprocess.run(["git", "-C", str(path), *identity, "commit", "-qm", "two", "--allow-empty"], check=True)
    shallow = tmp_path / "shallow"
    subprocess.run(["git", "clone", "--quiet", "--depth", "1", f"file://{path}", str(shallow)], check=True)
    instance = Instance([UP])

    assert stack.main(["up", "-C", str(shallow), "--repo", "example/app"], ENV, io.StringIO(), instance.connect) == 0

    bundle = tmp_path / "sent.bundle"
    bundle.write_bytes(instance.body)
    target = tmp_path / "target"
    subprocess.run(["git", "init", "--quiet", str(target)], check=True)
    subprocess.run(["git", "-C", str(target), "fetch", "--quiet", str(bundle), "refs/stack/head"], check=True)
    assert subprocess.run(["git", "-C", str(target), "show", "FETCH_HEAD:a"], capture_output=True, text=True, check=True).stdout == "a"
    assert not subprocess.run(["git", "-C", str(shallow), "for-each-ref", "refs/stack"], capture_output=True, text=True, check=True).stdout


def test_no_room_is_status_75_with_the_reason(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = checkout(tmp_path)

    status = stack.main(["up", "-C", str(path)], ENV, io.StringIO(), Instance([{"exit": 75, "message": "full"}]).connect)

    assert status == 75
    assert "full" in capsys.readouterr().err


def test_without_a_key_it_says_there_is_no_executor(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = checkout(tmp_path)

    assert stack.main(["up", "-C", str(path)], {}, io.StringIO(), Instance([UP]).connect) == 1
    assert "STACK_KEY is not set" in capsys.readouterr().err


def test_a_repository_origin_does_not_name_needs_one(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = checkout(tmp_path, origin="/srv/app.git")

    assert stack.main(["up", "-C", str(path)], ENV, io.StringIO(), Instance([UP]).connect) == 1
    assert "set STACK_REPO or pass --repo" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("url", "repo"),
    [
        ("https://github.com/example/app.git", "example/app"),
        ("https://github.com/example/app", "example/app"),
        ("ssh://git@github.com/example/my.app.git", "example/my.app"),
    ],
)
def test_the_repository_is_read_from_origin(tmp_path: Path, url: str, repo: str) -> None:
    assert stack.repo_of(str(checkout(tmp_path, origin=url)), {}) == repo


def test_uncommitted_changes_are_said_to_be_left_out(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = checkout(tmp_path)
    (path / "a").write_text("changed")

    stack.main(["up", "-C", str(path)], ENV, io.StringIO(), Instance([UP]).connect)

    assert "Uncommitted changes are not in the stack" in capsys.readouterr().err


def test_run_streams_the_suites_output(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    instance = Instance([{"out": "3 passed"}, {"exit": 0}])

    assert stack.main(["run", "e2e"], ENV, io.StringIO(), instance.connect) == 0
    assert instance.header == {"key": "k", "action": "run", "suite": "e2e"}
    assert "3 passed" in capsys.readouterr().err


def test_a_failed_suite_is_its_status(tmp_path: Path) -> None:
    assert stack.main(["run", "e2e"], ENV, io.StringIO(), Instance([{"exit": 2}]).connect) == 2


def test_logs_down_and_list(tmp_path: Path) -> None:
    logs = Instance([{"exit": 0}])
    stack.main(["logs", "web"], ENV, io.StringIO(), logs.connect)
    down = Instance([{"exit": 0}])
    stack.main(["down"], ENV, io.StringIO(), down.connect)
    listed = Instance([{"exit": 0, "caller": "me", "stacks": [{"caller": "me", "state": "running", "repo": "example/app", "url": "u"}]}])
    out = io.StringIO()
    stack.main(["list"], ENV, out, listed.connect)

    assert logs.header == {"key": "k", "action": "logs", "service": "web"}
    assert down.header == {"key": "k", "action": "down"}
    assert "(yours)" in out.getvalue()


def test_an_instance_that_ends_without_an_answer_is_a_failure() -> None:
    assert stack.main(["down"], ENV, io.StringIO(), Instance([{"out": "x"}]).connect) == 1


def test_an_unreachable_instance_is_said(capsys: pytest.CaptureFixture[str]) -> None:
    assert stack.main(["down"], ENV | {"STACK_SOCKET": "/nonexistent/stack.sock"}, io.StringIO()) == 1
    assert "cannot reach the executor at /nonexistent/stack.sock" in capsys.readouterr().err
