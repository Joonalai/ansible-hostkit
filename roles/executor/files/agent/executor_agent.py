#!/usr/bin/python3 -I
"""Build and run the callers' stacks, as an executor instance's exec account.

The instance's front authenticates a caller, checks its limits and hands
the request here, the bundle unopened. This side holds no key and no
credential: it clones the bundle, builds the stack and starts it, with the
exec account's own rootless podman. systemd starts it for every connection
to the agent's socket, a user socket unit of the exec account in a
directory only the front's account can open:

    executor-agent serve /etc/hostkit-executor/<instance>/agent.json

A request is one line of JSON, and for up the bundle's bytes after it:

    {"action": "up", "caller": "runner-app-1", "repo": "example/app",
     "profile": "default", "ports": [9302, 9303, 9304], "host": "...",
     "override": {...}, "bundle_size": 123456}            then the bundle
     with "https_port": 2 where the host serves the page with HTTPS
    {"action": "run", "caller": ..., "suite": "e2e"}
    {"action": "logs", "caller": ..., "service": "web"}   service optional
    {"action": "down" | "stop" | "start", "caller": ...}
    {"action": "stop" | "start", "caller": ..., "service": "db"}   one service
    {"action": "ps"} | {"action": "prune"}

Each may carry the front's id of the request, "request", which every line
the agent writes to its journal for it starts with. The agent exits 0 once
it has answered, whatever the request's status, which is in the answer.

The answer is lines of JSON: {"out": "<a line of output>"} as it comes,
then one {"exit": <status>, ...} with what the action found.

Each caller has one stack, compose project stack-<caller>, from one tree,
~/stacks/<caller>/tree, the bundle's commit checked out. How it comes up is
the repository's stack.toml, a profile of compose files, the compose
profiles to turn on, .env values and the page's port, which the instance's
config may override for a repository. The agent writes the .env values,
the caller's ports among them, to ~/stacks/<caller>/stack.env, outside the
tree, and the same as the tree's .env, which a service's `env_file: .env`
reads.

Before anything starts it reads the stack's compose config, the services
of the compose profiles turned on and no others, with nothing of the
account's but the stack's own directory in sight, and refuses what reaches
past the stack: privileged containers, added capabilities, security
options but no-new-privileges, devices and GPUs, the host's
namespaces or another container's, a bind mount from outside the tree or
a writable one, a volume or network not the project's own, compose's own
labels, a build that reaches out, and a host port that is not one of the
caller's. It writes the config it checked to ~/stacks/<caller>/checked.json
and runs everything after from that alone, never the tree's files again,
which a container may have rewritten. In it, a health check's
start_interval, which Debian trixie's podman cannot run, and a fixed
container_name, which would keep two callers from running the same
repository, are reset.

Standard library only, on the system python3.
"""

import contextlib
import json
import os
import re
import socket
import subprocess
import sys
import time
import tomllib
from collections.abc import Callable, Generator, Iterator
from dataclasses import dataclass, field
from typing import IO, Any, cast

PROJECT_LABEL = "com.docker.compose.project"
SERVICE_LABEL = "com.docker.compose.service"
# A container of `compose run`, which a suite's run leaves when it is cut
# short: not a service of the stack's.
ONEOFF_LABEL = "com.docker.compose.oneoff"
PROFILE_FILE = "stack.toml"
CALLER = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")
SERVICE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,62}")
NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")
ENV_KEY = re.compile(r"[A-Z_][A-Z0-9_]{0,63}")
REPO = re.compile(r"[A-Za-z0-9-]+/[A-Za-z0-9._-]+")
COMMIT = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
SECRET_NAME = re.compile(r"[a-z][a-z0-9_]{0,31}")
REQUEST_ID = re.compile(r"[A-Za-z0-9._:-]{1,96}")
PLACEHOLDER = re.compile(r"\{(port[0-9]+|bind|host|url|secret:[a-z][a-z0-9_]{0,31})\}")
MAX_HEADER = 65536
MAX_PORTS = 16
MAX_COMPOSE_FILES = 8
MAX_VALUE = 4096
# The shortest secret value the play lets through, so that hiding one in
# the output never mangles ordinary words.
MIN_SECRET = 8
MAX_COMMAND = 64
MAX_COMPOSE_PROFILES = 16
# The only security_opt a service may set: one that tightens.
SECURITY_OPTS = ("no-new-privileges", "no-new-privileges:true")
LOG_TAIL = 200
# The lines of each failed service's log an up that failed answers with.
FAILED_LOG_TAIL = 50
# How long an up or a start watches the services for, at most, well within
# the unit's RuntimeMaxSec: until every one runs, healthy where it has a
# health check, or ended with status 0, and has stayed so SETTLE_SECONDS,
# long enough to see a service that dies as it starts.
WAIT_SECONDS = 600
SETTLE_SECONDS = 5
POLL_SECONDS = 1
# Where the profile's page is published, the up is done only once the page
# has answered too: a service with no health check that runs past
# SETTLE_SECONDS may still die before it serves. Any HTTP status is an
# answer. The page is asked inside its container's own network namespace,
# never through the host's, which the host's sandbox keeps the exec account
# and every stack off.
PROBE_SECONDS = 2
# How long a page that took the connection may take to send its status
# line: a server rendering its first page can be slow.
ANSWER_SECONDS = 15
HTTP_ANSWER = b"HTTP/"
# The probe's status for a page that did not answer; any other failure is
# the probe's own, nsenter's or podman's, and fails the watch after
# PROBE_ERRORS_MAX in a row rather than wait out WAIT_SECONDS.
PROBE_NO_ANSWER = 3
PROBE_ERRORS_MAX = 3
SETTLED, PENDING, FAILED = "settled", "pending", "failed"
# The tree is checked out with git's usual modes, whatever the agent's own
# umask: a Dockerfile's COPY takes them into the image, where a service that
# is not root must still read its files. The tree is in the exec account's
# home, which no other account enters.
TREE_UMASK = 0o022
CHUNK = 65536
ACTIONS = ("up", "run", "logs", "down", "stop", "start", "ps", "prune")
# What a service may not set at all: more than a container gets, the
# host's devices and GPUs, another runtime or cgroup, the host's or
# another container's namespaces.
NEVER = (
    "cap_add", "devices", "device_cgroup_rules", "volumes_from", "gpus", "runtime",
    "cgroup_parent", "cgroup", "userns_mode", "uts", "isolation", "storage_opt", "credential_spec",
)
# The namespaces a service may set, and only to its own, none, or another
# service's of the same stack: anything else joins the host's or another
# stack's (host, container:, ns:/proc/..., slirp4netns: and pasta: options).
NAMESPACES = ("network_mode", "pid", "ipc")
OWN_MODE = re.compile(r"none|bridge|private|shareable|service:[A-Za-z0-9][A-Za-z0-9_.-]{0,62}")
# Labels compose and podman keep for themselves: a service that set them
# could pass for another caller's stack.
OWN_LABELS = ("com.docker.compose.", "io.podman.")
# The only build network a service may ask for.
BUILD_NETWORKS = (None, "default", "none")
# Reads the compose files with nothing of the account's but the stack's own
# directory in sight: the home, and every other caller's tree in it, behind
# an empty tmpfs, in a mount namespace of the command's own. An include,
# an extends or an env_file that names another file then finds nothing.
CONFINE = """set -eu
keep=$(mktemp -d -p /tmp)
mount --bind "$1" "$keep"
mount -t tmpfs -o mode=0700 tmpfs "$2"
if [ -d "$3" ]; then mount -t tmpfs -o mode=0700 tmpfs "$3"; fi
mkdir -p "$1"
mount --bind "$keep" "$1"
shift 3
exec "$@"
"""
SIZE_UNITS = {"b": 1, "kb": 1000, "kib": 1024, "mb": 1000**2, "mib": 1024**2, "gb": 1000**3, "gib": 1024**3}


class RefusedError(Exception):
    """A request the agent will not carry out, with the reason for the caller."""


@dataclass(frozen=True)
class Config:
    """The instance's settings this side reads: where the trees are, the
    ports a caller may be given, and the disk cap.
    """

    stacks_dir: str
    storage_dir: str
    ports_start: int
    ports_end: int
    disk_max_bytes: int


def load_config(path: str) -> Config:
    """The agent's config file, written by the play."""
    with open(path) as config_file:
        raw = json.load(config_file)
    return Config(
        stacks_dir=raw["stacks_dir"],
        storage_dir=raw["storage_dir"],
        ports_start=int(raw["ports_start"]),
        ports_end=int(raw["ports_end"]),
        disk_max_bytes=int(raw["disk_max_gb"] * 1024**3),
    )


class Out:
    """The answer, as lines of JSON on the connection."""

    def __init__(self, stream: IO[bytes]) -> None:
        """Write to stream, flushing every line."""
        self.stream = stream
        # Each hidden value and its placeholder, the longest value first.
        self.hidden: list[tuple[str, str]] = []
        # The request answered, for the journal's line of a failure.
        self.request: object = None

    def hide(self, secrets: dict[str, str]) -> None:
        """Replace each of the secrets' values with its placeholder in
        everything written from now on, the journal's line included.
        """
        hidden = dict(self.hidden) | {value: f"{{secret:{name}}}" for name, value in secrets.items() if value}
        self.hidden = sorted(hidden.items(), key=lambda pair: len(pair[0]), reverse=True)

    def clean(self, data: Any) -> Any:  # noqa: ANN401 -- an event's values are of every JSON kind
        """The data, with every hidden value replaced, the longest first."""
        if isinstance(data, str):
            for value, placeholder in self.hidden:
                data = data.replace(value, placeholder)
            return data
        if isinstance(data, dict):
            return {key: self.clean(item) for key, item in data.items()}
        if isinstance(data, list):
            return [self.clean(item) for item in data]
        return data

    def line(self, text: str) -> None:
        """One line of output, as it comes."""
        self.event({"out": text.rstrip("\n")})

    def event(self, data: dict[str, Any]) -> None:
        """One event, flushed at once so the caller sees progress."""
        self.stream.write(json.dumps(self.clean(data)).encode() + b"\n")
        self.stream.flush()

    def done(self, status: int, **data: object) -> int:
        """The last line, with the action's status; a failure's goes to the
        journal too, with the request's id, cleaned as the answer is.
        """
        self.event({"exit": status, **data})
        if status != 0:
            log_failure(self.request, self.clean(str(data.get("message") or f"exit {status}")))
        return status


@dataclass(frozen=True)
class Page:
    """The profile's page: the service that publishes the caller's port,
    and the port it listens on in its container.
    """

    service: str
    target: int
    port: int


def page_of(config: dict[str, Any], port: int) -> Page | None:
    """The page of a checked config: the service publishing the caller's
    port, if one does, by its target port; None for a stack with no page.
    """
    for name, service in sorted((config.get("services") or {}).items()):
        for entry in service.get("ports") or [] if isinstance(service, dict) else []:
            if not isinstance(entry, dict) or entry.get("protocol", "tcp") != "tcp":
                continue
            try:
                published = published_ports(entry)
            except RefusedError:
                continue
            target, _, last = str(entry.get("target") or "").partition("-")
            if port in published and target.isdigit():
                # A range maps onto a range, port for port; onto one port, all to it.
                return Page(service=name, target=int(target) + (published.index(port) if last else 0), port=port)
    return None


def probe_page(port: int) -> int:
    """Ask the page on port of this network namespace's loopback: 0 when it
    answered HTTP, PROBE_NO_ANSWER when nothing did.
    """
    for host in ("127.0.0.1", "::1"):
        try:
            with socket.create_connection((host, port), timeout=PROBE_SECONDS) as conn:
                conn.settimeout(ANSWER_SECONDS)
                conn.sendall(b"GET / HTTP/1.0\r\nHost: localhost\r\n\r\n")
                answer = b""
                while len(answer) < len(HTTP_ANSWER):
                    chunk = conn.recv(len(HTTP_ANSWER) - len(answer))
                    if not chunk:
                        break
                    answer += chunk
        except OSError:
            continue
        if answer == HTTP_ANSWER:
            return 0
    return PROBE_NO_ANSWER


def service_containers(containers: list[dict[str, Any]]) -> Iterator[tuple[str, dict[str, Any]]]:
    """The containers of the stack's services, by service: compose run's
    leftovers are none, nor is a container with no service's name.
    """
    for container in containers:
        labels = container.get("Labels") or {}
        service = labels.get(SERVICE_LABEL, "")
        if labels.get(ONEOFF_LABEL) != "True" and SERVICE.fullmatch(service):
            yield service, container


@dataclass
class System:
    """What the agent runs, as callables the tests replace."""

    # Runs a command and returns what it printed; a failure raises CalledProcessError.
    run: Callable[[list[str]], str]
    # Runs a command, yielding its lines of output as they come, then returns its status.
    stream: Callable[[list[str]], Iterator[str | int]]
    now: Callable[[], float] = time.time
    # The watch's clock, which a change of the wall clock leaves alone.
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep


def run_command(command: list[str]) -> str:
    """Run a command and return its output; a failure raises."""
    return subprocess.run(command, capture_output=True, text=True, check=True).stdout


def stream_command(command: list[str]) -> Iterator[str | int]:
    """Run a command, yielding its output's lines, and last its status."""
    with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace") as process:
        assert process.stdout is not None  # noqa: S101 -- stdout=PIPE always sets it
        yield from process.stdout
        yield process.wait()


@contextlib.contextmanager
def umask(mask: int) -> Generator[None]:
    """The process's umask set to mask, and the last one back after."""
    previous = os.umask(mask)
    try:
        yield
    finally:
        os.umask(previous)


def project(caller: str) -> str:
    """A caller's compose project."""
    return f"stack-{caller}"


class Stack:
    """One caller's stack: its directory, tree and the files beside it."""

    def __init__(self, config: Config, caller: str, *, staged: bool = False) -> None:
        """The stack of caller, which must already be checked as a name; or,
        staged, the next one beside it, checked there before it replaces
        the stack.
        """
        self.caller = caller
        self.project = project(caller)
        self.dir = os.path.join(config.stacks_dir, caller)
        prefix = "incoming." if staged else ""
        self.tree = os.path.join(self.dir, "incoming" if staged else "tree")
        self.env_file = os.path.join(self.dir, f"{prefix}stack.env")
        # The values the up was given, for hiding them in the stack's output.
        self.secrets_file = os.path.join(self.dir, f"{prefix}secrets.json")
        self.checked = os.path.join(self.dir, f"{prefix}checked.json")
        self.meta_file = os.path.join(self.dir, f"{prefix}stack.json")

    def meta(self) -> dict[str, Any] | None:
        """What the last up recorded, or None for a stack never brought up."""
        try:
            with open(self.meta_file) as meta_file:
                return json.load(meta_file)
        except FileNotFoundError:
            return None

    def secrets(self) -> dict[str, str]:
        """The secrets the stack's up was given, by name; none for a stack
        brought up without, or by an agent before them.
        """
        try:
            with open(self.secrets_file) as secrets_file:
                found = json.load(secrets_file)
        except (FileNotFoundError, ValueError):
            return {}
        return {str(name): str(value) for name, value in found.items()} if isinstance(found, dict) else {}

    def base(self) -> list[str]:
        """docker-compose with the project and its env file, which keeps the tree's .env out."""
        return ["docker-compose", "-p", self.project, "--project-directory", self.tree, "--env-file", self.env_file]

    def source(self, meta: dict[str, Any]) -> list[str]:
        """docker-compose on the repository's own files, only ever to check them."""
        command = self.base()
        for name in meta["files"]:
            command += ["-f", os.path.join(self.tree, name)]
        return command

    def services(self) -> set[str]:
        """The services of the checked config, which every later command runs."""
        try:
            with open(self.checked) as checked:
                return set(json.load(checked).get("services") or {})
        except FileNotFoundError:
            raise RefusedError("the stack has no checked config: up again") from None

    def compose(self) -> list[str]:
        """docker-compose on the config the up checked, never the tree's
        files again: a container may have rewritten them since; with the
        compose profiles the stack profile turned on.
        """
        if not os.path.isfile(self.checked):
            raise RefusedError("the stack has no checked config: up again")
        names = (self.meta() or {}).get("compose_profiles") or []
        return [*self.base(), "-f", self.checked, *profile_flags(names)]


# ---------------------------------------------------------------------
# The profile: how the repository says to bring itself up
# ---------------------------------------------------------------------


def read_profiles(tree: str, override: dict[str, Any]) -> dict[str, Any]:
    """The repository's stack.toml, with the instance's override for it on top."""
    path = os.path.join(tree, PROFILE_FILE)
    found: dict[str, Any] = {}
    if os.path.isfile(path) and not os.path.islink(path):
        try:
            with open(path, "rb") as profile_file:
                found = tomllib.load(profile_file)
        except tomllib.TOMLDecodeError as error:
            raise RefusedError(f"{PROFILE_FILE} does not parse: {error}") from error
    elif not override:
        raise RefusedError(f"the repository has no {PROFILE_FILE}, and the instance no profile for it")
    return {
        "profiles": {**(found.get("profiles") or {}), **(override.get("profiles") or {})},
        "suites": {**(found.get("suites") or {}), **(override.get("suites") or {})},
    }


def inside(tree: str, path: str) -> bool:
    """Whether path, links resolved, is the tree or under it."""
    real_tree = os.path.realpath(tree)
    real = os.path.realpath(path)
    return real == real_tree or real.startswith(real_tree + os.sep)


def check_compose_files(tree: str, files: object) -> list[str]:
    """The profile's compose files, each a file in the tree."""
    if not (isinstance(files, list) and 1 <= len(files) <= MAX_COMPOSE_FILES and all(isinstance(name, str) for name in files)):
        raise RefusedError(f"a profile's compose is a list of 1 to {MAX_COMPOSE_FILES} files")
    checked: list[str] = []
    for name in files:
        if os.path.isabs(name) or ".." in name.split("/"):
            raise RefusedError(f"compose file {name!r} is not a path in the repository")
        path = os.path.join(tree, name)
        if not (inside(tree, path) and os.path.isfile(path)):
            raise RefusedError(f"compose file {name!r} is not a file in the repository")
        checked.append(os.path.normpath(name))
    return checked


@dataclass
class Secrets:
    """The secrets the instance gives the request's repository, and the
    names the profile asked for, granted or not.
    """

    values: dict[str, str] = field(default_factory=dict)
    asked: set[str] = field(default_factory=set)

    def empty(self) -> list[str]:
        """The names asked for and not granted, which were filled in empty."""
        return sorted(self.asked - set(self.values))


def fill(value: str, ports: list[int], host: str, secrets: Secrets | None = None, url: str = "") -> str:
    """A value with {port1}..{portN}, {bind}, {host}, {url} and
    {secret:<name>} put in, once: a value put in is not read again. A
    secret the instance does not grant is put in empty, and noted in
    secrets.
    """

    def one(match: re.Match[str]) -> str:
        word = match.group(1)
        if word.startswith("secret:"):
            name = word.removeprefix("secret:")
            if secrets is None:
                return ""
            secrets.asked.add(name)
            return secrets.values.get(name, "")
        if word == "bind":
            return "0.0.0.0"  # noqa: S104 -- a stack's ports are published on every address, as the host's sandbox decides
        if word == "host":
            return host
        if word == "url":
            return url
        index = int(word.removeprefix("port"))
        if not 1 <= index <= len(ports):
            raise RefusedError(f"{{{word}}}: the caller has {len(ports)} ports")
        return str(ports[index - 1])

    return PLACEHOLDER.sub(one, value)


def check_env(env: object, ports: list[int], host: str, secrets: Secrets | None = None, url: str = "") -> dict[str, str]:
    """The profile's .env values, filled in, as the env file can hold them;
    the secrets they ask for are noted in secrets.
    """
    if env is None:
        return {}
    if not isinstance(env, dict):
        raise RefusedError("a profile's env is a table of names and values")
    checked = {}
    for key, value in env.items():
        if not (isinstance(key, str) and ENV_KEY.fullmatch(key)) or key.startswith(("STACK_", "COMPOSE_", "DOCKER_")):
            raise RefusedError(f"env name {key!r} is not one: upper case, digits and _, not starting STACK_, COMPOSE_ or DOCKER_")
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            raise RefusedError(f"env {key} is not a string or a number")
        text = fill(str(value), ports, host, secrets, url)
        if len(text) > MAX_VALUE or any(char in text for char in "\n\r\0'"):
            raise RefusedError(f"env {key} has a newline or a quote, or is too long")
        checked[key] = text
    return checked


def check_suites(suites: object) -> dict[str, dict[str, Any]]:
    """The repository's suites: a service of the stack and the command to run in it."""
    if not isinstance(suites, dict):
        raise RefusedError("suites is a table")
    checked = {}
    for name, suite in suites.items():
        if not (isinstance(name, str) and NAME.fullmatch(name) and isinstance(suite, dict)):
            raise RefusedError(f"suite {name!r} is not a name with a table")
        service, command = suite.get("service"), suite.get("command", [])
        if not (isinstance(service, str) and SERVICE.fullmatch(service)):
            raise RefusedError(f"suite {name}: service is a service's name")
        if not (isinstance(command, list) and len(command) <= MAX_COMMAND and all(isinstance(word, str) for word in command)):
            raise RefusedError(f"suite {name}: command is a list of words")
        checked[name] = {"service": service, "command": command}
    return checked


def resolve_profile(tree: str, request: dict[str, Any]) -> dict[str, Any]:
    """The profile the request names, checked, with its values filled in."""
    name = request.get("profile") or "default"
    if not NAME.fullmatch(name):
        raise RefusedError(f"profile {name!r} is not a name")
    override = request.get("override") or {}
    if not isinstance(override, dict):
        raise RefusedError("the instance's override is not a table")
    profiles = read_profiles(tree, override)
    profile = profiles["profiles"].get(name)
    if not isinstance(profile, dict):
        known = ", ".join(sorted(profiles["profiles"])) or "none"
        raise RefusedError(f"no profile {name!r}; the repository has {known}")
    ports = request["ports"]
    secrets = Secrets(values=dict(request.get("secrets") or {}))
    page = profile.get("page", 1)
    if isinstance(page, bool) or not isinstance(page, int) or not 1 <= page <= len(ports):
        raise RefusedError(f"page is the number of one of the caller's {len(ports)} ports")
    https = request.get("https_port")
    if https is not None and page != 1:
        raise RefusedError(f"page is 1 on this instance, whose host serves the first port's page with HTTPS on port {https}")
    return {
        "profile": name,
        "files": check_compose_files(tree, profile.get("compose", ["compose.yaml"])),
        "env": check_env(profile.get("env"), ports, request.get("host", ""), secrets, page_origin(request, page)),
        "page": page,
        "suites": check_suites(profiles["suites"]),
        "compose_profiles": check_compose_profiles(profile.get("compose_profiles", [])),
        # Names only: the keys whose values hold a secret, which the tree's
        # .env and the stack's record leave out.
        "secret_keys": sorted(key for key, value in (profile.get("env") or {}).items() if "{secret:" in str(value)),
        "empty_secrets": secrets.empty(),
    }


def page_origin(request: dict[str, Any], page: int) -> str:
    """Where the user opens the page: behind the host's HTTPS on an instance
    that has it, the page's own port otherwise.
    """
    host, ports, https = request.get("host", ""), request["ports"], request.get("https_port")
    if https is not None:
        return f"https://{host}:{ports[https - 1]}"
    return f"http://{host}:{ports[page - 1]}"


def https_refusals(services: dict[str, dict[str, Any]], request: dict[str, Any]) -> list[str]:
    """A service publishing the port the host's HTTPS holds."""
    https = request.get("https_port")
    if https is None:
        return []
    held = request["ports"][https - 1]
    refusals = []
    for name, service in services.items():
        published: list[int] = []
        for port in service.get("ports") or []:
            published += published_ports(port) if isinstance(port, dict) else []
        if held in published:
            refusals.append(f"{name}: host port {held} is the host's HTTPS in front of the page, not the stack's")
    return refusals


def check_compose_profiles(names: object) -> list[str]:
    """The compose profiles the stack profile turns on: only their services
    are checked, and only they are in the config the stack runs from.
    """
    if not (isinstance(names, list) and len(names) <= MAX_COMPOSE_PROFILES):
        raise RefusedError(f"compose_profiles is a list of at most {MAX_COMPOSE_PROFILES} compose profiles")
    for name in names:
        if not (isinstance(name, str) and SERVICE.fullmatch(name)):
            raise RefusedError(f"compose profile {name!r} is not a name")
    return list(dict.fromkeys(names))


def profile_flags(names: list[str]) -> list[str]:
    """docker-compose's options for the compose profiles turned on."""
    return [word for name in names for word in ("--profile", name)]


def write_tree_env(tree: str, text: str) -> None:
    """The stack's env as the tree's .env too, compose's default, which a
    service's `env_file: .env` reads; the repository's own goes, and a link
    is replaced, never written through. It is 0644 whatever the agent's
    umask, like the checkout, for an image that copies it.
    """
    path = os.path.join(tree, ".env")
    if os.path.isdir(path) and not os.path.islink(path):
        raise RefusedError("the repository's .env is a directory; the stack's env goes there")
    if os.path.lexists(path):
        os.unlink(path)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    os.fchmod(descriptor, 0o644)
    with os.fdopen(descriptor, "w") as env_file:
        env_file.write(text)


def without_secrets(env: dict[str, str], secret_keys: list[str]) -> dict[str, str]:
    """The profile's values without those that hold a secret: what the
    tree's .env and the stack's record may have.
    """
    return {key: value for key, value in env.items() if key not in secret_keys}


def env_file_text(ports: list[int], host: str, env: dict[str, str]) -> str:
    """The stack's env file: the caller's ports, then the profile's values."""
    lines = [f"STACK_PORT_{index}='{port}'" for index, port in enumerate(ports, start=1)]
    lines += ["STACK_BIND='0.0.0.0'", f"STACK_HOST='{host}'"]
    lines += [f"{key}='{value}'" for key, value in env.items()]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------
# The compose config: what the stack may not reach
# ---------------------------------------------------------------------


def published_ports(port: dict[str, Any]) -> list[int]:
    """The host ports one ports entry publishes; none for a port left to the host."""
    published = str(port.get("published") or "")
    if published in ("", "0"):
        return []
    first, _, last = published.partition("-")
    if not first.isdecimal() or (last and not last.isdecimal()):
        raise RefusedError(f"published port {published!r} is not a number")
    return list(range(int(first), int(last or first) + 1))


def own_mode(mode: object) -> bool:
    """Whether a network, pid or ipc mode is the service's own, none, or its stack's."""
    return mode is None or (isinstance(mode, str) and OWN_MODE.fullmatch(mode) is not None)


def build_refusals(name: str, build: dict[str, Any], tree: str) -> list[str]:
    """What a service's build asks for that the stack may not have."""
    found: list[str] = []
    contexts = [build.get("context"), *(build.get("additional_contexts") or {}).values()]
    found += [
        f"{name}: a build context outside the repository, {context}"
        for context in contexts
        if isinstance(context, str) and context.startswith("/") and not inside(tree, context)
    ]
    dockerfile = build.get("dockerfile")
    if isinstance(dockerfile, str) and isinstance(build.get("context"), str):
        path = os.path.join(build["context"], dockerfile)
        if path.startswith("/") and not inside(tree, path):
            found.append(f"{name}: a Dockerfile outside the repository, {dockerfile}")
    if build.get("network") not in BUILD_NETWORKS:
        found.append(f"{name}: a build on the network {build['network']}")
    found += [f"{name}: build {key}" for key in ("privileged", "entitlements", "ssh") if build.get(key)]
    return found


def service_refusals(name: str, service: dict[str, Any], tree: str, ports: list[int]) -> list[str]:
    """What one service of the stack asks for that the stack may not have."""
    found: list[str] = []
    if service.get("privileged"):
        found.append(f"{name}: privileged")
    found += [f"{name}: {key}" for key in NEVER if service.get(key)]
    found += [f"{name}: security_opt {opt}" for opt in service.get("security_opt") or [] if opt not in SECURITY_OPTS]
    found += [f"{name}: {key} {service[key]}" for key in NAMESPACES if not own_mode(service.get(key))]
    found += [f"{name}: the label {label}" for label in service.get("labels") or {} if str(label).startswith(OWN_LABELS)]
    reservations = ((service.get("deploy") or {}).get("resources") or {}).get("reservations") or {}
    if reservations.get("devices") or reservations.get("generic_resources"):
        found.append(f"{name}: devices reserved through deploy")
    for volume in service.get("volumes") or []:
        if volume.get("type") == "bind" and not inside(tree, volume.get("source", "/")):
            found.append(f"{name}: a bind mount from {volume.get('source')}, outside the repository")
        elif volume.get("type") == "bind" and not volume.get("read_only"):
            # A container writing the tree could swap a bind mount's source
            # for a link out of it before the next start creates a container.
            found.append(f"{name}: a bind mount from {volume.get('source')} not read-only; add :ro, or use a volume")
        elif volume.get("type") not in ("bind", "volume", "tmpfs"):
            found.append(f"{name}: a volume of type {volume.get('type')}")
    for port in service.get("ports") or []:
        outside = [number for number in published_ports(port) if number not in ports]
        if outside:
            found.append(f"{name}: host port {', '.join(map(str, outside))}, not one of the caller's {ports[0]}–{ports[-1]}")
    return found + build_refusals(name, service.get("build") or {}, tree)


def config_refusals(config: dict[str, Any], tree: str, ports: list[int]) -> list[str]:
    """Everything in the stack's compose config that reaches past the stack."""
    name = config.get("name", "")
    found: list[str] = []
    for service_name, service in sorted((config.get("services") or {}).items()):
        found += service_refusals(service_name, service, tree, ports)
    for kind in ("volumes", "networks"):
        for key, item in sorted((config.get(kind) or {}).items()):
            item = item or {}  # noqa: PLW2901 -- an entry with no settings is null
            if item.get("external"):
                found.append(f"{kind} {key}: external")
            elif not str(item.get("name", f"{name}_{key}")).startswith(f"{name}_"):
                found.append(f"{kind} {key}: named {item['name']}, not the stack's own")
            if item.get("driver_opts"):
                found.append(f"{kind} {key}: driver_opts")
    found += stack_image_refusals(config.get("services") or {})
    for kind in ("secrets", "configs"):
        for key, item in sorted((config.get(kind) or {}).items()):
            source = (item or {}).get("file")
            if source and not inside(tree, source):
                found.append(f"{kind} {key}: a file outside the repository, {source}")
    return found


def stack_image_refusals(services: dict[str, dict[str, Any]]) -> list[str]:
    """A service that runs a stack's image it does not build: the images
    the stacks build are named stack-..., and another caller's is not this
    stack's to run. A name one of its own services builds under passes.
    """
    built = {str(service["image"]) for service in services.values() if service.get("build") and service.get("image")}
    found = []
    for name, service in sorted(services.items()):
        image = str(service.get("image") or "")
        if not service.get("build") and image not in built and image.removeprefix("localhost/").startswith("stack-"):
            found.append(f"{name}: the image {image}, a stack's that this one does not build")
    return found


def strings_in(value: object) -> Iterator[str]:
    """Every string in a value of the compose config, keys included."""
    if isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from strings_in(item)
    elif isinstance(value, list):
        for item in value:
            yield from strings_in(item)
    elif value is not None:
        yield str(value)


def secret_build_refusals(services: dict[str, dict[str, Any]], secrets: dict[str, str]) -> list[str]:
    """A build holding a secret's value, in an arg, a label, an inline
    Dockerfile or anywhere else: it would stay in the image and its build
    cache, in the store every caller builds in.
    """
    values = [secret for secret in secrets.values() if secret]

    def holds(value: object) -> bool:
        return any(secret in text for text in strings_in(value) for secret in values)

    refusals: list[str] = []
    for name, service in services.items():
        build = service.get("build")
        if not isinstance(build, dict):
            continue
        for key, value in build.items():
            if key == "args" and isinstance(value, dict):
                held = [f"arg {arg}" for arg, item in value.items() if holds(item)]
            else:
                held = [key] if holds(value) else []
            refusals += [f"{name}: build {what} holds a secret, which would stay in the image" for what in held]
    return refusals


def relocate(value: object, old: str, new: str) -> object:
    """The value with every path in or at old moved to new: the config checked
    in the staged tree, as the stack's tree runs it.
    """
    if isinstance(value, str) and (value == old or value.startswith(old + os.sep)):
        return new + value[len(old) :]
    if isinstance(value, dict):
        return {key: relocate(item, old, new) for key, item in value.items()}
    if isinstance(value, list):
        return [relocate(item, old, new) for item in value]
    return value


def with_resets(config: dict[str, Any], project_name: str) -> dict[str, Any]:
    """The checked config as the stack runs it: no fixed container_name,
    which would clash across callers, no healthcheck start_interval, which
    podman does not take, and the images it builds named its own.
    """
    services = {}
    for name, service in (config.get("services") or {}).items():
        service = {key: value for key, value in service.items() if key != "container_name"}  # noqa: PLW2901 -- a copy, reset
        if "start_interval" in (service.get("healthcheck") or {}):
            service["healthcheck"] = {key: value for key, value in service["healthcheck"].items() if key != "start_interval"}
        services[name] = service
    return {**config, "services": own_images(services, project_name)}


def own_images(services: dict[str, dict[str, Any]], project_name: str) -> dict[str, dict[str, Any]]:
    """The services with every image they build named after the stack: the
    callers share one image store, so a name the compose file gives could
    replace another stack's image, or the image another stack pulls. A
    service that runs a built image by the name the file gave it gets the
    stack's name for it too.
    """
    renamed: dict[str, str] = {}
    taken: set[str] = set()
    named = {}
    for name, service in sorted(services.items()):
        if not service.get("build"):
            named[name] = service
            continue
        own = image_name(project_name, name, taken)
        if service.get("image"):
            renamed.setdefault(str(service["image"]), own)
        named[name] = {**service, "image": own}
    for name, service in named.items():
        if not service.get("build") and str(service.get("image")) in renamed:
            named[name] = {**service, "image": renamed[str(service["image"])]}
    return named


def image_name(project_name: str, service: str, taken: set[str]) -> str:
    """A name of the stack's own for the image a service builds, one an
    image reference can hold and no other service of the stack's has.
    """
    base = f"{project_name}-" + (re.sub(r"[^a-z0-9]+", "-", service.lower()).strip("-") or "service")
    name, number = base, 1
    while name in taken:
        number += 1
        name = f"{base}-{number}"
    taken.add(name)
    return name


# ---------------------------------------------------------------------
# The actions
# ---------------------------------------------------------------------


class Agent:
    """The actions, on the exec account's podman and the stacks' trees."""

    def __init__(self, config: Config, system: System, out: Out) -> None:
        """Act for the instance described by config, answering on out."""
        self.config = config
        self.system = system
        self.out = out

    def emit(self, command: list[str]) -> int:
        """Run a command, passing its output on, and return its status."""
        status = 1
        for item in self.system.stream(command):
            if isinstance(item, int):
                status = item
            else:
                self.out.line(item)
        return status

    def quiet(self, command: list[str]) -> str:
        """A command whose failure leaves nothing worse: removing what is gone."""
        try:
            return self.system.run(command)
        except subprocess.CalledProcessError:
            return ""

    def containers(self, name: str | None = None) -> list[dict[str, Any]]:
        """The stacks' containers, or one project's."""
        label = f"label={PROJECT_LABEL}" + (f"={name}" if name is not None else "")
        return json.loads(self.system.run(["podman", "ps", "--all", "--format", "json", "--filter", label]) or "[]")

    def remove_tree(self, path: str) -> None:
        """Remove a directory a container may have written to as a subordinate uid."""
        if os.path.lexists(path):
            self.system.run(["podman", "unshare", "rm", "-rf", "--", path])

    def remove_containers(self, stack: Stack) -> None:
        """Remove the stack's containers and networks, keeping its volumes."""
        ids = [container["Id"] for container in self.containers(stack.project)]
        if ids:
            self.quiet(["podman", "rm", "--force", "--depend", *ids])
        names = self.quiet(["podman", "network", "ls", "--quiet", "--filter", f"label={PROJECT_LABEL}={stack.project}"]).split()
        if names:
            self.quiet(["podman", "network", "rm", "--force", *names])

    def disk_used(self) -> int:
        """Bytes of the account's container storage and the stacks' trees."""
        paths = [path for path in (self.config.storage_dir, self.config.stacks_dir) if os.path.isdir(path)]
        if not paths:
            return 0
        output = self.system.run(["podman", "unshare", "du", "-s", "-b", "-x", *paths])
        return sum(int(line.split()[0]) for line in output.splitlines() if line.split() and line.split()[0].isdecimal())

    def make_room_on_disk(self) -> None:
        """Prune unused images when the account is past its disk cap, and
        refuse when it still is.
        """
        if self.disk_used() <= self.config.disk_max_bytes:
            return
        self.out.line("Past the disk cap: removing images no container uses.")
        self.quiet(["podman", "image", "prune", "--all", "--force"])
        self.quiet(["podman", "builder", "prune", "--all", "--force"])
        used = self.disk_used()
        if used > self.config.disk_max_bytes:
            raise RefusedError(
                f"the instance's stacks take {used / 1024**3:.1f} GB, past its cap of "
                f"{self.config.disk_max_bytes / 1024**3:.1f} GB, with no unused image left to remove",
            )

    def confined(self, stack: Stack, command: list[str]) -> list[str]:
        """command, run with only the stack's own directory of the account's in sight."""
        home = os.path.expanduser("~")
        hidden = [stack.dir, home, self.config.stacks_dir]
        return ["unshare", "--user", "--map-root-user", "--mount", "sh", "-c", CONFINE, "confine", *hidden, *command]

    def check_out(self, bundle: str, target: str) -> str:
        """A fresh repository at target with the bundle's HEAD checked out; its commit."""
        self.remove_tree(target)
        safe = ["-c", "core.hooksPath=/dev/null", "-c", "fetch.fsckObjects=true", "-c", "advice.detachedHead=false"]
        try:
            with umask(TREE_UMASK):
                return self._check_out(bundle, target, safe)
        except subprocess.CalledProcessError as error:
            raise RefusedError(f"the bundle did not check out: {(error.stderr or error.output or '').strip()}") from error

    def _check_out(self, bundle: str, target: str, safe: list[str]) -> str:
        """The git commands of check_out."""
        self.system.run(["git", "init", "--quiet", target])
        heads = self.system.run(["git", "-C", target, "bundle", "list-heads", bundle]).split("\n")
        refs = [line.split()[1] for line in heads if len(line.split()) == 2]  # noqa: PLR2004 -- "<commit> <ref>"
        if not refs:
            raise RefusedError("the bundle has no head")
        ref = "HEAD" if "HEAD" in refs else refs[0]
        self.system.run(["git", "-C", target, *safe, "fetch", "--quiet", "--no-tags", bundle, ref])
        self.system.run(["git", "-C", target, *safe, "checkout", "--quiet", "--detach", "FETCH_HEAD"])
        return self.system.run(["git", "-C", target, "rev-parse", "HEAD"]).strip()

    def stage(self, request: dict[str, Any], bundle: str, staged: Stack) -> tuple[str, dict[str, Any], dict[str, Any]]:
        """Check the bundle out beside the stack and check its config there:
        its commit, profile and config, or a refusal that leaves the stack
        as it was.
        """
        try:
            self.make_room_on_disk()
            commit = self.check_out(bundle, staged.tree)
        finally:
            if os.path.exists(bundle):
                os.unlink(bundle)
        profile = resolve_profile(staged.tree, request)
        self.out.line(f"{request['repo']} at {commit[:12]}, profile {profile['profile']}.")
        if profile["empty_secrets"]:
            self.out.line(f"Secrets the instance does not give this repository, left empty: {', '.join(profile['empty_secrets'])}.")
        ports, host = request["ports"], request.get("host", "")
        with open(staged.env_file, "w") as env_file:
            env_file.write(env_file_text(ports, host, profile["env"]))
        with open(staged.secrets_file, "w") as secrets_file:
            json.dump(request.get("secrets") or {}, secrets_file)
        # Never a secret in the tree: it is the build's context, and a
        # service's to bind-mount.
        write_tree_env(staged.tree, env_file_text(ports, host, without_secrets(profile["env"], profile["secret_keys"])))
        # The services of the compose profiles turned on, and only those: the
        # rest are not in the config the stack runs from, so nothing starts them.
        flags = profile_flags(profile["compose_profiles"])
        command = self.confined(staged, [*staged.source(profile), *flags, "config", "--format", "json"])
        try:
            config = json.loads(self.system.run(command))
        except subprocess.CalledProcessError as error:
            raise RefusedError(f"docker-compose cannot read the stack: {(error.stderr or '').strip()}") from error
        refusals = config_refusals(config, staged.tree, request["ports"])
        refusals += secret_build_refusals(config.get("services") or {}, request.get("secrets") or {})
        refusals += https_refusals(config.get("services") or {}, request)
        unknown = sorted({suite["service"] for suite in profile["suites"].values()} - set(config.get("services") or {}))
        refusals += [f"a suite's service {name} is not one of the stack's" for name in unknown]
        if refusals:
            raise RefusedError("the stack reaches past itself:\n  " + "\n  ".join(refusals))
        return commit, profile, config

    def up(self, request: dict[str, Any], bundle: str) -> int:
        """Build the bundle's commit and start it as the caller's stack,
        replacing its last only once the new one has passed its checks: a
        refused up leaves the last one running, and says kept. The last
        one's volumes stay only when this commit follows its commit.
        """
        stack = Stack(self.config, request["caller"])
        staged = Stack(self.config, request["caller"], staged=True)
        self.out.hide(request.get("secrets") or {})
        try:
            commit, profile, config = self.stage(request, bundle, staged)
        except RefusedError as error:
            self.remove_staged(staged)
            return self.out.done(1, message=f"{error}\nThe last stack is as it was.", kept=True)
        except BaseException:
            # A failure of git or podman: the staged env and secrets go too,
            # and the failure is what the caller is told.
            with contextlib.suppress(subprocess.CalledProcessError, OSError):
                self.remove_staged(staged)
            raise
        self.remove_containers(stack)
        volumes = self.replace_volumes(stack, request["repo"], commit, staged.tree)
        if volumes is None:
            self.remove_staged(staged)
            return self.out.done(1, message="the last stack's volumes could not be removed: down, then up again")
        self.remove_tree(stack.tree)
        for stale in (stack.checked, stack.meta_file):
            if os.path.exists(stale):
                os.unlink(stale)
        os.rename(staged.tree, stack.tree)
        os.replace(staged.env_file, stack.env_file)
        os.replace(staged.secrets_file, stack.secrets_file)
        with open(stack.checked, "w") as checked:
            json.dump(with_resets(cast("dict[str, Any]", relocate(config, staged.tree, stack.tree)), stack.project), checked)
        meta = {"repo": request["repo"], "commit": commit, "up_at": self.system.now(), **profile}
        meta["page_port"] = request["ports"][profile["page"] - 1]
        meta["env"] = without_secrets(profile["env"], profile["secret_keys"])
        with open(f"{stack.meta_file}.new", "w") as meta_file:
            json.dump(meta, meta_file)
        os.replace(f"{stack.meta_file}.new", stack.meta_file)
        failure = self.bring_up(stack, ["--build", "--force-recreate", "--remove-orphans"])
        if failure is not None:
            return self.out.done(1, message=failure, commit=commit, volumes=volumes)
        return self.out.done(0, commit=commit, profile=profile["profile"], page=profile["page"], volumes=volumes)

    def remove_staged(self, staged: Stack) -> None:
        """Remove what a refused or abandoned up staged beside the stack."""
        self.remove_tree(staged.tree)
        for path in (staged.env_file, staged.secrets_file):
            if os.path.exists(path):
                os.unlink(path)

    def replace_volumes(self, stack: Stack, repo: str, commit: str, tree: str) -> str | None:
        """After the last stack's containers: its volumes kept when this
        commit continues it, "kept"; else removed, "new"; None when some
        could not be, which the new stack must not start on.
        """
        try:
            last = stack.meta()
        except ValueError:
            last = None
        followed = self.followed_commit(last if isinstance(last, dict) else None, repo, commit, tree)
        if followed is not None:
            self.out.line(f"The volumes are kept: the last stack was of {followed[:12]}, this commit or one before it.")
            return "kept"
        if self.remove_volumes(stack):
            return None
        self.out.line("The volumes start empty: the last stack, if any, was of another branch.")
        return "new"

    def followed_commit(self, last: dict[str, Any] | None, repo: str, commit: str, tree: str) -> str | None:
        """The last stack's commit when the new one continues it, and may
        keep its volumes: the same repository, and that commit this one or
        an ancestor of it in the new tree; else None. Another branch's data
        was made and migrated by code this commit does not have, and a
        commit the bundle does not carry counts as another branch's.
        """
        if last is None or last.get("repo") != repo:
            return None
        followed = str(last.get("commit", ""))
        if not COMMIT.fullmatch(followed):
            return None
        try:
            self.system.run(["git", "-C", tree, "merge-base", "--is-ancestor", followed, commit])
        except subprocess.CalledProcessError:
            return None
        return followed

    def bring_up(self, stack: Stack, flags: list[str]) -> str | None:
        """docker-compose up, detached, then the services watched until they
        settle: None, or why they did not, with the stack stopped and the
        failed services' logs passed on.
        """
        if self.emit([*stack.compose(), "up", "--detach", *flags]) != 0:
            services, reason = self.services_in(self.containers(stack.project), (FAILED,)), "docker-compose up failed"
        else:
            try:
                failure = self.watch(stack, self.page(stack))
            except (subprocess.CalledProcessError, OSError, ValueError) as error:
                detail = getattr(error, "stderr", "") or str(error)
                failure = {}, f"the stack could not be watched: {detail.strip()}"
            if failure is None:
                return None
            services, reason = failure
        self.fail(stack, services)
        return f"{reason}; the stack is stopped."

    def services_in(
        self,
        containers: list[dict[str, Any]],
        verdicts: tuple[str, ...],
        restarts: dict[str, int] | None = None,
    ) -> dict[str, str]:
        """The services whose containers are in one of verdicts, by the
        worst verdict of their containers; with restarts, the count each
        container was first seen with, one restarted since is failed.
        """
        found: dict[str, str] = {}
        for service, container in service_containers(containers):
            verdict = FAILED if restarts is not None and restarted(container, restarts) else verdict_of(container)
            if verdict in verdicts and found.get(service) != FAILED:
                found[service] = verdict
        return found

    def watch(self, stack: Stack, page: Page | None) -> tuple[dict[str, str], str] | None:
        """Poll the stack's containers until every one has stayed settled
        for SETTLE_SECONDS and the page, where it has one, has answered:
        None; or the services that failed, at once, or that had not settled,
        or the page's service when the page had not answered, by
        WAIT_SECONDS, and why.
        """
        deadline = self.system.clock() + WAIT_SECONDS
        settled_at = None
        answered = page is None
        probe_errors = 0
        restarts: dict[str, int] = {}
        while True:
            containers = self.containers(stack.project)
            found = self.services_in(containers, (FAILED, PENDING), restarts)
            failed = {name: verdict for name, verdict in found.items() if verdict == FAILED}
            if failed:
                return failed, f"{', '.join(sorted(failed))} exited with an error, restarted or is unhealthy"
            now = self.system.clock()
            if found:
                settled_at = None
            else:
                if not answered and page is not None:
                    answered, probe_errors = self.ask_page(page, containers, probe_errors)
                if settled_at is None:
                    settled_at = now
                elif now - settled_at >= SETTLE_SECONDS and answered:
                    return None
            if now >= deadline:
                if found or answered or page is None:
                    return found, f"{', '.join(sorted(found)) or 'the stack'} did not run, or was not healthy, within {WAIT_SECONDS} s"
                return {page.service: PENDING}, f"the page on port {page.port} did not answer within {WAIT_SECONDS} s"
            self.system.sleep(POLL_SECONDS)

    def page(self, stack: Stack) -> Page | None:
        """The stack's page, from its record and its checked config; None for
        a stack with none, or brought up by an agent before pages were asked.
        """
        meta = stack.meta()
        port = meta.get("page_port") if isinstance(meta, dict) else None
        if isinstance(port, bool) or not isinstance(port, int):
            return None
        with open(stack.checked) as checked:
            return page_of(json.load(checked), port)

    def ask_page(self, page: Page, containers: list[dict[str, Any]], errors: int) -> tuple[bool, int]:
        """Whether the page answered, and the probe's failures in a row since
        it last ran: a container gone between the listing and the probe
        fails it once, a probe that cannot run fails it every time, and
        raises at the PROBE_ERRORS_MAXth.
        """
        try:
            return self.page_answers(page, containers), 0
        except subprocess.CalledProcessError:
            if errors + 1 >= PROBE_ERRORS_MAX:
                raise
            return False, errors + 1

    def page_answers(self, page: Page, containers: list[dict[str, Any]]) -> bool:
        """Whether the page's service answers HTTP on its port, asked from
        inside a running container of it, in that container's network
        namespace: the host's is the sandbox's to keep the stacks off. A
        probe that could not run raises.
        """
        for service, container in service_containers(containers):
            pid = container.get("Pid")
            if service != page.service or container.get("State") != "running":
                continue
            if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
                continue
            probe = [sys.executable, "-I", os.path.abspath(__file__), "probe-page", str(page.target)]
            try:
                self.system.run(["podman", "unshare", "nsenter", "--target", str(pid), "--net", "--", *probe])
            except subprocess.CalledProcessError as error:
                if error.returncode != PROBE_NO_ANSWER:
                    raise
                continue
            return True
        return False

    def fail(self, stack: Stack, services: dict[str, str]) -> None:
        """After an up or a start that failed: the services' logs, then the
        stack stopped, even when the logs cannot be read.
        """
        try:
            if services:
                self.out.line(f"The logs of {', '.join(sorted(services))}:")
                self.emit([*stack.compose(), "logs", "--no-color", "--tail", str(FAILED_LOG_TAIL), *sorted(services)])
        except (subprocess.CalledProcessError, OSError) as error:
            self.out.line(f"The logs could not be read: {error}")
        finally:
            self.stop(stack)

    def stack_meta(self, stack: Stack) -> dict[str, Any]:
        """The stack's record of its last up; refused when it has none."""
        meta = stack.meta()
        if meta is None:
            raise RefusedError("the caller has no stack: up first")
        self.out.hide(stack.secrets())
        return meta

    def run(self, request: dict[str, Any]) -> int:
        """Run a suite of the repository's in its service, against the stack."""
        stack = Stack(self.config, request["caller"])
        meta = self.stack_meta(stack)
        suite = meta["suites"].get(request.get("suite"))
        if suite is None:
            known = ", ".join(sorted(meta["suites"])) or "none"
            raise RefusedError(f"no suite {request.get('suite')!r}; the repository has {known}")
        status = self.emit([*stack.compose(), "run", "--rm", "--no-TTY", suite["service"], *suite["command"]])
        return self.out.done(status)

    def logs(self, request: dict[str, Any]) -> int:
        """The tail of the stack's logs, or of one service's."""
        stack = Stack(self.config, request["caller"])
        self.stack_meta(stack)
        service = request.get("service")
        if service is not None and not (isinstance(service, str) and SERVICE.fullmatch(service)):
            raise RefusedError("service is a service's name")
        command = [*stack.compose(), "logs", "--no-color", "--tail", str(LOG_TAIL)]
        return self.out.done(self.emit(command + ([service] if service is not None else [])))

    def service(self, request: dict[str, Any]) -> int:
        """Stop or start one service of the stack with compose's own stop or
        start, from the checked config: a service it does not have is not
        started, and nothing else is started with it.
        """
        stack = Stack(self.config, request["caller"])
        self.stack_meta(stack)
        service = request["service"]
        if service not in stack.services():
            raise RefusedError(f"the stack has no service {service}")
        return self.out.done(self.emit([*stack.compose(), request["action"], service]))

    def stop(self, stack: Stack) -> int:
        """Stop the stack's running containers, keeping everything."""
        ids = [container["Id"] for container in self.containers(stack.project) if container.get("State") == "running"]
        if ids:
            self.quiet(["podman", "stop", "--time", "10", *ids])
        return len(ids)

    def start(self, stack: Stack) -> int:
        """Bring a stopped stack back from its tree, building only what changed."""
        self.stack_meta(stack)
        failure = self.bring_up(stack, ["--force-recreate"])
        if failure is not None:
            return self.out.done(1, message=failure)
        return self.out.done(0)

    def down(self, stack: Stack) -> None:
        """Remove the stack, its volumes and its tree."""
        self.remove_containers(stack)
        self.remove_volumes(stack)
        self.remove_tree(stack.dir)

    def remove_volumes(self, stack: Stack) -> list[str]:
        """Remove the stack's volumes, its containers already gone; the
        names of those still there after.
        """
        listing = ["podman", "volume", "ls", "--quiet", "--filter", f"label={PROJECT_LABEL}={stack.project}"]
        names = self.quiet(listing).split()
        if not names:
            return []
        self.quiet(["podman", "volume", "rm", "--force", *names])
        try:
            return self.system.run(listing).split()
        except subprocess.CalledProcessError:
            return names

    def ps(self) -> int:
        """Every stack on the account: its state, when it was made, and its memory."""
        memory = self.memory()
        stacks: dict[str, dict[str, Any]] = {}
        for container in self.containers():
            name = (container.get("Labels") or {}).get(PROJECT_LABEL, "")
            if not (name.startswith("stack-") and CALLER.fullmatch(name.removeprefix("stack-"))):
                continue
            entry = stacks.setdefault(name.removeprefix("stack-"), {"state": "stopped", "created": 0, "containers": 0, "memory": 0})
            entry["containers"] += 1
            entry["created"] = max(entry["created"], container.get("Created", 0))
            entry["memory"] += memory.get(container["Id"][:12], 0)
            if container.get("State") == "running":
                entry["state"] = "running"
        if os.path.isdir(self.config.stacks_dir):
            for caller in os.listdir(self.config.stacks_dir):
                if CALLER.fullmatch(caller):
                    stacks.setdefault(caller, {"state": "none", "created": 0, "containers": 0, "memory": 0})
        return self.out.done(0, stacks=stacks)

    def memory(self) -> dict[str, int]:
        """Each running container's memory, by its short ID."""
        try:
            stats = json.loads(self.system.run(["podman", "stats", "--all", "--no-stream", "--format", "json"]) or "[]")
        except (subprocess.CalledProcessError, ValueError):
            return {}
        return {str(item.get("id", ""))[:12]: size_of(str(item.get("mem_usage", "")).split("/")[0]) for item in stats or []}

    def prune(self) -> int:
        """Remove the images no container uses, keeping the build cache's layers."""
        self.quiet(["podman", "image", "prune", "--force"])
        return self.out.done(0)


def restarted(container: dict[str, Any], restarts: dict[str, int]) -> bool:
    """Whether the container restarted since the watch first saw it: a
    restart policy brings back a service that dies, often before a poll
    sees it ended.
    """
    count = int(container.get("Restarts") or 0)
    return count > restarts.setdefault(str(container.get("Id", "")), count)


def verdict_of(container: dict[str, Any]) -> str:
    """Whether a container has settled: running, and healthy where it has a
    health check, or ended with status 0; failed: ended otherwise, dead or
    unhealthy; or pending, as one created or starting is. Podman shows one
    its restart policy is about to bring back as stopped.
    """
    state, status = container.get("State"), str(container.get("Status", ""))
    if state in ("exited", "stopped"):
        return SETTLED if container.get("ExitCode", 0) == 0 else FAILED
    if state == "dead" or (state == "running" and "(unhealthy)" in status):
        return FAILED
    if state == "running" and "(starting)" not in status:
        return SETTLED
    return PENDING


def size_of(text: str) -> int:
    """Bytes in a size podman prints, such as 12.5MB; 0 when it is none."""
    match = re.fullmatch(r"\s*([0-9.]+)\s*([a-zA-Z]*)\s*", text)
    if not match:
        return 0
    unit = SIZE_UNITS.get(match.group(2).lower() or "b", 0)
    try:
        return int(float(match.group(1)) * unit)
    except ValueError:
        return 0


# ---------------------------------------------------------------------
# One connection
# ---------------------------------------------------------------------


def check_request(config: Config, request: object) -> dict[str, Any]:
    """The request, if it is one the agent takes: the front is trusted to
    have authenticated it, not to have shaped it.
    """
    if not isinstance(request, dict) or request.get("action") not in ACTIONS:
        raise RefusedError(f"the request wants an action of {', '.join(ACTIONS)}")
    if "request" in request and not (isinstance(request["request"], str) and REQUEST_ID.fullmatch(request["request"])):
        raise RefusedError("request is the front's id of it")
    if request["action"] in ("ps", "prune"):
        return request
    caller = request.get("caller")
    if not (isinstance(caller, str) and CALLER.fullmatch(caller)):
        raise RefusedError("caller is a name of lowercase letters, digits and dashes")
    if "secrets" in request and request["action"] != "up":
        raise RefusedError("only up takes secrets")
    check_service(request)
    if request["action"] == "up":
        check_up(config, request)
    return request


def check_service(request: dict[str, Any]) -> None:
    """A service named only where an action takes one, and as a name."""
    if "service" not in request:
        return
    if request["action"] not in ("logs", "stop", "start"):
        raise RefusedError("only logs, stop and start take a service")
    if not (isinstance(request["service"], str) and SERVICE.fullmatch(request["service"])):
        raise RefusedError("service is a service's name")


def check_up(config: Config, request: dict[str, Any]) -> None:
    """An up's ports within the instance's, its repository and its bundle's size."""
    check_secrets(request.get("secrets"))
    ports = request.get("ports")
    if not (
        isinstance(ports, list)
        and 1 <= len(ports) <= MAX_PORTS
        and all(isinstance(port, int) and config.ports_start <= port <= config.ports_end for port in ports)
    ):
        raise RefusedError(f"ports are 1 to {MAX_PORTS} of the instance's {config.ports_start}–{config.ports_end}")
    if not (isinstance(request.get("repo"), str) and REPO.fullmatch(request["repo"])):
        raise RefusedError("repo is owner/name")
    if not (isinstance(request.get("bundle_size"), int) and request["bundle_size"] > 0):
        raise RefusedError("up wants the bundle's size")
    https = request.get("https_port")
    if https is not None and (isinstance(https, bool) or not isinstance(https, int) or not 1 < https <= len(ports)):
        raise RefusedError(f"https_port is the number of one of the caller's ports but the first, 2 to {len(ports)}")


def check_secrets(secrets: object) -> None:
    """The secrets the front sends with an up: names and values the env
    file can hold, as the play checked them.
    """
    if secrets is None:
        return
    if not isinstance(secrets, dict):
        raise RefusedError("secrets are a table of names and values")
    for name, value in secrets.items():
        if not (isinstance(name, str) and SECRET_NAME.fullmatch(name)):
            raise RefusedError("a secret's name is lowercase letters, digits and _")
        if not (isinstance(value, str) and MIN_SECRET <= len(value) <= MAX_VALUE) or any(char in value for char in "\n\r\0'"):
            raise RefusedError(f"secret {name} is not a value the env file can hold")


def receive_bundle(conn: IO[bytes], size: int, path: str) -> None:
    """The bundle's bytes, exactly size of them, into a file of the agent's own."""
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    remaining = size
    with open(path, "wb") as bundle:
        while remaining:
            chunk = conn.read(min(CHUNK, remaining))
            if not chunk:
                raise RefusedError("the bundle ended early")
            bundle.write(chunk)
            remaining -= len(chunk)


def dispatch(agent: Agent, config: Config, conn: IO[bytes], request: dict[str, Any]) -> int:
    """Carry out a checked request."""
    action = request["action"]
    if action == "up":
        bundle = os.path.join(config.stacks_dir, request["caller"], "incoming.bundle")
        receive_bundle(conn, request["bundle_size"], bundle)
        return agent.up(request, bundle)
    simple: dict[str, Callable[[], int]] = {
        "run": lambda: agent.run(request),
        "logs": lambda: agent.logs(request),
        "ps": agent.ps,
        "prune": agent.prune,
    }
    if action in simple:
        return simple[action]()
    if action in ("stop", "start") and "service" in request:
        return agent.service(request)
    stack = Stack(config, request["caller"])
    if action == "stop":
        return agent.out.done(0, stopped=agent.stop(stack))
    if action == "start":
        return agent.start(stack)
    agent.down(stack)
    return agent.out.done(0)


def log_failure(request: object, message: str) -> None:
    """One line to the journal for a refused or failed request: its id, its
    caller, its action and what the caller was told, which is all it holds.
    """
    fields = request if isinstance(request, dict) else {}
    caller, action = fields.get("caller"), fields.get("action")
    who = caller if isinstance(caller, str) and CALLER.fullmatch(caller) else "-"
    what = action if action in ACTIONS else "-"
    request_id = fields.get("request")
    known = isinstance(request_id, str) and REQUEST_ID.fullmatch(request_id) is not None
    print(f"{f'request {request_id}: ' if known else ''}{what} for {who}: {json.dumps(message)}", file=sys.stderr, flush=True)


def handle(config: Config, conn: IO[bytes], out: Out, system: System) -> int:
    """Read one request from conn and carry it out, answering on out."""
    request: object = None
    try:
        line = conn.readline(MAX_HEADER)
        try:
            request = json.loads(line or b"null")
        except ValueError as error:
            raise RefusedError("the request is one line of JSON") from error
        out.request = request
        return dispatch(Agent(config, system, out), config, conn, check_request(config, request))
    except RefusedError as error:
        message = str(error)
    except (subprocess.CalledProcessError, OSError) as error:
        detail = getattr(error, "stderr", "") or str(error)
        message = f"failed: {detail.strip()}"
    return out.done(1, message=message)


def main(argv: list[str]) -> int:
    """Serve one connection on stdin and stdout, as systemd hands it over;
    or, in a stack's network namespace, ask its page.
    """
    if len(argv) == 2 and argv[0] == "probe-page" and argv[1].isdigit():  # noqa: PLR2004 -- "probe-page" and the port
        return probe_page(int(argv[1]))
    if len(argv) != 2 or argv[0] != "serve":  # noqa: PLR2004 -- "serve" and the config
        print("usage: executor-agent serve <agent.json>", file=sys.stderr)
        return 2
    os.umask(0o077)
    config = load_config(argv[1])
    system = System(run=run_command, stream=stream_command)
    # Answered: the request's status is in the answer, and a failed unit
    # is left for an agent that could not answer.
    handle(config, sys.stdin.buffer, Out(sys.stdout.buffer), system)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
