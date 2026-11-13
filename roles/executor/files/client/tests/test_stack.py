import hashlib
import io
import json
import shutil
import socket
import ssl
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

    def connect(self, path: str, env: dict[str, str]) -> socket.socket:
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
    "volumes": "new",
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
    assert "Its volumes started empty" in out.getvalue()
    assert instance.paths == ["/run/stack.sock"]


def test_up_as_json(tmp_path: Path) -> None:
    path = checkout(tmp_path)
    out = io.StringIO()

    stack.main(["up", "-C", str(path), "--json", "--profile", "seed"], ENV | {"STACK_REPO": "example/other"}, out, Instance([UP]).connect)

    printed = json.loads(out.getvalue())
    assert printed["repo"] == "example/other"
    assert printed["volumes"] == "new"
    assert printed["page"] == "http://stacks.example.ts.net:9303/"
    assert printed["local_ports"]["9304"] == "http://host.containers.internal:9304/"


def test_behind_the_hosts_https_the_user_gets_https_and_here_stays_http(tmp_path: Path) -> None:
    path = checkout(tmp_path)
    out = io.StringIO()

    stack.main(["up", "-C", str(path), "--json"], ENV, out, Instance([UP | {"page": 1, "https_port": 3}]).connect)

    printed = json.loads(out.getvalue())
    assert printed["page"] == "https://stacks.example.ts.net:9304/"
    assert printed["local"] == "http://host.containers.internal:9302/"


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


def test_a_shallow_checkout_sends_one_commit_for_one_head(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = checkout(tmp_path)
    shallow = tmp_path / "shallow"
    subprocess.run(["git", "clone", "--quiet", "--depth", "1", f"file://{path}", str(shallow)], check=True)
    heads = []
    for number in (1, 2):
        monkeypatch.setenv("GIT_COMMITTER_DATE", f"170000000{number} +0000")
        monkeypatch.setenv("GIT_AUTHOR_DATE", f"170000000{number} +0000")
        stack.make_bundle(str(shallow), str(tmp_path / f"{number}.bundle"))
        heads.append((tmp_path / f"{number}.bundle").read_bytes().split(b"\n")[1])

    assert heads[0] == heads[1]


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


@pytest.mark.parametrize(
    ("argv", "result", "printed"),
    [
        (["down"], {"exit": 75, "message": "no room", "request": "5-1234-2001"}, "stack: failed (request 5-1234-2001): no room"),
        (["run", "e2e"], {"exit": 2, "request": "5-1234-2001"}, "stack: failed (request 5-1234-2001): exit 2"),
        (["list"], {"exit": 1, "message": "refused", "request": "ab12"}, "stack: failed (request ab12): refused"),
    ],
)
def test_a_failure_is_printed_with_its_request_id(
    argv: list[str],
    result: dict[str, Any],
    printed: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    status = stack.main(argv, ENV, io.StringIO(), Instance([result]).connect)

    assert status == result["exit"]
    assert capsys.readouterr().err.strip() == printed


def test_a_failed_up_is_printed_with_its_request_id(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = checkout(tmp_path)
    out = io.StringIO()

    status = stack.main(["up", "-C", str(path), "--json"], ENV, out, Instance([{"exit": 1, "message": "bad", "request": "r-9"}]).connect)

    assert status == 1
    assert "stack: failed (request r-9): bad" in capsys.readouterr().err
    assert out.getvalue() == ""


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


@pytest.mark.parametrize("action", ["stop", "start"])
def test_stop_and_start_send_one_service(action: str) -> None:
    instance = Instance([{"exit": 0}])

    assert stack.main([action, "db"], ENV, io.StringIO(), instance.connect) == 0
    assert instance.header == {"key": "k", "action": action, "service": "db"}


def test_list_names_the_stopped_services() -> None:
    listed = Instance([{"exit": 0, "caller": "me", "stacks": [{"caller": "me", "state": "running", "stopped_services": ["db", "mail"]}]}])
    out = io.StringIO()

    stack.main(["list"], ENV, out, listed.connect)

    assert "(stopped: db, mail)" in out.getvalue()


def test_an_instance_that_ends_without_an_answer_is_a_failure() -> None:
    assert stack.main(["down"], ENV, io.StringIO(), Instance([{"out": "x"}]).connect) == 1


def test_an_unreachable_instance_is_said(capsys: pytest.CaptureFixture[str]) -> None:
    assert stack.main(["down"], ENV | {"STACK_SOCKET": "/nonexistent/stack.sock"}, io.StringIO()) == 1
    assert "cannot reach the executor at /nonexistent/stack.sock" in capsys.readouterr().err


# ---------------------------------------------------------------------
# A remote instance, over TLS
# ---------------------------------------------------------------------


class TlsInstance:
    """A TLS port on localhost that records what it reads and answers one event."""

    def __init__(self, tmp_path: Path) -> None:
        if shutil.which("openssl") is None:
            pytest.skip("no openssl to make a certificate with")
        cert, key = tmp_path / "tls.crt", tmp_path / "tls.key"
        subprocess.run(
            ["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes", "-days", "1",
             "-subj", "/CN=localhost", "-keyout", str(key), "-out", str(cert)],
            check=True,
            capture_output=True,
        )
        self.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.context.load_cert_chain(cert, key)
        self.sha256 = hashlib.sha256(ssl.PEM_cert_to_DER_cert(cert.read_text())).hexdigest()
        self.listener = socket.create_server(("127.0.0.1", 0))
        self.url = f"tls://127.0.0.1:{self.listener.getsockname()[1]}"
        self.received = b""
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def serve(self) -> None:
        raw, _ = self.listener.accept()
        try:
            with self.context.wrap_socket(raw, server_side=True) as conn, conn.makefile("rb") as incoming:
                self.received = incoming.readline()
                conn.sendall(b'{"exit": 0, "message": "done"}\n')
                conn.unwrap()
        except (ssl.SSLError, OSError):
            return


def test_a_remote_instance_with_the_pinned_certificate_gets_the_request(tmp_path: Path) -> None:
    instance = TlsInstance(tmp_path)
    env = {"STACK_KEY": "k", "STACK_URL": instance.url, "STACK_TLS_SHA256": instance.sha256.upper()}

    assert stack.main(["down"], env, io.StringIO()) == 0

    instance.thread.join(10)
    assert json.loads(instance.received) == {"key": "k", "action": "down"}


def test_a_remote_instance_with_another_certificate_is_sent_nothing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    instance = TlsInstance(tmp_path)
    env = {"STACK_KEY": "k", "STACK_URL": instance.url, "STACK_TLS_SHA256": "0" * 64}

    assert stack.main(["down"], env, io.StringIO()) == 1

    instance.thread.join(10)
    assert instance.received == b""
    assert f"shows the certificate {instance.sha256}, not the pinned {'0' * 64}: nothing was sent" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("env", "says"),
    [
        ({"STACK_URL": "tls://stacks.example.ts.net:9300"}, "STACK_URL wants STACK_TLS_SHA256"),
        ({"STACK_URL": "tls://stacks.example.ts.net:9300", "STACK_TLS_SHA256": "not-a-hash"}, "STACK_URL wants STACK_TLS_SHA256"),
        ({"STACK_URL": "tls://stacks.example.ts.net", "STACK_TLS_SHA256": "0" * 64}, "is not tls://host:port"),
        ({"STACK_URL": "tls://stacks.example.ts.net:9300", "STACK_TLS_CA": "/nonexistent/ca.pem"}, "STACK_TLS_CA /nonexistent/ca.pem"),
    ],
)
def test_a_remote_instance_it_cannot_check_is_not_reached(env: dict[str, str], says: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert stack.main(["down"], {"STACK_KEY": "k"} | env, io.StringIO()) == 1
    assert says in capsys.readouterr().err
