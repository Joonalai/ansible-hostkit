#!/usr/bin/python3 -I
"""The front of an executor instance: who may ask, for what, and when.

Runs as the instance's front account, which has no podman and never opens
the code it is sent. It holds the callers' keys, checks them and the
instance's limits, keeps the list of stacks, and hands each request with
its bundle, unread, to the agent, the exec account's side, which builds and
runs the stacks. Four ways in, the same script:

    executor-front serve <instance.json>   one connection on the API socket
    executor-front serve-tls <instance.json>   one connection on the TLS port, a remote caller's
    executor-front page <instance.json>    the phone page, on its port
    executor-front reap <instance.json>    the timer: stop idle stacks, remove old ones
    executor-front check <instance.json>   the play's: can the front reach the agent

A caller, a runner slot, a workbench repository or a person, sends one line
of JSON and, for up, its bundle's bytes after it:

    {"key": "...", "action": "up", "repo": "example/app", "profile": "default",
     "bundle_size": 123456}                                     then the bundle
    {"key": "...", "action": "run", "suite": "e2e"}
    {"key": "...", "action": "logs", "service": "web"}          service optional
    {"key": "...", "action": "stop", "service": "db"}           and start: one service
    {"key": "...", "action": "down"} | {"key": "...", "action": "list"}

and gets lines of JSON back: {"out": "..."} as the work goes, then one
{"exit": <status>, ...}. The caller is the key's, never the request's: a
key is checked against the caller files the play writes, each with the
key's SHA-256, the uid that may present it, and the repositories it may
bring up. A remote caller's file names the networks it connects from
instead of a uid, and its key is taken only on the TLS port, from them; a
local caller's only on the socket. A caller has one stack, on a block of the instance's ports that
is its own for as long as it is a caller.

At the limit, max_stacks running, an up evicts the running stack of a
caller that waits used longest ago, if it has been idle for
evict_idle_minutes; a caller's stack in use is never stopped to make room,
and a person's, a caller that does not wait, never at all: it holds its
place until its down or the reaper. People's stacks take at most
max_stacks - waiting_reserve places. With nothing to evict, a caller that
waits, a runner's job, waits for up to queue_timeout_minutes and then gets
status 75, to go on without a stack; one that does not, a person, gets 75
at once with what holds the room. While the instance's pause file exists,
no up or run is taken, the same way.

Every request has an id, the front's alone, never the caller's: the unit
instance systemd started for the connection, or a short random one. It is
in every line of the answer, as "request", in every line the front and the
agent write to their journals for it, and with the request the agent gets,
so `journalctl --grep <id>` finds both sides. Once the front has answered
it exits 0, whatever the request's status, which is in the answer and the
journal; only a front that could not answer, the agent out of reach, an
answer it could not write or an error before it, exits otherwise, so a
failed unit means a broken executor, never a refused request.

Standard library only, on the system python3.
"""

import contextlib
import contextvars
import fcntl
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import socket
import ssl
import struct
import sys
import threading
import time
from collections.abc import Callable, Generator, Iterator
from dataclasses import dataclass, field, replace
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import IO, Any, override

MAX_HEADER = 65536
CHUNK = 65536
NO_ROOM = 75
WAIT_STEP_S = 5
SAY_WAITING_EVERY_S = 60
PAGE_HEADER = "X-Executor"
CALLER = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")
NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")
SERVICE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,62}")
ACTIONS = ("up", "run", "logs", "stop", "start", "down", "list")
PAGE_PATH_PARTS = 3
# Longer than any up runs, the front's unit's RuntimeMaxSec.
STARTING_MAX_S = 2 * 3600
# How long a remote caller may leave a read waiting: the request's line,
# and each part of its bundle.
REMOTE_TIMEOUT_S = 60

# What the agent answers, one event at a time; the last has "exit".
Events = Iterator[dict[str, Any]]
# Sends the agent a request, and the bundle's file for up; yields its answer.
AgentCall = Callable[[dict[str, Any], "str | None"], Events]


class UnreachableError(OSError):
    """The agent cannot be reached: the executor is broken, not the request."""


# The id of the request being served, for every line of the journal.
REQUEST_ID: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="")
UNIT_INSTANCE = re.compile(r"[^/@]+@([A-Za-z0-9._:-]{1,96})\.service")


def new_request_id(cgroup: str = "/proc/self/cgroup") -> str:
    """The id of this connection's request: the instance of the unit
    systemd started for it, `<n>-<pid>-<uid>` on the socket, or a short
    random one outside such a unit.
    """
    try:
        with open(cgroup) as cgroup_file:
            last = cgroup_file.read().strip().rsplit("/", 1)[-1]
    except OSError:
        last = ""
    match = UNIT_INSTANCE.fullmatch(last)
    return str(match.group(1)) if match else secrets.token_hex(6)


def journal(text: str) -> None:
    """One line to the journal, with the request's id while one is served."""
    request_id = REQUEST_ID.get()
    print(f"request {request_id}: {text}" if request_id else text, file=sys.stderr, flush=True)


class RefusedError(Exception):
    """A request the front turns away, with the reason for the caller."""

    def __init__(self, message: str, status: int = 1) -> None:
        """A refusal with the exit status the caller gets."""
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Caller:
    """One caller of the instance, as the play registered it."""

    name: str
    key_sha256: str
    uid: int | None
    repos: tuple[str, ...]
    waits: bool
    # A remote caller's networks, which its key is taken from on the TLS
    # port alone; none for a local caller.
    sources: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = ()


@dataclass
class Instance:
    """The instance's config, as the play wrote it."""

    raw: dict[str, Any]

    def __getitem__(self, key: str) -> Any:  # noqa: ANN401 -- the config's values are of several kinds
        """A value of the config."""
        return self.raw[key]

    def caller_blocks(self) -> list[int]:
        """The first port of every block a caller can be given."""
        size = int(self["ports_per_caller"])
        start, end = int(self["callers_ports_start"]), int(self["ports_end"])
        return list(range(start, end - size + 2, size))

    def secrets_for(self, repo: str) -> dict[str, str]:
        """The secrets the instance gives repo's stacks, by name, read anew
        for every up; none when the play wrote none. A file that does not
        read refuses the up, without a word of what it holds.
        """
        path = self.raw.get("secrets_file")
        if not path:
            return {}
        try:
            with open(path) as secrets_file:
                granted = json.load(secrets_file)["repositories"].get(repo) or {}
        except FileNotFoundError:
            return {}
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            raise RefusedError("the instance's secrets do not read: tell its host") from None
        if not (isinstance(granted, dict) and all(isinstance(name, str) and isinstance(value, str) for name, value in granted.items())):
            raise RefusedError("the instance's secrets do not read: tell its host")
        return {str(name): str(value) for name, value in granted.items()}

    def remote_timeout_s(self) -> float:
        """How long the TLS port waits for a handshake and for each read,
        REMOTE_TIMEOUT_S unless the play set another.
        """
        return float(self.raw.get("remote_timeout_seconds") or REMOTE_TIMEOUT_S)

    def paused(self) -> bool:
        """Whether the host has paused the instance."""
        return os.path.exists(self["pause_file"])

    def callers(self) -> dict[str, Caller]:
        """Every registered caller, by name, from the callers directory."""
        found: dict[str, Caller] = {}
        directory = self["callers_dir"]
        for entry in sorted(os.listdir(directory)) if os.path.isdir(directory) else []:
            name = entry.removesuffix(".json")
            if not (entry.endswith(".json") and CALLER.fullmatch(name)):
                continue
            try:
                with open(os.path.join(directory, entry)) as caller_file:
                    raw = json.load(caller_file)
                found[name] = Caller(
                    name=name,
                    key_sha256=str(raw["key_sha256"]),
                    uid=None if raw.get("uid") in (None, "") else int(raw["uid"]),
                    repos=tuple(raw.get("repos") or ()),
                    waits=bool(raw.get("waits")),
                    sources=sources_of(raw),
                )
            except (OSError, ValueError, KeyError, TypeError) as error:
                journal(f"Skipping the caller file {entry}: {error}")
        return found


def sources_of(raw: dict[str, Any]) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    """A remote caller's networks; a file that has `from` but names none is
    refused, never taken for a local caller's that any uid may use.
    """
    if "from" not in raw:
        return ()
    sources = tuple(ipaddress.ip_network(str(source), strict=False) for source in raw["from"] or ())
    if not sources:
        raise ValueError("a remote caller's `from` names no network")
    return sources


def peer_address(address: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    """A peer's address, an IPv4 one as itself when it came mapped into IPv6."""
    found = ipaddress.ip_address(address.partition("%")[0])
    if isinstance(found, ipaddress.IPv6Address) and found.ipv4_mapped is not None:
        return found.ipv4_mapped
    return found


def authenticate(instance: Instance, key: object, uid: int | None, address: str | None = None) -> Caller:
    """The caller whose key this is, presented the way it may be: a local
    caller's on the socket, by a uid it allows; a remote caller's on the
    TLS port, from address, in one of its networks.
    """
    if not isinstance(key, str) or not key:
        raise RefusedError("refused: no key")
    digest = hashlib.sha256(key.encode()).hexdigest()
    for caller in instance.callers().values():
        if hmac.compare_digest(digest, caller.key_sha256):
            if caller.sources:
                if address is None:
                    raise RefusedError("refused: a remote caller's key is for the instance's TLS port")
                if not any(peer_address(address) in network for network in caller.sources):
                    raise RefusedError(f"refused: {address} may not use this key")
                return caller
            if address is not None:
                raise RefusedError("refused: a local caller's key is for the instance's socket")
            if caller.uid is not None and uid != caller.uid:
                raise RefusedError(f"refused: uid {uid} may not use this key")
            return caller
    raise RefusedError("refused: the key is no caller's")


# ---------------------------------------------------------------------
# The list of stacks, and the ports, under one lock
# ---------------------------------------------------------------------


@dataclass
class State:
    """The front's record: each caller's ports, and its stack."""

    ports: dict[str, int] = field(default_factory=dict)
    stacks: dict[str, dict[str, Any]] = field(default_factory=dict)

    def running(self) -> dict[str, dict[str, Any]]:
        """The stacks that take a place: running, or being brought up."""
        return {name: stack for name, stack in self.stacks.items() if stack.get("state") in ("running", "starting")}


@contextlib.contextmanager
def locked_state(instance: Instance) -> Generator[State]:
    """The state, read under the instance's lock and written back after."""
    directory = instance["state_dir"]
    path = os.path.join(directory, "state.json")
    with open(os.path.join(directory, "state.lock"), "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            with open(path) as state_file:
                raw = json.load(state_file)
        except FileNotFoundError:
            raw = {}
        state = State(ports=raw.get("ports", {}), stacks=raw.get("stacks", {}))
        yield state
        with open(path + ".new", "w") as state_file:
            json.dump({"ports": state.ports, "stacks": state.stacks}, state_file, indent=1, sort_keys=True)
        os.replace(path + ".new", path)


@contextlib.contextmanager
def caller_lock(instance: Instance, caller: str, *, wait: bool = True) -> Generator[None]:
    """One action at a time on a caller's stack. A caller's own request
    does not wait for its last: it is refused at once, before its bundle
    takes any disk, so one caller cannot pile up connections and spools.
    """
    os.makedirs(os.path.join(instance["state_dir"], "locks"), mode=0o700, exist_ok=True)
    with open(os.path.join(instance["state_dir"], "locks", f"{caller}.lock"), "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX if wait else fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RefusedError("refused: this caller's last request is still running; try again when it ends") from error
        yield


def ports_of(instance: Instance, state: State, caller: str) -> list[int]:
    """The caller's block of ports, given it the first time it asks."""
    if caller not in state.ports:
        taken = set(state.ports.values())
        free = [first for first in instance.caller_blocks() if first not in taken]
        if not free:
            raise RefusedError(f"the instance has no ports left for another caller: {len(state.ports)} hold them all")
        state.ports[caller] = free[0]
    first = state.ports[caller]
    return list(range(first, first + int(instance["ports_per_caller"])))


def holders(state: State, now: float) -> str:
    """What holds the instance's places, for a refusal."""
    lines = [
        f"{name} ({stack.get('repo', '?')}, used {(now - stack.get('used_at', now)) / 60:.0f} min ago)"
        for name, stack in sorted(state.running().items())
    ]
    return "; ".join(lines) or "nothing"


def waits(callers: dict[str, Caller], name: str) -> bool:
    """Whether the stack's caller waits for a place, and so may lose its own
    to another's; a caller no longer registered counts as one that waits.
    """
    caller = callers.get(name)
    return caller is None or caller.waits


@dataclass
class Front:
    """The front's actions, with what they reach replaced by the tests."""

    instance: Instance
    agent: AgentCall
    now: Callable[[], float] = time.time
    sleep: Callable[[float], None] = time.sleep

    def admit(self, caller: Caller, say: Callable[[str], None], *, replacing: bool) -> list[int]:
        """A place for the caller's stack, its ports reserved; waits or refuses
        when there is none, as the caller does.
        """
        deadline = self.now() + float(self.instance["queue_timeout_minutes"]) * 60
        said = 0.0
        while True:
            reason = self.try_admit(caller, replacing=replacing)
            if isinstance(reason, list):
                return reason
            if not caller.waits:
                raise RefusedError(reason, NO_ROOM)
            if self.now() >= deadline:
                raise RefusedError(f"{reason}, for {self.instance['queue_timeout_minutes']} minutes: go on without a stack", NO_ROOM)
            if self.now() - said >= SAY_WAITING_EVERY_S:
                say(f"Waiting for a place: {reason}.")
                said = self.now()
            self.sleep(WAIT_STEP_S)

    def try_admit(self, caller: Caller, *, replacing: bool) -> list[int] | str:
        """The caller's ports when there is a place, else why there is none."""
        if self.instance.paused():
            return "the instance is paused by its host"
        callers = self.instance.callers()
        with locked_state(self.instance) as state:
            now = self.now()
            others = {name: stack for name, stack in state.running().items() if name != caller.name}
            people = [name for name in others if not waits(callers, name)]
            people_max = int(self.instance["max_stacks"]) - int(self.instance.raw.get("waiting_reserve", 0))
            # The registered caller's, not the request's: the page starts a
            # runner's stack as a person, but it stays a runner's.
            if not waits(callers, caller.name) and not replacing and len(people) >= people_max:
                return f"{len(people)} of the {people_max} places for people's stacks are taken: {holders(state, now)}"
            if len(others) >= int(self.instance["max_stacks"]) and not replacing:
                idle_before = now - float(self.instance["evict_idle_minutes"]) * 60
                idle = [
                    name
                    for name, stack in others.items()
                    if stack.get("state") == "running" and stack.get("used_at", 0) < idle_before and waits(callers, name)
                ]
                if not idle:
                    return f"{len(others)} of {self.instance['max_stacks']} stacks are in use: {holders(state, now)}"
                victim = min(idle, key=lambda name: others[name].get("used_at", 0))
                self.finish(self.agent({"action": "stop", "caller": victim}, None))
                state.stacks[victim]["state"] = "stopped"
            ports = ports_of(self.instance, state, caller.name)
            state.stacks.setdefault(caller.name, {})["state"] = "starting"
            state.stacks[caller.name]["used_at"] = now
            return ports

    def finish(self, events: Events, say: Callable[[str], None] | None = None) -> dict[str, Any]:
        """The agent's last event, passing its output on to say if given."""
        for event in events:
            if "exit" in event:
                return event
            if say is not None and "out" in event:
                say(str(event["out"]))
        return {"exit": 1, "message": "the agent ended without an answer"}

    def touch(self, caller: str, **changes: object) -> None:
        """Record that the caller's stack was used, with any changes."""
        with locked_state(self.instance) as state:
            if caller in state.stacks:
                state.stacks[caller].update({"used_at": self.now(), **changes})

    def up(self, caller: Caller, request: dict[str, Any], bundle: str, say: Callable[[str], None]) -> dict[str, Any]:
        """Bring up the bundle as the caller's stack, replacing its last."""
        repo = request.get("repo")
        if repo not in caller.repos:
            raise RefusedError(f"refused: {caller.name} may bring up {', '.join(caller.repos) or 'nothing'}, not {repo}")
        profile = request.get("profile") or "default"
        if not (isinstance(profile, str) and NAME.fullmatch(profile)):
            raise RefusedError("refused: the profile is a name")
        # Only the request's repository's, which the caller may bring up;
        # read before a place is taken, which a refusal would leave starting.
        secrets = self.instance.secrets_for(repo)
        with locked_state(self.instance) as state:
            previous = state.stacks.get(caller.name)
            previous = dict(previous) if previous is not None else None
            replacing = (previous or {}).get("state") in ("running", "starting")
        ports = self.admit(caller, say, replacing=replacing)
        size = os.path.getsize(bundle)
        overrides = self.instance.raw.get("overrides") or {}
        agent_request = {
            "action": "up",
            "caller": caller.name,
            "repo": repo,
            "profile": profile,
            "ports": ports,
            "host": self.instance["host"],
            "override": overrides.get(repo) or {},
            "secrets": secrets,
            "bundle_size": size,
        } | self.https()
        try:
            result = self.finish(self.agent(agent_request, bundle), say)
        except UnreachableError:
            # The agent never had it: the last stack is as it was.
            self.restore(caller, previous)
            raise
        except OSError as error:
            result = {"exit": 1, "message": f"the agent failed: {error}"}
        now = self.now()
        # Refused before it replaced anything: the last stack is as it was.
        if result.get("kept") is True:
            self.restore(caller, previous)
            return {**result, "ports": ports, "host": self.instance["host"], **self.https()}
        with locked_state(self.instance) as state:
            state.stacks[caller.name] = {
                "repo": repo,
                "profile": profile,
                "commit": result.get("commit", ""),
                "page": result.get("page", 1),
                "state": "running" if result["exit"] == 0 else "failed",
                "up_at": now,
                "used_at": now,
            }
        page_url = f"http://{self.instance['host']}:{self.instance['page_port']}/"
        return {**result, "ports": ports, "host": self.instance["host"], "control": page_url, **self.https()}

    def restore(self, caller: Caller, previous: dict[str, Any] | None) -> None:
        """Put back the caller's last stack, as it was before an up took its place."""
        with locked_state(self.instance) as state:
            if previous is None:
                state.stacks.pop(caller.name, None)
            else:
                state.stacks[caller.name] = previous

    def https(self) -> dict[str, int]:
        """The caller's port the host serves the page on with HTTPS, by its
        number, where the host does.
        """
        https = self.instance.raw.get("https_port")
        return {"https_port": int(https)} if https else {}

    def existing(self, caller: Caller) -> dict[str, Any]:
        """The caller's stack, refused when it has none."""
        with locked_state(self.instance) as state:
            stack = state.stacks.get(caller.name)
        if stack is None or stack.get("state") == "starting":
            raise RefusedError("refused: no stack: up first")
        return stack

    def run(self, caller: Caller, request: dict[str, Any], say: Callable[[str], None]) -> dict[str, Any]:
        """Run one of the repository's suites against the caller's stack."""
        suite = request.get("suite")
        if not (isinstance(suite, str) and NAME.fullmatch(suite)):
            raise RefusedError("refused: the suite is a name")
        if self.instance.paused():
            raise RefusedError("the instance is paused by its host: go on without the suite", NO_ROOM)
        stack = self.existing(caller)
        if stack.get("state") != "running":
            raise RefusedError(f"refused: the stack is {stack.get('state')}: up again first")
        self.touch(caller.name)
        result = self.finish(self.agent({"action": "run", "caller": caller.name, "suite": suite}, None), say)
        self.touch(caller.name)
        return result

    def logs(self, caller: Caller, request: dict[str, Any], say: Callable[[str], None]) -> dict[str, Any]:
        """The tail of the caller's stack's logs."""
        service = request.get("service")
        if service is not None and not (isinstance(service, str) and SERVICE.fullmatch(service)):
            raise RefusedError("refused: the service is a name")
        self.existing(caller)
        self.touch(caller.name)
        agent_request = {"action": "logs", "caller": caller.name} | ({"service": service} if service is not None else {})
        return self.finish(self.agent(agent_request, None), say)

    def service(self, caller: Caller, action: str, request: dict[str, Any], say: Callable[[str], None]) -> dict[str, Any]:
        """Stop or start one service of the caller's running stack, to try
        its page without it; the stack stays running, the service named
        stopped until it is started again, the next up or the page's start.
        """
        service = request.get("service")
        if not (isinstance(service, str) and SERVICE.fullmatch(service)):
            raise RefusedError(f"refused: {action} wants a service's name")
        stack = self.existing(caller)
        if stack.get("state") != "running":
            raise RefusedError(f"refused: the stack is {stack.get('state')}: up again first")
        self.touch(caller.name)
        result = self.finish(self.agent({"action": action, "caller": caller.name, "service": service}, None), say)
        with locked_state(self.instance) as state:
            if result["exit"] == 0 and caller.name in state.stacks:
                stopped = set(state.stacks[caller.name].get("stopped_services") or [])
                stopped = stopped | {service} if action == "stop" else stopped - {service}
                state.stacks[caller.name]["stopped_services"] = sorted(stopped)
        return result

    def down(self, caller: Caller) -> dict[str, Any]:
        """Remove the caller's stack, its volumes and its tree; its ports stay its own."""
        result = self.finish(self.agent({"action": "down", "caller": caller.name}, None))
        with locked_state(self.instance) as state:
            state.stacks.pop(caller.name, None)
        return result

    def stacks(self) -> list[dict[str, Any]]:
        """Every stack of the instance, as the state and the agent see it."""
        live = self.finish(self.agent({"action": "ps"}, None)).get("stacks") or {}
        with locked_state(self.instance) as state:
            recorded = dict(state.stacks)
            ports = dict(state.ports)
        now = self.now()
        https = self.https().get("https_port")
        scheme = "https" if https is not None else "http"
        items = []
        for name in sorted(set(recorded) | set(live)):
            stack, seen = recorded.get(name, {}), live.get(name, {})
            state_now = seen.get("state", "none")
            if stack.get("state") in ("starting", "failed") and state_now != "running":
                state_now = stack["state"]
            # Every service stopped one by one: still the caller's, holding its place.
            if stack.get("state") == "running" and stack.get("stopped_services") and state_now == "stopped":
                state_now = "running"
            first = ports.get(name)
            page = first + int(stack.get("page", 1)) - 1 if first is not None else None
            if first is not None and https is not None:
                page = first + https - 1
            items.append({
                "caller": name,
                "repo": stack.get("repo", ""),
                "commit": stack.get("commit", ""),
                "state": state_now,
                "up_hours": round((now - stack["up_at"]) / 3600, 1) if "up_at" in stack else None,
                "idle_minutes": round((now - stack["used_at"]) / 60) if "used_at" in stack else None,
                "memory_mb": round(seen.get("memory", 0) / 1024**2),
                "url": f"{scheme}://{self.instance['host']}:{page}/" if page is not None else "",
                "stopped_services": (stack.get("stopped_services") or []) if state_now == "running" else [],
            })
        return items

    def page_start(self, name: str) -> tuple[int, str]:
        """Bring a stopped stack back, as a person: never waits for a place."""
        with locked_state(self.instance) as state:
            stack = state.stacks.get(name)
        if stack is None or stack.get("state") not in ("stopped", "running", "failed"):
            return 409, f"{name} has no stack to start."
        caller = Caller(name=name, key_sha256="", uid=None, repos=(), waits=False)
        try:
            self.admit(caller, lambda _: None, replacing=stack.get("state") == "running")
        except RefusedError as error:
            return 409, str(error)
        result = self.finish(self.agent({"action": "start", "caller": name}, None))
        self.touch(name, state="running" if result["exit"] == 0 else "failed", stopped_services=[])
        return (200, "Started.") if result["exit"] == 0 else (500, str(result.get("message", "Did not start.")))

    def page_stop(self, name: str) -> tuple[int, str]:
        """Stop a stack, keeping its tree and volumes."""
        with locked_state(self.instance) as state:
            if name not in state.stacks:
                return 404, f"No stack {name!r}."
        result = self.finish(self.agent({"action": "stop", "caller": name}, None))
        with locked_state(self.instance) as state:
            if name in state.stacks:
                state.stacks[name].update(state="stopped", stopped_services=[])
        return 200, f"Stopped {result.get('stopped', 0)} container(s)."

    def reap(self) -> list[str]:
        """Stop the stacks idle for idle_hours, remove those up for
        max_age_hours and those of callers no longer registered, and the
        images no container uses.
        """
        callers = self.instance.callers()
        now = self.now()
        idle_before = now - float(self.instance["idle_hours"]) * 3600
        old_before = now - float(self.instance["max_age_hours"]) * 3600
        live = self.finish(self.agent({"action": "ps"}, None)).get("stacks") or {}
        with locked_state(self.instance) as state:
            recorded = dict(state.stacks)
        done = []
        for name in sorted(set(recorded) | set(live)):
            stack = recorded.get(name, {})
            if name not in callers or name not in recorded or stack.get("up_at", 0) < old_before:
                # An up in progress; one past any up's time died with its front.
                if stack.get("state") == "starting" and stack.get("used_at", 0) > now - STARTING_MAX_S:
                    continue
                with caller_lock(self.instance, name):
                    self.finish(self.agent({"action": "down", "caller": name}, None))
                with locked_state(self.instance) as state:
                    state.stacks.pop(name, None)
                    if name not in callers:
                        state.ports.pop(name, None)
                done.append(f"removed {name}")
            elif live.get(name, {}).get("state") == "running" and stack.get("used_at", 0) < idle_before:
                self.finish(self.agent({"action": "stop", "caller": name}, None))
                with locked_state(self.instance) as state:
                    if name in state.stacks:
                        state.stacks[name]["state"] = "stopped"
                done.append(f"stopped {name}")
        with locked_state(self.instance) as state:
            for name in [name for name in state.ports if name not in callers]:
                del state.ports[name]
        self.finish(self.agent({"action": "prune"}, None))
        return done


# ---------------------------------------------------------------------
# The agent, over its socket
# ---------------------------------------------------------------------


def agent_over_socket(path: str) -> AgentCall:
    """A call to the agent through its socket."""

    def call(request: dict[str, Any], bundle: str | None) -> Events:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            try:
                conn.connect(path)
            except OSError as error:
                raise UnreachableError(f"the agent is out of reach: {error}") from error
            conn.sendall(json.dumps(request).encode() + b"\n")
            if bundle is not None:
                with open(bundle, "rb") as bundle_file:
                    while chunk := bundle_file.read(CHUNK):
                        conn.sendall(chunk)
            conn.shutdown(socket.SHUT_WR)
            with conn.makefile("rb") as answer:
                for line in answer:
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(event, dict):
                        yield event

    return call


# ---------------------------------------------------------------------
# One connection on the API socket
# ---------------------------------------------------------------------


def peer_uid(conn: socket.socket) -> int:
    """The uid of the process at the other end of the socket."""
    creds = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    return struct.unpack("3i", creds)[1]


def read_header(conn: IO[bytes]) -> dict[str, Any]:
    """The request's first line, a JSON object."""
    line = conn.readline(MAX_HEADER)
    try:
        request = json.loads(line or b"null")
    except ValueError:
        request = None
    if not isinstance(request, dict):
        raise RefusedError("refused: the request is one line, a JSON object")
    return request


def spool_bundle(instance: Instance, conn: IO[bytes], size: object) -> str:
    """The bundle's bytes into a file of the front's, unread, before any wait."""
    limit = int(instance["bundle_max_mb"]) * 1024**2
    if isinstance(size, bool) or not isinstance(size, int) or not 0 < size <= limit:
        raise RefusedError(f"refused: up wants bundle_size, at most {instance['bundle_max_mb']} MB")
    spool = os.path.join(instance["state_dir"], "spool")
    os.makedirs(spool, mode=0o700, exist_ok=True)
    path = os.path.join(spool, f"{os.getpid()}.bundle")
    try:
        with open(path, "wb") as bundle:
            copy_exactly(conn, bundle, size)
    except BaseException:
        # Ended early, or a remote caller's read timed out: nothing is left.
        os.unlink(path)
        raise
    return path


def copy_exactly(conn: IO[bytes], bundle: IO[bytes], size: int) -> None:
    """Copy exactly size bytes from conn into bundle, refused when conn ends first."""
    remaining = size
    while remaining:
        chunk = conn.read(min(CHUNK, remaining))
        if not chunk:
            raise RefusedError("refused: the bundle ended early")
        bundle.write(chunk)
        remaining -= len(chunk)


def answer(out: IO[bytes], event: dict[str, Any]) -> bool:
    """One line of the answer, flushed so the caller sees it as it comes;
    False when the caller is gone: a closed pipe, a TLS connection reset or
    a remote caller that stopped reading.
    """
    try:
        out.write(json.dumps(event).encode() + b"\n")
        out.flush()
    except OSError:
        return False
    return True


class Reply:
    """The answer's lines, until the caller is gone: after a write that
    fails nothing more is written, and the request goes on, so the agent
    is never left mid-up and a remote caller that stopped reading costs no
    timeout per line.
    """

    def __init__(self, out: IO[bytes]) -> None:
        """Answer on out."""
        self.out = out
        self.gone = False

    def __call__(self, event: dict[str, Any]) -> None:
        """One line of the answer, if the caller is still there, with the
        request's id, whatever an event of the agent's said.
        """
        self.gone = self.gone or not answer(self.out, {**event, "request": REQUEST_ID.get()})


def with_request_id(agent: AgentCall, request_id: str) -> AgentCall:
    """The agent's calls, each request carrying the id the front gave it."""

    def call(request: dict[str, Any], bundle: str | None) -> Events:
        return agent({**request, "request": request_id}, bundle)

    return call


def on_stack(front: Front, caller: Caller, action: str, request: dict[str, Any], say: Callable[[str], None]) -> dict[str, Any]:
    """An action on the caller's stack, its own lock held."""
    if action == "run":
        return front.run(caller, request, say)
    if action == "logs":
        return front.logs(caller, request, say)
    if action in ("stop", "start"):
        return front.service(caller, action, request, say)
    return front.down(caller)


def serve(front: Front, conn: IO[bytes], out: IO[bytes], uid: int | None, address: str | None = None) -> int:
    """Read one request, carry it out and answer it; returns 0 once it has
    answered, whatever the request's status, and 1 when it could not: the
    agent out of reach or the caller gone after it asked. uid is a local peer's, address
    a remote one's. The id is REQUEST_ID's where it is set, a new one
    otherwise, and never the request's.
    """
    token = REQUEST_ID.set(REQUEST_ID.get() or new_request_id())
    try:
        return serve_request(replace(front, agent=with_request_id(front.agent, REQUEST_ID.get())), conn, out, uid, address)
    finally:
        REQUEST_ID.reset(token)


def serve_request(front: Front, conn: IO[bytes], out: IO[bytes], uid: int | None, address: str | None) -> int:
    """The request serve reads, under its id."""
    reply = Reply(out)
    broken = False
    read = False

    def say(text: str) -> None:
        reply({"out": text})

    bundle = None
    result: dict[str, Any]
    action: object = None
    caller: Caller | None = None
    try:
        request = read_header(conn)
        read = True
        action = request.get("action")
        caller = authenticate(front.instance, request.get("key"), uid, address)
        if action not in ACTIONS:
            result = {"exit": 1, "message": f"refused: the action is one of {', '.join(ACTIONS)}"}
        elif action == "list":
            result = {"exit": 0, "stacks": front.stacks(), "caller": caller.name}
        elif action == "up":
            with caller_lock(front.instance, caller.name, wait=False):
                bundle = spool_bundle(front.instance, conn, request.get("bundle_size"))
                result = front.up(caller, request, bundle, say)
        else:
            with caller_lock(front.instance, caller.name, wait=False):
                result = on_stack(front, caller, str(action), request, say)
    except RefusedError as error:
        result = {"exit": error.status, "message": str(error)}
    except UnreachableError as error:
        broken = True
        result = {"exit": 1, "message": f"the instance failed: {error}"}
    except OSError as error:
        result = {"exit": 1, "message": f"the instance failed: {error}" if read else f"no request came: {error}"}
    finally:
        if bundle is not None and os.path.exists(bundle):
            os.unlink(bundle)
    reply(result)
    log_failure(caller, action, address or f"uid {uid}", result)
    if reply.gone and read:
        journal("the answer could not be written: the caller is gone")
    # A caller gone before it asked anything, one whose pin did not match,
    # is the peer's doing, as a peer with no TLS is: nothing broke.
    return 1 if broken or (reply.gone and read) else 0


def tls_context(instance: Instance) -> ssl.SSLContext:
    """The TLS port's side: the instance's certificate, TLS 1.2 at least."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(instance["tls_cert"], instance["tls_key"])
    return context


def serve_tls(front: Front, raw: socket.socket, context: ssl.SSLContext, address: str) -> int:
    """One connection on the TLS port, from address: the handshake and
    every read bounded by the instance's remote timeout, then served as
    the socket's.
    """
    raw.settimeout(front.instance.remote_timeout_s())
    try:
        conn = context.wrap_socket(raw, server_side=True)
    except (ssl.SSLError, OSError) as error:
        # The peer's doing, a scan or a client with no TLS: nothing broke.
        log_line("-", address, 1, f"no TLS: {error}")
        return 0
    with conn:
        with conn.makefile("rb") as reader:
            writer = conn.makefile("wb")
            try:
                status = serve(front, reader, writer, None, address)
            finally:
                # What a caller that is gone left unflushed.
                with contextlib.suppress(OSError):
                    writer.close()
        # close_notify, so the caller reads the answer to its end.
        with contextlib.suppress(OSError):
            conn.unwrap()
    return status


def log_failure(caller: Caller | None, action: object, peer: str, result: dict[str, Any]) -> None:
    """One line to the journal for a request refused or failed: its caller,
    or the uid or address that presented no caller's key, its action, its status and
    what the caller was told, which is all it holds. Nothing for one that
    did what it was asked.
    """
    if result["exit"] == 0:
        return
    who = caller.name if caller is not None else peer
    log_line(action if isinstance(action, str) and action in ACTIONS else "-", who, result["exit"], result.get("message", ""))


def log_line(action: str, who: str, status: object, message: object) -> None:
    """One line to the journal: the message quoted, so it stays one line."""
    journal(f"{action} for {who}: exit {status}: {json.dumps(str(message))}")


# ---------------------------------------------------------------------
# The phone page
# ---------------------------------------------------------------------

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Stacks</title>
<style>
:root { color-scheme: light dark; --bg: #fafafa; --fg: #1a1a1a; --card: #fff; --muted: #666;
        --line: #ddd; --ok: #1a7f37; --off: #888; --busy: #9a6700; --btn: #0969da; }
@media (prefers-color-scheme: dark) {
  :root { --bg: #111; --fg: #eee; --card: #1c1c1c; --muted: #999; --line: #333;
          --ok: #3fb950; --off: #777; --busy: #d29922; --btn: #4493f8; } }
body { margin: 0; padding: 16px; background: var(--bg); color: var(--fg);
       font: 16px/1.4 system-ui, sans-serif; }
h1 { font-size: 20px; margin: 0 0 12px; }
.stack { background: var(--card); border: 1px solid var(--line); border-radius: 10px;
         padding: 12px; margin-bottom: 10px; }
.top { display: flex; justify-content: space-between; align-items: center; gap: 8px; }
.id { font-weight: 600; word-break: break-all; }
.state { font-size: 14px; }
.running { color: var(--ok); } .stopped, .none, .failed { color: var(--off); } .starting { color: var(--busy); }
.meta { color: var(--muted); font-size: 14px; margin-top: 4px; word-break: break-all; }
a { color: var(--btn); }
button { font: inherit; padding: 8px 16px; border-radius: 8px; border: 1px solid var(--btn);
         background: none; color: var(--btn); min-width: 76px; }
#msg { min-height: 1.4em; color: var(--muted); font-size: 14px; }
</style></head><body>
<h1>Stacks</h1>
<div id="msg"></div>
<div id="list">Loading…</div>
<script>
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
async function act(id, what) {
  document.getElementById("msg").textContent = "…";
  const r = await fetch(`/stacks/${encodeURIComponent(id)}/${what}`, {method: "POST", headers: {"X-Executor": "1"}});
  document.getElementById("msg").textContent = `${id}: ${(await r.json()).message}`;
  load();
}
function card(s) {
  const button = s.state === "running"
    ? `<button data-id="${esc(s.caller)}" data-act="stop">Stop</button>`
    : ["stopped", "failed"].includes(s.state) ? `<button data-id="${esc(s.caller)}" data-act="start">Start</button>` : "";
  const age = s.up_hours === null ? "" : ` · up ${esc(s.up_hours)} h ago, used ${esc(s.idle_minutes)} min ago`;
  const held = (s.stopped_services || []).length ? ` · stopped: ${s.stopped_services.map(esc).join(", ")}` : "";
  return `<div class="stack"><div class="top">
      <div><div class="id">${esc(s.caller)}</div>
        <div class="state ${esc(s.state)}">${esc(s.state)}${held}${age}</div></div>
      <div>${button}</div></div>
    <div class="meta">${esc(s.repo)} ${esc(String(s.commit).slice(0, 7))} · ${esc(s.memory_mb)} MB${s.state === "running" && s.url
      ? ` · <a href="${esc(s.url)}">${esc(s.url)}</a>` : ""}</div></div>`;
}
async function load() {
  const all = await (await fetch("/stacks")).json();
  document.getElementById("list").innerHTML = all.length ? all.map(card).join("") : `<p class="meta">No stacks.</p>`;
}
document.addEventListener("click", e => { const b = e.target.closest("button[data-act]"); if (b) act(b.dataset.id, b.dataset.act); });
load(); setInterval(load, 5000);
</script></body></html>
"""


def page_host(instance: Instance, host_header: str | None) -> bool:
    """Whether a request names the page by its own host and port: a site
    that rebinds its name to this address does not.
    """
    return host_header == f"{instance['host']}:{instance['page_port']}"


def page_post(front: Front, path: str, header_value: str | None) -> tuple[int, str]:
    """A button's POST: stop or start one stack."""
    if header_value != "1":
        return 403, f"Needs the {PAGE_HEADER} header."
    parts = path.strip("/").split("/")
    if len(parts) != PAGE_PATH_PARTS or parts[0] != "stacks" or parts[2] not in ("stop", "start") or not CALLER.fullmatch(parts[1]):
        return 404, "Not found."
    name, action = parts[1], parts[2]
    with caller_lock(front.instance, name):
        code, text = front.page_stop(name) if action == "stop" else front.page_start(name)
    if code >= HTTPStatus.BAD_REQUEST:
        log_line(f"page {action}", name, code, text)
    return code, text


def page_get(front: Front, path: str) -> tuple[int, bytes, str]:
    """The page itself, or its list of stacks."""
    if path == "/":
        return 200, PAGE.encode(), "text/html; charset=utf-8"
    if path == "/stacks":
        return 200, json.dumps(front.stacks()).encode(), "application/json"
    return 404, b'{"message": "Not found."}', "application/json"


def page_request(front: Front, method: str, path: str, headers: dict[str, str]) -> tuple[int, bytes, str]:
    """The answer to one request of the page: code, body and its type;
    headers by their names in lower case.

    Every request must name the instance's host and the page's port, so a
    site that rebinds its own name to this address gets nothing. A POST
    must also carry the X-Executor header: a page on another origin, a
    stack's own among them, cannot send it without a CORS preflight this
    server never answers, so it cannot press the buttons for you.
    """
    if not page_host(front.instance, headers.get("host")):
        return 421, b'{"message": "Open the page by the instance\'s own name."}', "application/json"
    if method == "GET":
        return page_get(front, path)
    try:
        code, message = page_post(front, path, headers.get(PAGE_HEADER.lower()))
    except OSError as error:
        code, message = 500, f"Failed: {error}"
    return code, json.dumps({"message": message}).encode(), "application/json"


def page_handler(front: Front) -> type[BaseHTTPRequestHandler]:
    """The request handler class serving the page and its buttons, as page_request answers."""

    class Handler(BaseHTTPRequestHandler):
        def send(self, code: int, body: bytes, kind: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            self.send(*page_request(front, "GET", self.path, {key.lower(): value for key, value in self.headers.items()}))

        def do_POST(self) -> None:
            self.send(*page_request(front, "POST", self.path, {key.lower(): value for key, value in self.headers.items()}))

        @override
        def log_message(self, format: str, *args: object) -> None:
            # One line per button for the journal; the page's polling is noise.
            if self.command == "POST":
                sys.stderr.write(f"POST {self.path} from {self.client_address[0]}\n")

    return Handler


# ---------------------------------------------------------------------
# The script
# ---------------------------------------------------------------------


def load_instance(path: str) -> Instance:
    """The instance's config file, written by the play."""
    with open(path) as config_file:
        return Instance(json.load(config_file))


def main(argv: list[str]) -> int:
    """Run one of the front's ways in; returns the exit status."""
    usage = "usage: executor-front serve|serve-tls|page|reap|check <instance.json>"
    if len(argv) != 2 or argv[0] not in ("serve", "serve-tls", "page", "reap", "check"):  # noqa: PLR2004 -- the way in and the config
        print(usage, file=sys.stderr)
        return 2
    os.umask(0o077)
    instance = load_instance(argv[1])
    front = Front(instance, agent_over_socket(instance["agent_socket"]))
    if argv[0] == "serve":
        with socket.fromfd(sys.stdin.fileno(), socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            uid = peer_uid(conn)
        return serve(front, sys.stdin.buffer, sys.stdout.buffer, uid)
    if argv[0] == "serve-tls":
        # The family and the peer from the socket systemd accepted.
        with socket.socket(fileno=os.dup(sys.stdin.fileno())) as raw:
            return serve_tls(front, raw, tls_context(instance), str(raw.getpeername()[0]))
    if argv[0] == "reap":
        for line in front.reap():
            print(line)
        return 0
    if argv[0] == "check":
        result = front.finish(front.agent({"action": "ps"}, None))
        print(json.dumps(result))
        return int(result["exit"])
    server = ThreadingHTTPServer(("", int(instance["page_port"])), page_handler(front))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"Serving the page of {instance['name']} on port {instance['page_port']}", file=sys.stderr)
    threading.Event().wait()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
