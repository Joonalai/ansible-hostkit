import io
import json
import os
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import executor_agent
import pytest

PORTS = [9302, 9303, 9304]


def make_config(tmp_path: Path, disk_max_gb: float = 10) -> executor_agent.Config:
    return executor_agent.Config(
        stacks_dir=str(tmp_path / "stacks"),
        storage_dir=str(tmp_path / "storage"),
        ports_start=9300,
        ports_end=9399,
        disk_max_bytes=int(disk_max_gb * 1024**3),
    )


class Fake:
    """Stands in for podman and docker-compose, and runs git for real."""

    def __init__(self, compose_config: dict[str, Any] | None = None, up_status: int = 0, disk: list[int] | None = None) -> None:
        self.calls: list[list[str]] = []
        self.compose_config = compose_config or {"name": "stack-r-1", "services": {"web": {"ports": [{"published": "9302"}]}, "e2e": {}}}
        self.up_status = up_status
        self.disk = disk or [0]
        self.containers: list[dict[str, Any]] = []

    def run(self, command: list[str]) -> str:
        self.calls.append(command)
        if command[0] == "git":
            return subprocess.run(command, capture_output=True, text=True, check=True).stdout
        if command[:2] == ["podman", "unshare"] and command[2] == "du":
            used = self.disk.pop(0) if len(self.disk) > 1 else self.disk[0]
            return f"{used}\t/x\n"
        if command[:2] == ["podman", "unshare"]:
            subprocess.run(command[2:], check=True)
            return ""
        if command[:2] == ["podman", "ps"]:
            return json.dumps(self.containers)
        if "docker-compose" in command and "config" in command:
            return json.dumps(self.compose_config)
        return ""

    def stream(self, command: list[str]) -> Iterator[str | int]:
        self.calls.append(command)
        yield "building\n"
        yield self.up_status if "up" in command else 0


def events(stream: io.BytesIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in stream.getvalue().splitlines()]


def repository(tmp_path: Path, files: dict[str, str]) -> Path:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    subprocess.run(["git", "init", "--quiet", str(checkout)], check=True)
    for name, text in files.items():
        (checkout / name).parent.mkdir(parents=True, exist_ok=True)
        (checkout / name).write_text(text)
    subprocess.run(["git", "-C", str(checkout), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(checkout), "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "--quiet", "-m", "one"],
        check=True,
    )
    return checkout


def bundle_of(checkout: Path) -> bytes:
    path = checkout.parent / "head.bundle"
    subprocess.run(["git", "-C", str(checkout), "bundle", "create", "--quiet", str(path), "HEAD"], check=True)
    return path.read_bytes()


PROFILE = """
[profiles.default]
compose = ["compose.yaml"]
page = 2
env = { WEB_PORT = "{port1}", API_PORT = "{port2}", BIND = "{bind}", URL = "http://{host}:{port2}/" }

[suites.e2e]
service = "e2e"
command = ["pytest", "-q"]
"""


def up_request(bundle: bytes, **changes: object) -> bytes:
    request: dict[str, object] = {"action": "up", "caller": "r-1", "repo": "example/app", "ports": PORTS, "host": "stacks.example.ts.net"}
    request |= {"bundle_size": len(bundle), **changes}
    return json.dumps(request).encode() + b"\n" + bundle


def serve(config: executor_agent.Config, fake: Fake, request: bytes) -> list[dict[str, Any]]:
    out = io.BytesIO()
    system = executor_agent.System(run=fake.run, stream=fake.stream, now=lambda: 1000.0)
    executor_agent.handle(config, io.BytesIO(request), executor_agent.Out(out), system)
    return events(out)


def test_up_checks_out_the_bundle_writes_the_env_and_starts_compose(tmp_path: Path) -> None:
    checkout = repository(tmp_path, {"stack.toml": PROFILE, "compose.yaml": "services: {}\n"})
    config, fake = make_config(tmp_path), Fake()

    answer = serve(config, fake, up_request(bundle_of(checkout)))

    head = subprocess.run(["git", "-C", str(checkout), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
    assert answer[-1] == {"exit": 0, "commit": head, "profile": "default", "page": 2}
    stack = tmp_path / "stacks" / "r-1"
    assert (stack / "tree" / "compose.yaml").is_file()
    assert (stack / "tree" / ".env").read_text() == (stack / "stack.env").read_text()
    assert not (stack / "incoming.bundle").exists()
    env = (stack / "stack.env").read_text()
    assert "STACK_PORT_1='9302'" in env
    assert "WEB_PORT='9302'" in env
    assert "BIND='0.0.0.0'" in env
    assert "URL='http://stacks.example.ts.net:9303/'" in env
    check = next(call for call in fake.calls if "config" in call)
    assert check[:4] == ["unshare", "--user", "--map-root-user", "--mount"]
    assert ["-f", str(stack / "incoming" / "compose.yaml"), "config"] == check[check.index("-f") : check.index("-f") + 3]
    assert "--profile" not in check
    assert not (stack / "incoming").exists()
    up = fake.calls[-1]
    assert up[:3] == ["docker-compose", "-p", "stack-r-1"]
    assert up[-4:] == ["--detach", "--build", "--force-recreate", "--remove-orphans"]
    at = up.index("--env-file")
    assert ["--env-file", str(stack / "stack.env"), "-f", str(stack / "checked.json")] == up[at : at + 4]
    assert json.loads((stack / "checked.json").read_text())["services"]["web"] == {"ports": [{"published": "9302"}]}
    assert json.loads((stack / "stack.json").read_text())["suites"] == {"e2e": {"service": "e2e", "command": ["pytest", "-q"]}}


def test_up_replaces_the_last_stack_keeping_its_volumes(tmp_path: Path) -> None:
    checkout = repository(tmp_path, {"stack.toml": PROFILE, "compose.yaml": "services: {}\n"})
    config, fake = make_config(tmp_path), Fake()
    fake.containers = [{"Id": "old1", "State": "running"}]

    serve(config, fake, up_request(bundle_of(checkout)))

    assert ["podman", "rm", "--force", "--depend", "old1"] in fake.calls
    assert not any(call[:2] == ["podman", "volume"] for call in fake.calls)


def test_the_bundle_runs_no_hook_and_is_checked(tmp_path: Path) -> None:
    checkout = repository(tmp_path, {"stack.toml": PROFILE, "compose.yaml": "services: {}\n"})
    config, fake = make_config(tmp_path), Fake()

    serve(config, fake, up_request(bundle_of(checkout)))

    fetch = next(call for call in fake.calls if "fetch" in call)
    assert "core.hooksPath=/dev/null" in fetch
    assert "fetch.fsckObjects=true" in fetch


def test_a_bundle_that_is_not_one_is_refused(tmp_path: Path) -> None:
    answer = serve(make_config(tmp_path), Fake(), up_request(b"not a bundle"))

    assert answer[-1]["exit"] == 1
    assert not (tmp_path / "stacks" / "r-1" / "incoming.bundle").exists()


def test_a_short_bundle_is_refused(tmp_path: Path) -> None:
    request = up_request(b"x" * 10)[:-5]

    answer = serve(make_config(tmp_path), Fake(), request)

    assert answer[-1] == {"exit": 1, "message": "the bundle ended early"}


def test_a_stack_that_reaches_past_itself_is_refused_before_it_starts(tmp_path: Path) -> None:
    checkout = repository(tmp_path, {"stack.toml": PROFILE, "compose.yaml": "services: {}\n"})
    fake = Fake({"name": "stack-r-1", "services": {"web": {"privileged": True}}})

    answer = serve(make_config(tmp_path), fake, up_request(bundle_of(checkout)))

    assert answer[-1]["exit"] == 1
    assert "web: privileged" in answer[-1]["message"]
    assert not any("up" in call for call in fake.calls if call[0] == "docker-compose")


def test_a_failed_up_is_stopped(tmp_path: Path) -> None:
    checkout = repository(tmp_path, {"stack.toml": PROFILE, "compose.yaml": "services: {}\n"})
    fake = Fake(up_status=1)

    answer = serve(make_config(tmp_path), fake, up_request(bundle_of(checkout)))

    assert answer[-1]["exit"] == 1
    assert {"out": "building"} in answer


@pytest.fixture
def agent_umask() -> Iterator[None]:
    previous = os.umask(0o077)
    yield
    os.umask(previous)


@pytest.mark.usefixtures("agent_umask")
def test_the_tree_has_gits_usual_modes_whatever_the_agents_umask(tmp_path: Path) -> None:
    files = {"stack.toml": PROFILE, "compose.yaml": "services: {}\n", "corpus/pin.json": "{}\n", "run.sh": "true\n"}
    checkout = repository(tmp_path, files)
    (checkout / "run.sh").chmod(0o755)
    subprocess.run(["git", "-C", str(checkout), "add", "run.sh"], check=True)
    subprocess.run(
        ["git", "-C", str(checkout), "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "--quiet", "-m", "two"],
        check=True,
    )

    answer = serve(make_config(tmp_path), Fake(), up_request(bundle_of(checkout)))

    assert answer[-1]["exit"] == 0
    stack = tmp_path / "stacks" / "r-1"
    tree = stack / "tree"
    assert (tree / "corpus" / "pin.json").stat().st_mode & 0o777 == 0o644
    assert (tree / "run.sh").stat().st_mode & 0o777 == 0o755
    assert (tree / "corpus").stat().st_mode & 0o777 == 0o755
    assert tree.stat().st_mode & 0o777 == 0o755
    assert stack.stat().st_mode & 0o777 == 0o700
    assert (tree / ".env").stat().st_mode & 0o777 == 0o644
    assert (stack / "stack.env").stat().st_mode & 0o777 == 0o600
    assert os.umask(0o077) == 0o077


def test_past_the_disk_cap_unused_images_go_and_then_it_refuses(tmp_path: Path) -> None:
    (tmp_path / "storage").mkdir()
    checkout = repository(tmp_path, {"stack.toml": PROFILE, "compose.yaml": "services: {}\n"})
    fake = Fake(disk=[3 * 1024**3, 3 * 1024**3])

    answer = serve(make_config(tmp_path, disk_max_gb=2), fake, up_request(bundle_of(checkout)))

    assert ["podman", "image", "prune", "--all", "--force"] in fake.calls
    assert answer[-1]["exit"] == 1
    assert "past its cap" in answer[-1]["message"]


def test_past_the_disk_cap_a_prune_that_frees_enough_lets_it_up(tmp_path: Path) -> None:
    (tmp_path / "storage").mkdir()
    checkout = repository(tmp_path, {"stack.toml": PROFILE, "compose.yaml": "services: {}\n"})
    fake = Fake(disk=[3 * 1024**3, 1024**3])

    answer = serve(make_config(tmp_path, disk_max_gb=2), fake, up_request(bundle_of(checkout)))

    assert answer[-1]["exit"] == 0


@pytest.mark.parametrize(
    "request_",
    [
        {"action": "rm"},
        {"action": "down", "caller": "../x"},
        {"action": "up", "caller": "r-1", "repo": "example/app", "ports": [22], "bundle_size": 1},
        {"action": "up", "caller": "r-1", "repo": "example/app", "ports": [], "bundle_size": 1},
        {"action": "up", "caller": "r-1", "repo": "not a repo", "ports": PORTS, "bundle_size": 1},
        {"action": "up", "caller": "r-1", "repo": "example/app", "ports": PORTS},
    ],
)
def test_a_request_out_of_shape_is_refused(tmp_path: Path, request_: dict[str, Any]) -> None:
    answer = serve(make_config(tmp_path), Fake(), json.dumps(request_).encode() + b"\n")

    assert answer[-1]["exit"] == 1


def test_run_runs_the_suite_in_its_service(tmp_path: Path) -> None:
    checkout = repository(tmp_path, {"stack.toml": PROFILE, "compose.yaml": "services: {}\n"})
    config, fake = make_config(tmp_path), Fake()
    serve(config, fake, up_request(bundle_of(checkout)))

    answer = serve(config, fake, b'{"action": "run", "caller": "r-1", "suite": "e2e"}\n')

    assert answer[-1] == {"exit": 0}
    assert fake.calls[-1][-6:] == ["run", "--rm", "--no-TTY", "e2e", "pytest", "-q"]


def test_run_refuses_a_suite_the_repository_does_not_name(tmp_path: Path) -> None:
    checkout = repository(tmp_path, {"stack.toml": PROFILE, "compose.yaml": "services: {}\n"})
    config, fake = make_config(tmp_path), Fake()
    serve(config, fake, up_request(bundle_of(checkout)))

    answer = serve(config, fake, b'{"action": "run", "caller": "r-1", "suite": "rm"}\n')

    assert answer[-1] == {"exit": 1, "message": "no suite 'rm'; the repository has e2e"}


def test_logs_and_run_want_a_stack(tmp_path: Path) -> None:
    answer = serve(make_config(tmp_path), Fake(), b'{"action": "logs", "caller": "r-1"}\n')

    assert answer[-1] == {"exit": 1, "message": "the caller has no stack: up first"}


def test_down_removes_the_volumes_and_the_tree(tmp_path: Path) -> None:
    checkout = repository(tmp_path, {"stack.toml": PROFILE, "compose.yaml": "services: {}\n"})
    config, fake = make_config(tmp_path), Fake()
    serve(config, fake, up_request(bundle_of(checkout)))

    answer = serve(config, fake, b'{"action": "down", "caller": "r-1"}\n')

    assert answer[-1] == {"exit": 0}
    assert ["podman", "volume", "ls", "--quiet", "--filter", "label=com.docker.compose.project=stack-r-1"] in fake.calls
    assert not (tmp_path / "stacks" / "r-1").exists()


def test_ps_lists_the_stacks_by_caller(tmp_path: Path) -> None:
    fake = Fake()
    fake.containers = [
        {"Id": "a" * 64, "State": "running", "Created": 5, "Labels": {"com.docker.compose.project": "stack-r-1"}},
        {"Id": "b" * 64, "State": "exited", "Created": 9, "Labels": {"com.docker.compose.project": "stack-r-1"}},
        {"Id": "c" * 64, "State": "exited", "Created": 1, "Labels": {"com.docker.compose.project": "stack-r-2"}},
        {"Id": "d" * 64, "State": "running", "Created": 1, "Labels": {"com.docker.compose.project": "something"}},
    ]

    answer = serve(make_config(tmp_path), fake, b'{"action": "ps"}\n')

    stacks = answer[-1]["stacks"]
    assert stacks["r-1"] == {"state": "running", "created": 9, "containers": 2, "memory": 0}
    assert stacks["r-2"]["state"] == "stopped"
    assert set(stacks) == {"r-1", "r-2"}


# ---------------------------------------------------------------------
# The profile
# ---------------------------------------------------------------------


def tree_with(tmp_path: Path, files: dict[str, str]) -> str:
    for name, text in files.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(text)
    return str(tmp_path)


def test_a_repository_without_a_profile_is_refused(tmp_path: Path) -> None:
    with pytest.raises(executor_agent.RefusedError, match=r"no stack\.toml"):
        executor_agent.resolve_profile(tree_with(tmp_path, {}), {"ports": PORTS})


def test_the_instance_override_stands_in_for_a_missing_profile(tmp_path: Path) -> None:
    tree = tree_with(tmp_path, {"compose.yaml": ""})
    override = {"profiles": {"default": {"compose": ["compose.yaml"], "env": {"SEED": "1"}}}}

    profile = executor_agent.resolve_profile(tree, {"ports": PORTS, "override": override})

    assert profile["env"] == {"SEED": "1"}
    assert profile["files"] == ["compose.yaml"]


def test_the_override_replaces_a_profile_of_the_same_name(tmp_path: Path) -> None:
    tree = tree_with(tmp_path, {"stack.toml": PROFILE, "compose.yaml": "", "seed.yaml": ""})
    override = {"profiles": {"seed": {"compose": ["compose.yaml", "seed.yaml"]}}}

    profile = executor_agent.resolve_profile(tree, {"ports": PORTS, "profile": "seed", "override": override})

    assert profile["files"] == ["compose.yaml", "seed.yaml"]
    assert profile["suites"]["e2e"]["service"] == "e2e"


@pytest.mark.parametrize(
    ("toml", "message"),
    [
        ('[profiles.default]\ncompose = ["../outside.yaml"]\n', "not a path in the repository"),
        ('[profiles.default]\ncompose = ["/etc/passwd"]\n', "not a path in the repository"),
        ('[profiles.default]\ncompose = ["link.yaml"]\n', "not a file in the repository"),
        ('[profiles.default]\ncompose = ["missing.yaml"]\n', "not a file in the repository"),
        ('[profiles.default]\nenv = { STACK_PORT_1 = "1" }\n', "not starting STACK_"),
        ('[profiles.default]\nenv = { lower = "1" }\n', "is not one"),
        ("[profiles.default]\nenv = { A = \"it's\" }\n", "a newline or a quote"),
        ('[profiles.default]\nenv = { A = "{port4}" }\n', "the caller has 3 ports"),
        ("[profiles.default]\npage = 4\n", "page is the number"),
        ('[profiles.default]\n[suites."bad name"]\nservice = "e2e"\n', "not a name"),
        ('[profiles.default]\n[suites.e2e]\nservice = "x;y"\n', "service is"),
        ("not toml [", "does not parse"),
    ],
)
def test_a_profile_out_of_bounds_is_refused(tmp_path: Path, toml: str, message: str) -> None:
    tree = tree_with(tmp_path / "tree", {"stack.toml": toml, "compose.yaml": ""})
    (tmp_path / "outside.yaml").write_text("")
    (tmp_path / "tree" / "link.yaml").symlink_to(tmp_path / "outside.yaml")

    with pytest.raises(executor_agent.RefusedError, match=message):
        executor_agent.resolve_profile(tree, {"ports": PORTS})


def test_a_profile_the_repository_lacks_is_named_with_the_ones_it_has(tmp_path: Path) -> None:
    tree = tree_with(tmp_path, {"stack.toml": PROFILE, "compose.yaml": ""})

    with pytest.raises(executor_agent.RefusedError, match="no profile 'seed'; the repository has default"):
        executor_agent.resolve_profile(tree, {"ports": PORTS, "profile": "seed"})


# ---------------------------------------------------------------------
# The compose config
# ---------------------------------------------------------------------


def refusals(tree: Path, service: dict[str, Any], **top: object) -> list[str]:
    return executor_agent.config_refusals({"name": "stack-r-1", "services": {"web": service}, **top}, str(tree), PORTS)


def test_a_plain_stack_passes(tmp_path: Path) -> None:
    service = {
        "volumes": [
            {"type": "bind", "source": str(tmp_path / "data"), "read_only": True},
            {"type": "volume", "source": "db"},
            {"type": "tmpfs", "target": "/tmp"},
        ],
        "ports": [{"published": "9302"}, {"published": "9303-9304"}, {"published": ""}, {"target": 80}],
        "build": {"context": str(tmp_path)},
        "network_mode": "service:db",
    }

    assert refusals(tmp_path, service, volumes={"db": {"name": "stack-r-1_db"}}, networks={"default": None}) == []


@pytest.mark.parametrize(
    ("service", "found"),
    [
        ({"privileged": True}, "web: privileged"),
        ({"cap_add": ["SYS_ADMIN"]}, "web: cap_add"),
        ({"devices": ["/dev/kvm"]}, "web: devices"),
        ({"security_opt": ["seccomp=unconfined"]}, "web: security_opt"),
        ({"network_mode": "host"}, "web: network_mode host"),
        ({"network_mode": "container:stack-r-2-web-1"}, "web: network_mode container:stack-r-2-web-1"),
        ({"pid": "host"}, "web: pid host"),
        ({"userns_mode": "host"}, "web: userns_mode"),
        ({"network_mode": "ns:/proc/1/ns/net"}, "web: network_mode ns:/proc/1/ns/net"),
        ({"network_mode": "slirp4netns:allow_host_loopback=true"}, "web: network_mode slirp4netns"),
        ({"network_mode": "pasta:--map-gw"}, "web: network_mode pasta"),
        ({"ipc": "container:x"}, "web: ipc container:x"),
        ({"gpus": "all"}, "web: gpus"),
        ({"runtime": "runc"}, "web: runtime"),
        ({"cgroup_parent": "/"}, "web: cgroup_parent"),
        ({"labels": {"com.docker.compose.project": "stack-r-2"}}, "web: the label com.docker.compose.project"),
        ({"deploy": {"resources": {"reservations": {"devices": [{"capabilities": ["gpu"]}]}}}}, "web: devices reserved"),
        ({"build": {"context": ".", "network": "host"}}, "web: a build on the network host"),
        ({"build": {"context": ".", "privileged": True}}, "web: build privileged"),
        ({"build": {"context": "/x/tree", "dockerfile": "/etc/Dockerfile"}}, "web: a Dockerfile outside the repository"),
        ({"volumes": [{"type": "bind", "source": "/run/user/1/podman/podman.sock"}]}, "a bind mount from /run/user/1/podman"),
        ({"volumes": [{"type": "npipe", "source": "x"}]}, "a volume of type npipe"),
        ({"ports": [{"published": "9305"}]}, "host port 9305, not one of the caller's 9302–9304"),
        ({"ports": [{"published": "9303-9310"}]}, "host port 9305, 9306"),
        ({"build": {"context": "/"}}, "a build context outside the repository, /"),
        ({"build": {"context": ".", "additional_contexts": {"x": "/home"}}}, "a build context outside the repository, /home"),
    ],
)
def test_a_service_that_reaches_past_the_stack_is_refused(tmp_path: Path, service: dict[str, Any], found: str) -> None:
    assert any(found in line for line in refusals(tmp_path, service))


@pytest.mark.parametrize(
    ("top", "found"),
    [
        ({"volumes": {"data": {"external": True}}}, "volumes data: external"),
        ({"volumes": {"data": {"name": "stack-r-2_data"}}}, "volumes data: named stack-r-2_data"),
        ({"volumes": {"data": {"name": "stack-r-1_data", "driver_opts": {"o": "bind"}}}}, "volumes data: driver_opts"),
        ({"networks": {"default": {"name": "podman"}}}, "networks default: named podman"),
        ({"secrets": {"key": {"file": "/etc/shadow"}}}, "secrets key: a file outside the repository"),
    ],
)
def test_a_volume_network_or_file_not_the_stacks_own_is_refused(tmp_path: Path, top: dict[str, Any], found: str) -> None:
    assert any(found in line for line in refusals(tmp_path, {}, **top))


def test_a_writable_bind_mount_of_the_tree_is_refused(tmp_path: Path) -> None:
    found = refusals(tmp_path, {"volumes": [{"type": "bind", "source": str(tmp_path / "src")}]})

    assert found == [f"web: a bind mount from {tmp_path / 'src'} not read-only; add :ro, or use a volume"]


def test_a_bind_mount_through_a_link_out_of_the_tree_is_refused(tmp_path: Path) -> None:
    (tmp_path / "tree").mkdir()
    (tmp_path / "tree" / "out").symlink_to("/etc")

    assert refusals(tmp_path / "tree", {"volumes": [{"type": "bind", "source": str(tmp_path / "tree" / "out")}]})


def test_the_checked_config_drops_what_the_host_cannot_run_and_fixed_names() -> None:
    config = {
        "name": "stack-r-1",
        "services": {
            "db": {"container_name": "db", "healthcheck": {"start_interval": "1s", "interval": "5s"}},
            "web": {"healthcheck": {"interval": "5s"}},
        },
    }

    assert executor_agent.with_resets(config) == {
        "name": "stack-r-1",
        "services": {"db": {"healthcheck": {"interval": "5s"}}, "web": {"healthcheck": {"interval": "5s"}}},
    }


def test_a_refused_up_keeps_the_last_stack_as_it_was(tmp_path: Path) -> None:
    checkout = repository(tmp_path, {"stack.toml": PROFILE, "compose.yaml": "services: {}\n"})
    config, fake = make_config(tmp_path), Fake()
    serve(config, fake, up_request(bundle_of(checkout)))
    stack = tmp_path / "stacks" / "r-1"
    before = {name: (stack / name).read_text() for name in ("checked.json", "stack.json", "stack.env")}
    fake.compose_config = {"name": "stack-r-1", "services": {"web": {"privileged": True}, "e2e": {}}}
    fake.calls.clear()

    answer = serve(config, fake, up_request(bundle_of(checkout), ports=[9305, 9306, 9307]))

    assert answer[-1]["exit"] == 1
    assert answer[-1]["kept"] is True
    assert "web: privileged" in answer[-1]["message"]
    assert {name: (stack / name).read_text() for name in before} == before
    assert (stack / "tree" / "compose.yaml").is_file()
    assert not (stack / "incoming").exists()
    assert not (stack / "incoming.stack.env").exists()
    assert not any(call[:2] == ["podman", "rm"] or "up" in call for call in fake.calls)


def test_a_bundle_that_does_not_check_out_keeps_the_last_stack(tmp_path: Path) -> None:
    checkout = repository(tmp_path, {"stack.toml": PROFILE, "compose.yaml": "services: {}\n"})
    config, fake = make_config(tmp_path), Fake()
    serve(config, fake, up_request(bundle_of(checkout)))

    answer = serve(config, fake, up_request(b"not a bundle"))

    assert answer[-1]["kept"] is True
    assert (tmp_path / "stacks" / "r-1" / "tree" / "compose.yaml").is_file()


def test_the_checked_config_names_the_stacks_tree_not_the_staged_one(tmp_path: Path) -> None:
    checkout = repository(tmp_path, {"stack.toml": PROFILE, "compose.yaml": "services: {}\n"})
    stack = tmp_path / "stacks" / "r-1"
    source = {"type": "bind", "source": str(stack / "incoming" / "site"), "read_only": True}
    fake = Fake({"name": "stack-r-1", "services": {"web": {"volumes": [source], "build": {"context": str(stack / "incoming")}}, "e2e": {}}})

    serve(make_config(tmp_path), fake, up_request(bundle_of(checkout)))

    web = json.loads((stack / "checked.json").read_text())["services"]["web"]
    assert web["volumes"][0]["source"] == str(stack / "tree" / "site")
    assert web["build"]["context"] == str(stack / "tree")


def test_a_suite_whose_service_the_stack_lacks_is_refused(tmp_path: Path) -> None:
    checkout = repository(tmp_path, {"stack.toml": PROFILE, "compose.yaml": "services: {}\n"})
    fake = Fake({"name": "stack-r-1", "services": {"web": {}}})

    answer = serve(make_config(tmp_path), fake, up_request(bundle_of(checkout)))

    assert answer[-1]["exit"] == 1
    assert "a suite's service e2e is not one of the stack's" in answer[-1]["message"]


def test_later_actions_run_the_checked_config_not_the_tree(tmp_path: Path) -> None:
    checkout = repository(tmp_path, {"stack.toml": PROFILE, "compose.yaml": "services: {}\n"})
    config, fake = make_config(tmp_path), Fake()
    serve(config, fake, up_request(bundle_of(checkout)))
    checked = str(tmp_path / "stacks" / "r-1" / "checked.json")

    for request in (b'{"action": "run", "caller": "r-1", "suite": "e2e"}\n', b'{"action": "start", "caller": "r-1"}\n'):
        serve(config, fake, request)
        assert ["-f", checked] == fake.calls[-1][fake.calls[-1].index("-f") : fake.calls[-1].index("-f") + 2]
        assert not any(word.endswith("compose.yaml") for word in fake.calls[-1])


def test_a_stack_without_its_checked_config_is_brought_up_again(tmp_path: Path) -> None:
    checkout = repository(tmp_path, {"stack.toml": PROFILE, "compose.yaml": "services: {}\n"})
    config, fake = make_config(tmp_path), Fake()
    serve(config, fake, up_request(bundle_of(checkout)))
    (tmp_path / "stacks" / "r-1" / "checked.json").unlink()

    answer = serve(config, fake, b'{"action": "start", "caller": "r-1"}\n')

    assert answer[-1] == {"exit": 1, "message": "the stack has no checked config: up again"}


@pytest.mark.parametrize("name", ["COMPOSE_FILE", "COMPOSE_PROFILES", "DOCKER_HOST", "STACK_PORT_1"])
def test_env_names_compose_or_the_agent_read_are_refused(name: str) -> None:
    with pytest.raises(executor_agent.RefusedError, match="not starting STACK_, COMPOSE_ or DOCKER_"):
        executor_agent.check_env({name: "x"}, PORTS, "h")


def test_ps_skips_a_project_label_that_is_no_caller(tmp_path: Path) -> None:
    fake = Fake()
    fake.containers = [{"Id": "a" * 64, "State": "running", "Labels": {"com.docker.compose.project": "stack-../x"}}]

    answer = serve(make_config(tmp_path), fake, b'{"action": "ps"}\n')

    assert answer[-1]["stacks"] == {}


@pytest.mark.parametrize(("text", "size"), [("12.5MB", 12_500_000), ("1GiB", 1024**3), ("0B", 0), ("", 0), ("bad", 0)])
def test_sizes_are_read_as_podman_prints_them(text: str, size: int) -> None:
    assert executor_agent.size_of(text) == size


PROFILED = """
[profiles.default]
compose = ["compose.yaml"]
compose_profiles = ["seed", "docs"]
"""


def test_only_the_compose_profiles_turned_on_are_checked_and_started(tmp_path: Path) -> None:
    checkout = repository(tmp_path, {"stack.toml": PROFILED, "compose.yaml": "services: {}\n"})
    config, fake = make_config(tmp_path), Fake({"name": "stack-r-1", "services": {"seed": {"profiles": ["seed"]}}})

    answer = serve(config, fake, up_request(bundle_of(checkout)))

    assert answer[-1]["exit"] == 0
    check = next(call for call in fake.calls if "config" in call)
    assert check[check.index("config") - 4 : check.index("config")] == ["--profile", "seed", "--profile", "docs"]
    assert "*" not in check
    up = fake.calls[-1]
    assert up[up.index("--profile") : up.index("--profile") + 4] == ["--profile", "seed", "--profile", "docs"]
    serve(config, fake, b'{"action": "start", "caller": "r-1"}\n')
    assert fake.calls[-1][fake.calls[-1].index("--profile") : fake.calls[-1].index("--profile") + 2] == ["--profile", "seed"]


def test_a_compose_profile_turned_on_is_checked_like_any_service(tmp_path: Path) -> None:
    checkout = repository(tmp_path, {"stack.toml": PROFILED, "compose.yaml": "services: {}\n"})
    docs = {"profiles": ["docs"], "volumes": [{"type": "bind", "source": str(tmp_path / "stacks" / "r-1" / "incoming" / "docs")}]}
    fake = Fake({"name": "stack-r-1", "services": {"docs": docs}})

    answer = serve(make_config(tmp_path), fake, up_request(bundle_of(checkout)))

    assert answer[-1]["exit"] == 1
    assert "docs: a bind mount" in answer[-1]["message"]
    assert "not read-only" in answer[-1]["message"]


@pytest.mark.parametrize("names", ["seed", [1], ["../x"], ["a b"], [f"p{n}" for n in range(17)]])
def test_compose_profiles_out_of_shape_are_refused(names: object) -> None:
    with pytest.raises(executor_agent.RefusedError, match="compose"):
        executor_agent.check_compose_profiles(names)


@pytest.mark.parametrize("opts", [["no-new-privileges:true"], ["no-new-privileges"], ["no-new-privileges", "no-new-privileges:true"]])
def test_a_security_opt_that_only_tightens_passes(tmp_path: Path, opts: list[str]) -> None:
    assert refusals(tmp_path, {"security_opt": opts}) == []


@pytest.mark.parametrize(
    "opts",
    [
        ["no-new-privileges:false"],
        ["seccomp=unconfined"],
        ["apparmor=unconfined"],
        ["label=disable"],
        ["systempaths=unconfined"],
        ["no-new-privileges:true", "seccomp=unconfined"],
    ],
)
def test_any_other_security_opt_is_refused(tmp_path: Path, opts: list[str]) -> None:
    assert any("security_opt" in line for line in refusals(tmp_path, {"security_opt": opts}))


def test_the_tree_env_replaces_the_repositorys_own(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("SECRET=from-the-repository\n")

    executor_agent.write_tree_env(str(tmp_path), "WEB_PORT='9302'\n")

    assert (tmp_path / ".env").read_text() == "WEB_PORT='9302'\n"


def test_a_tree_env_link_is_replaced_not_written_through(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.write_text("untouched\n")
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / ".env").symlink_to(outside)

    executor_agent.write_tree_env(str(tree), "WEB_PORT='9302'\n")

    assert outside.read_text() == "untouched\n"
    assert not (tree / ".env").is_symlink()
    assert (tree / ".env").read_text() == "WEB_PORT='9302'\n"


def test_a_tree_env_directory_is_refused(tmp_path: Path) -> None:
    (tmp_path / ".env").mkdir()

    with pytest.raises(executor_agent.RefusedError, match="directory"):
        executor_agent.write_tree_env(str(tmp_path), "")
