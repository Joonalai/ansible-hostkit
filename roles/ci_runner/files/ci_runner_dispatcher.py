#!/usr/bin/python3 -I
"""Start a CI runner on this host for each job GitHub has queued for it.

Runs as root from its service. Each poll:

1. Every repository in the config that is not private loses its runners: a
   pull request in a public repository could otherwise ask for this host by
   its label. This pass comes before anything starts.
2. Nothing starts while CI is paused, or while the host's sandbox for the CI
   account is not held: its unit is not active, or the host's own check of
   it fails. Without the sandbox the running runners are stopped too.
3. For each private repository and each pool, the jobs queued for the pool's
   label are counted, less the runners of that repository and pool that are
   up and not yet busy, and a runner is started for each one left while a
   slot is free. A runner that has waited for a job longer than IDLE_MAX is
   stopped.

Where each job's time went is written to timing_dir, a JSON line per job,
per runner stopped unused and per poll sample (Timing). Only the records
read it back.

A poll comes every poll_seconds, counted from the start of the last one, and
sooner when a runner's container is gone, so its slot is given again at once.

Two pools keep pull requests apart from main: a job of a pull request asks
for the label with "-pr" added, and its runner gets the stores of the "pr"
pool. So a pull request cannot leave anything in the caches that a job on
main then trusts.

A runner is ephemeral: GitHub gives it one job, and its container ends with
the job. It runs in the CI account's own systemd manager, as a transient unit
named after its pool and slot, so the account's limits hold it and systemd
refuses a second runner in the same slot. The token that registers runners
stays here, and the runner gets only its one-time configuration.

The script is installed as <name>-dispatcher and reads
/etc/<name>/dispatcher.json. The name is the consumer's, and every unit,
container, volume and label of the runners begins with it.

    <name>-dispatcher run      poll until stopped
    <name>-dispatcher poll     poll once
    <name>-dispatcher pause    start no runner until resumed, also after a
                               reboot or a play. Running jobs finish
    <name>-dispatcher resume
    <name>-dispatcher stop     stop every runner now and remove them
    <name>-dispatcher warm     pull the jobs' images once and write them to
                               the shared image files
    <name>-dispatcher enable   set each private repository's variable, so
                               its jobs come here
    <name>-dispatcher disable  remove the variables, so the jobs go back to
                               GitHub's runners
    <name>-dispatcher remove   disable, then stop every runner and remove
                               its registration. The first step of taking
                               CI off this host

Standard library only, so it runs on the system python3. The config is root's,
written by the play: nothing GitHub sends names a command, a path or an
option. The token never reaches an argument list.
"""

import json
import os
import pwd
import re
import secrets
import shutil
import stat
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from pathlib import Path
from typing import Any, cast

# The config as the play wrote it, and an object in an answer of GitHub's.
type Config = dict[str, Any]
type JsonObject = dict[str, Any]

# What every unit, container, volume and label of the runners begins with.
# main() sets it from the name the script is installed under.
NAME = "ci-runner"
NAME_RE = re.compile(r"^[a-z][a-z0-9-]*$")
SUFFIX = "-dispatcher"
ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
API = "https://api.github.com"
REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
LABEL_RE = re.compile(r"^[a-z0-9-]+$")
RUNUSER = "/usr/sbin/runuser"
SYSTEMD_RUN = "/usr/bin/systemd-run"
SYSTEMCTL = "/usr/bin/systemctl"
TOKEN_WARN = timedelta(days=14)
# A runner that has not taken a job this long after it started is stopped:
# GitHub gave the job to another runner, or it was cancelled.
IDLE_MAX = timedelta(minutes=10)
# Sooner when more runners wait than jobs are queued for them, as after a push
# that cancelled the jobs they were started for.
SURPLUS_AFTER = timedelta(minutes=3)
# A slot given out is not given again this long, even while its container is
# not listed yet.
CLAIM_HOLD = timedelta(seconds=90)
# Each call to GitHub. Over a busy connection, calls allowed 30 s held a poll
# for minutes.
API_TIMEOUT = 15
# Calls to GitHub at once. One after another over a slow connection a poll
# takes most of a minute, and a queued job waits for it.
API_PARALLEL = 8
# Below this many calls left in the hour, polls slow down (run()).
RATE_LOW = 500
# Ended jobs looked up for their timing records in one poll, at most.
LOOK_UPS = 20
# A call to GitHub slower than this is logged, so a poll held up by one call
# names it.
SLOW_CALL = 5
# The least time between polls, also when a container's end wakes the next one
# early (ExitWatcher).
MIN_PAUSE = 2
# A runner's registration exists before its container is listed. One younger
# than this is never taken for a leftover.
REGISTRATION_GRACE = timedelta(minutes=5)
# The pools: a suffix of the label, and the name of the pool's stores.
POOLS = {"main": "", "pr": "-pr"}
# The most slots a config may ask for.
MAX_SLOTS = 8
# The calls that can fail in a poll without stopping the others.
POLL_ERRORS = (
    urllib.error.URLError,
    TimeoutError,
    OSError,
    ValueError,
    KeyError,
    subprocess.SubprocessError,
)


def runner_label() -> str:
    """What the dispatcher's containers carry, so it finds them again."""
    return f"{NAME}.runner"


def log(message: str) -> None:
    """One line to the service's log, written at once."""
    print(message, flush=True)


class GitHub:
    """GitHub's API for the repositories' runners, with the token that registers them."""

    def __init__(self, token: str) -> None:
        """Nothing is asked yet: the cache is empty and the token's expiry unknown."""
        self.token = token
        self.token_expiry: str | None = None
        # GET answers by URL, with their ETag. A 304 to a conditional request
        # does not count against the rate limit.
        self.cache: dict[str, tuple[str, object]] = {}
        self.used: set[str] = set()
        self.rate_remaining: int | None = None

    def forget_unused(self) -> None:
        """Keep the cached answers of the last poll only. Their URLs name runs,
        so the cache would otherwise grow for as long as the service runs.
        """
        self.cache = {url: cached for url, cached in self.cache.items() if url in self.used}
        self.used = set()

    def call(self, method: str, path: str, body: dict[str, object] | None = None, **query: str | int) -> object:
        """One call to the API, its answer as parsed JSON. A call that fails or
        is slow is logged with how long it took.
        """
        url = f"{API}{path}"
        if query:
            url += "?" + urllib.parse.urlencode(query)
        data = json.dumps(body).encode() if body is not None else None
        # Always https: the URL begins with API.
        request = urllib.request.Request(url, data=data, method=method)  # noqa: S310
        request.add_header("Authorization", f"Bearer {self.token}")
        request.add_header("Accept", "application/vnd.github+json")
        request.add_header("X-GitHub-Api-Version", "2022-11-28")
        request.add_header("User-Agent", "ci-runner-dispatcher")
        if data is not None:
            request.add_header("Content-Type", "application/json")
        cached = self.cache.get(url) if method == "GET" else None
        if method == "GET":
            self.used.add(url)
        if cached:
            request.add_header("If-None-Match", cached[0])
        began = time.monotonic()
        try:
            answer = self.answer(request, method, url, cached)
        except urllib.error.HTTPError:
            raise
        except (urllib.error.URLError, TimeoutError) as error:
            log(f"GitHub {method} {path} failed after {time.monotonic() - began:.0f} s: {error}")
            raise
        took = time.monotonic() - began
        if took > SLOW_CALL:
            log(f"GitHub {method} {path} took {took:.0f} s")
        return answer

    def answer(self, request: urllib.request.Request, method: str, url: str, cached: tuple[str, object] | None) -> object:
        """Send the request and read its answer, noting the token's expiry and
        the calls left this hour. A 304 gives the cached answer.
        """
        try:
            # The request is call()'s, to API.
            with urllib.request.urlopen(request, timeout=API_TIMEOUT) as response:  # noqa: S310
                expiry = response.headers.get("github-authentication-token-expiration")
                if expiry:
                    self.token_expiry = expiry
                remaining = response.headers.get("x-ratelimit-remaining")
                if remaining and remaining.isdigit():
                    self.rate_remaining = int(remaining)
                raw = response.read()
                answer = json.loads(raw) if raw else None
                etag = response.headers.get("ETag")
                if method == "GET" and etag:
                    self.cache[url] = (etag, answer)
                return answer
        except urllib.error.HTTPError as error:
            if error.code == HTTPStatus.NOT_MODIFIED and cached:
                return cached[1]
            raise

    def get(self, path: str, **query: str | int) -> JsonObject:
        """A GET of one of the paths used here, which all answer with an object."""
        return cast("JsonObject", self.call("GET", path, None, **query))

    def is_private(self, repo: str) -> bool:
        """Whether GitHub says the repository is private."""
        # Anything but a plain true is public to this host.
        return self.get(f"/repos/{repo}").get("private") is True

    def jobs(self, repo: str, labels: list[str]) -> list[JsonObject]:
        """The jobs of the repository's queued and running workflow runs that
        ask for one of the labels, whatever their own status.
        """
        run_ids = []
        for status in ("queued", "in_progress"):
            runs = self.get(f"/repos/{repo}/actions/runs", status=status, per_page=50)
            run_ids += [run["id"] for run in runs.get("workflow_runs", [])]

        def jobs_of(run_id: int) -> JsonObject:
            return self.get(f"/repos/{repo}/actions/runs/{run_id}/jobs", filter="latest", per_page=100)

        wanted = set(labels)
        found = []
        with ThreadPoolExecutor(API_PARALLEL) as pool:
            for jobs in pool.map(jobs_of, run_ids):
                found += [job for job in jobs.get("jobs", []) if wanted & set(job.get("labels", []))]
        return found

    def job(self, repo: str, job_id: int) -> JsonObject:
        """One job, as GitHub has it now."""
        return self.get(f"/repos/{repo}/actions/jobs/{job_id}")

    def runners(self, repo: str, labels: list[str]) -> list[JsonObject]:
        """The repository's self-hosted runners that carry one of the labels."""
        found = self.get(f"/repos/{repo}/actions/runners", per_page=100)
        wanted = set(labels)
        return [
            entry
            for entry in found.get("runners", [])
            if wanted & {item.get("name") for item in entry.get("labels", [])}
        ]

    def remove_runner(self, repo: str, runner_id: int) -> None:
        """Remove a runner's registration from the repository."""
        self.call("DELETE", f"/repos/{repo}/actions/runners/{runner_id}")

    def set_variable(self, repo: str, name: str, value: str) -> None:
        """Create or update a repository's Actions variable."""
        try:
            self.call("PATCH", f"/repos/{repo}/actions/variables/{name}", {"name": name, "value": value})
        except urllib.error.HTTPError as error:
            if error.code != HTTPStatus.NOT_FOUND:
                raise
            self.call("POST", f"/repos/{repo}/actions/variables", {"name": name, "value": value})

    def delete_variable(self, repo: str, name: str) -> bool:
        """Remove a repository's Actions variable. False when there was none."""
        try:
            self.call("DELETE", f"/repos/{repo}/actions/variables/{name}")
        except urllib.error.HTTPError as error:
            if error.code == HTTPStatus.NOT_FOUND:
                return False
            raise
        return True

    def jit_config(self, repo: str, name: str, label: str) -> str:
        """Register a runner for one job and give its one-time configuration."""
        answer = cast(
            "dict[str, str]",
            self.call(
                "POST",
                f"/repos/{repo}/actions/runners/generate-jitconfig",
                {"name": name, "runner_group_id": 1, "labels": [label], "work_folder": "_work"},
            ),
        )
        return answer["encoded_jit_config"]


@dataclass(frozen=True)
class Runner:
    """A container in one of the slots: a runner, or the play warming it."""

    container: str
    name: str
    repo: str
    pool: str
    slot: int
    started: datetime


def slot_unit(pool: str, slot: int) -> str:
    """The name of a slot's unit and container. One of each at a time."""
    return f"{NAME}-{pool}-{slot}"


class Account:
    """Runs podman and systemd-run as the CI account, never as root."""

    def __init__(self, user: str) -> None:
        """Look the account up, which fails when the host has none of that name."""
        self.user = user
        entry = pwd.getpwnam(user)
        self.uid = entry.pw_uid
        self.home = entry.pw_dir

    def env(self) -> dict[str, str]:
        """The environment of the account's commands, with its own runtime directory and bus."""
        runtime = f"/run/user/{self.uid}"
        return {
            "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "HOME": self.home,
            "XDG_RUNTIME_DIR": runtime,
            "DBUS_SESSION_BUS_ADDRESS": f"unix:path={runtime}/bus",
        }

    def podman(self, *args: str, check: bool = True, timeout: int = 120) -> str:
        """Run podman as the account and give what it printed. A failure is
        logged with podman's reason before it is raised.
        """
        result = subprocess.run(
            [RUNUSER, "-u", self.user, "--", "podman", *args],
            env=self.env(),
            cwd="/",
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        if check and result.returncode != 0:
            # The error says only the exit status, podman's reason is here.
            log(f"podman {' '.join(args[:2])} failed: {result.stderr.strip()}")
            result.check_returncode()
        return result.stdout

    def runners(self) -> list[Runner]:
        """The containers in the slots, running or not."""
        # --all: a container still being created or stopping holds its slot.
        listed = json.loads(
            self.podman("ps", "--all", "--filter", f"label={runner_label()}", "--format", "json") or "[]",
        )
        found = []
        for item in listed:
            labels = item.get("Labels") or {}
            try:
                # A container not started yet has a zero or negative StartedAt,
                # which is not its age.
                started = max(int(item.get("StartedAt") or 0), int(item.get("Created") or 0))
                found.append(
                    Runner(
                        container=item["Names"][0],
                        name=labels.get(f"{NAME}.name", ""),
                        repo=labels.get(f"{NAME}.repo", ""),
                        pool=labels[f"{NAME}.pool"],
                        slot=int(labels[f"{NAME}.slot"]),
                        started=datetime.fromtimestamp(started, UTC),
                    ),
                )
            except (KeyError, ValueError, IndexError):
                log(f"Ignoring a container without the dispatcher's labels: {item.get('Names')}")
        return found

    def start(self, command: list[str], unit: str, max_seconds: int) -> None:
        """Start a command as a transient unit of the account's own manager.
        systemd refuses a unit name that is already active.
        """
        subprocess.run(
            [
                SYSTEMD_RUN,
                f"--machine={self.user}@.host",
                "--user",
                "--collect",
                "--quiet",
                f"--unit={unit}",
                f"--property=RuntimeMaxSec={max_seconds}",
                "--",
                *command,
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def stop(self, container: str) -> None:
        """Stop a container, which gets half a minute to end by itself."""
        self.podman("stop", "--time", "30", container, check=False)


class ExitWatcher:
    """Wakes the next poll as soon as a runner's container is gone, so its
    slot is given again within seconds rather than at the next poll. Polls
    still come on time without it: a watcher that fails only logs and tries
    again.
    """

    RETRY = 10

    def __init__(self, command: list[str], env: dict[str, str] | None = None) -> None:
        """Watch with the command, which prints a line for each container gone."""
        self.command = command
        self.env = env
        self.wake = threading.Event()

    def start(self) -> None:
        """Watch in a thread of its own, which ends with the process."""
        threading.Thread(target=self.run, name="exit-watcher", daemon=True).start()

    def run(self) -> None:
        """Watch for good, again after a pause whenever the watching ends."""
        while True:
            self.watch()
            time.sleep(self.RETRY)

    def watch(self) -> None:
        """Wake the poll at each line of the command, until the command ends."""
        try:
            with subprocess.Popen(
                self.command, env=self.env, cwd="/", stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, text=True,
            ) as events:
                # Always there with stdout=PIPE: said for the type checker.
                assert events.stdout is not None  # noqa: S101
                for _ in events.stdout:
                    self.wake.set()
            log(f"podman events ended with {events.returncode}, watching again in {self.RETRY} s")
        except OSError as error:
            log(f"could not watch for runners ending: {error}")

    def sleep(self, seconds: float) -> None:
        """Until the pause is over or a container is gone."""
        self.wake.wait(seconds)


def exit_watcher(account: "Account") -> ExitWatcher:
    """Each of the account's runner containers removed, which with --rm is
    when its job and its tidying are over.
    """
    return ExitWatcher(
        [RUNUSER, "-u", account.user, "--", "podman", "events",
         "--filter", f"label={runner_label()}=1", "--filter", "event=remove", "--format", "{{.Name}}"],
        env=account.env(),
    )


def pause_after(took: float, poll_seconds: float, rate_remaining: int | None) -> float:
    """The pause before the next poll. Counted from the start of the last one,
    so a slow poll is not followed by a full pause too. A minute while few
    calls to GitHub are left this hour.
    """
    if rate_remaining is not None and rate_remaining < RATE_LOW:
        return 60
    return max(MIN_PAUSE, poll_seconds - took)


def sandbox_held(config: Config) -> bool:
    """Whether the host still holds the account in its sandbox. The unit the
    host named must be active, and since a firewall's rules can be flushed
    while its unit still reads active, the host may also give a command of
    its own that looks at the rules themselves. Anything but a clean answer
    counts as not held.
    """
    checks = [[SYSTEMCTL, "is-active", "--quiet", config["sandbox_unit"]]]
    if config.get("sandbox_check"):
        checks.append(list(config["sandbox_check"]))
    for command in checks:
        try:
            result = subprocess.run(command, cwd="/", capture_output=True, timeout=30, check=False)
        except (OSError, subprocess.SubprocessError):
            return False
        if result.returncode != 0:
            return False
    return True


def free_slots(slots: int, running: list[Runner], claimed: set[int]) -> list[int]:
    """Slots are shared by the pools: there are as many jobs at once as slots."""
    used = {runner.slot for runner in running} | claimed
    return [slot for slot in range(1, slots + 1) if slot not in used]


def free_runners(repo: str, pool: str, running: list[Runner], registered: dict[str, JsonObject]) -> list[Runner]:
    """Runners of the repository and pool that may still take a queued job:
    registered and not busy, oldest first. One whose registration is gone has
    ended its job and is tidying its slot, and is left to it.
    """
    found = [
        runner
        for runner in running
        if runner.repo == repo and runner.pool == pool and runner.name in registered
        and not registered[runner.name].get("busy")
    ]
    return sorted(found, key=lambda runner: runner.started)


def to_stop(free: list[Runner], queued: int, now: datetime) -> list[Runner]:
    """Free runners that waited IDLE_MAX, and those beyond the queued jobs
    that waited SURPLUS_AFTER, oldest first.
    """
    stop = [runner for runner in free if now - runner.started >= IDLE_MAX]
    rest = [runner for runner in free if runner not in stop]
    surplus = len(rest) - queued
    for runner in rest:
        if surplus <= 0:
            break
        if now - runner.started >= SURPLUS_AFTER:
            stop.append(runner)
            surplus -= 1
    return stop


def queued_counts(jobs: list[JsonObject], labels: list[str]) -> dict[str, int]:
    """Jobs waiting for a runner, by label."""
    counts = dict.fromkeys(labels, 0)
    for job in jobs:
        if job.get("status") != "queued":
            continue
        for label in labels:
            if label in job.get("labels", []):
                counts[label] += 1
    return counts


def to_start(queued: int, waiting_runners: int) -> int:
    """Runners to start: one for each queued job no waiting runner will take."""
    return max(0, queued - waiting_runners)


def leftovers(registered: list[JsonObject], running: list[Runner], recent: dict[str, datetime],
              now: datetime) -> list[JsonObject]:
    """Registered runners whose container is gone: they never take a job. A
    registration made in the last few minutes may not have its container yet.
    """
    names = {runner.name for runner in running}
    return [
        entry
        for entry in registered
        if entry["name"] not in names
        and entry.get("status") == "offline"
        and now - recent.get(entry["name"], datetime.min.replace(tzinfo=UTC)) > REGISTRATION_GRACE
    ]


def runner_name(repo: str, pool: str) -> str:
    """A new runner's name: its repository and pool, and random hex so no two are alike."""
    short = re.sub(r"[^a-z0-9-]", "-", repo.split("/")[1].lower())
    return f"{NAME}-{short}-{pool}-{secrets.token_hex(4)}"


# Where a container finds the shared image files (ci-runner-warm). A path
# inside the runner image, the same whatever the host calls its runners.
IMAGES_MOUNT = "/ci-runner-images"
IMAGE_FILE = re.compile(r"^[0-9a-f]{64}\.tar$")
IMAGE_REF = re.compile(r"^[a-z0-9][a-z0-9._/-]*(:[A-Za-z0-9._-]+)?(@sha256:[0-9a-f]{64})?$")


def images_container() -> str:
    """The container that pulls the jobs' images. Not in a slot."""
    return f"{NAME}-images"


def copy_regular(source: Path, target: Path) -> None:
    """Copy a file the account wrote, never what a link of its points at."""
    fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as src:
        if not stat.S_ISREG(os.fstat(src.fileno()).st_mode):
            raise ValueError(f"{source} is not a file")
        with open(target, "wb") as dst:
            shutil.copyfileobj(src, dst, 1 << 20)


def publish_images(staging: Path, public: Path) -> None:
    """Copy the image files the warming wrote, as the account, into the
    directory the runners mount, which root owns. A runner has every
    capability of its user namespace and can remount its read-only mount
    writable. Over a directory of the account's it could then change what a
    job on main loads, over root's it can still only read.
    """
    entries = []
    for line in (staging / "index").read_text().splitlines():
        image, _, name = line.partition("\t")
        if not IMAGE_REF.match(image) or not IMAGE_FILE.match(name):
            raise ValueError(f"not an image file entry: {line!r}")
        entries.append((image, name))
    public.mkdir(mode=0o755, parents=True, exist_ok=True)
    for _, name in entries:
        target = public / name
        if target.exists():
            continue
        partial = public / f".{name}"
        copy_regular(staging / name, partial)
        os.chmod(partial, 0o644)
        os.replace(partial, target)
    partial = public / ".index"
    partial.write_text("".join(f"{image}\t{name}\n" for image, name in entries))
    os.chmod(partial, 0o644)
    os.replace(partial, public / "index")
    names = {name for _, name in entries} | {"index"}
    for path in public.iterdir():
        if path.name not in names:
            path.unlink()


def dockerd_options() -> list[str]:
    """What a container that runs its own dockerd needs. Not --privileged,
    which would hand it every device of the host the account may open.
    dockerd needs the capabilities of its user namespace, which are the CI
    account's and no more, no seccomp filter, and cgroups it can write, which
    systemd mode gives.
    """
    return [
        "--rm",
        "--cap-add=all",
        "--security-opt=seccomp=unconfined",
        "--security-opt=label=disable",
        "--security-opt=unmask=all",
        "--security-opt=systempaths=unconfined",
        "--systemd=always",
        "--stop-signal=SIGTERM",
        "--stop-timeout=60",
    ]


def slot_options(config: Config, pool: str, slot: int) -> list[str]:
    """What every runner gets. Named after its slot, so a second container in
    it is refused. Each pool and slot keeps its own Docker store and tool
    caches, so a job starts warm. The shared image files come read only: a
    job can load from them and never change what another slot loads.
    """
    return [
        *dockerd_options(),
        "--name",
        slot_unit(pool, slot),
        "--label",
        f"{runner_label()}=1",
        "--label",
        f"{NAME}.pool={pool}",
        "--label",
        f"{NAME}.slot={slot}",
        "--volume",
        f"{config['images_dir']}:{IMAGES_MOUNT}:ro",
        "--volume",
        f"{NAME}-docker-{pool}-{slot}:/var/lib/docker",
        "--volume",
        f"{NAME}-cache-{pool}-{slot}:/root/.cache",
    ]


# One runner takes this many to describe.
def run_command(config: Config, name: str, repo: str, pool: str, slot: int, env_file: str) -> list[str]:  # noqa: PLR0913, PLR0917
    """The podman run of one runner. The jobs' environment is the play's and
    shows in the argument list, so it never holds a secret.
    """
    job_env = []
    for key, value in sorted(config.get("job_env", {}).items()):
        job_env += ["--env", f"{key}={value}"]
    return [
        "podman",
        "run",
        *slot_options(config, pool, slot),
        "--label",
        f"{NAME}.name={name}",
        "--label",
        f"{NAME}.repo={repo}",
        "--env-file",
        env_file,
        *job_env,
        config["image"],
    ]


def warm_command(config: Config) -> list[str]:
    """The podman run that pulls the jobs' images once, into a store of its
    own, and writes them to the shared image files. Runs no job's code, so it
    alone may write there.
    """
    return [
        "run",
        *dockerd_options(),
        "--name",
        images_container(),
        # One an interrupted play left goes, not this one.
        "--replace",
        "--volume",
        f"{config['images_staging']}:{IMAGES_MOUNT}",
        "--volume",
        f"{NAME}-docker-images:/var/lib/docker",
        "--entrypoint",
        "/usr/local/bin/ci-runner-warm",
        config["image"],
        *config["warm_images"],
    ]


# Where a job's time goes, for the report. Written by the dispatcher alone,
# root's to read, and nothing in it feeds a decision: the allocation never
# reads it.
TIMING_VERSION = 1
# A poll sample is written when what it holds changes, and at least this often.
SAMPLE_EVERY = timedelta(minutes=5)
# A job pending longer than a job may run and this is written with what is
# known, and dropped.
PENDING_SLACK = timedelta(hours=1)
TIMING_FILE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.jsonl$")
# Job and workflow names are the repositories' authors' text.
NAME_MAX = 200
# What every job record holds, null when unknown.
JOB_FIELDS = (
    "repo", "pool", "job_id", "run_id", "run_attempt", "workflow", "job", "conclusion", "runner", "slot",
    "created", "seen", "slot_given", "registered", "unit_started", "container_started", "job_started",
    "job_completed", "container_died", "container_removed",
)
RUNNER_FIELDS = ("slot", "slot_given", "registered", "unit_started", "container_started", "container_died",
                 "container_removed")


def utc_now() -> datetime:
    """The time now, for the records."""
    return datetime.now(UTC)


def stamp(when: datetime | None) -> str | None:
    """A time as the records hold it: UTC, to the second."""
    return when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ") if when else None


def github_time(value: object) -> str | None:
    """A time GitHub gave, in the records' form, or null if it is not one."""
    if not isinstance(value, str):
        return None
    try:
        when = datetime.fromisoformat(value)
    except ValueError:
        return None
    return stamp(when if when.tzinfo else when.replace(tzinfo=UTC))


def number(value: object) -> int | None:
    """A whole number GitHub gave, or null."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def text(value: object) -> str | None:
    """A name GitHub gave, cut short, or null."""
    return value[:NAME_MAX] if isinstance(value, str) and value else None


def job_fields(job: JsonObject) -> JsonObject:
    """What a job record takes from GitHub's answer for the job. Unknown
    fields are left out, so they do not replace what an earlier answer gave.
    The job started when its first step did, which is when a runner took it;
    started_at is the fallback.
    """
    steps = job.get("steps")
    first = steps[0] if isinstance(steps, list) and steps and isinstance(steps[0], dict) else {}
    fields = {
        "job_id": number(job.get("id")),
        "run_id": number(job.get("run_id")),
        "run_attempt": number(job.get("run_attempt")),
        "workflow": text(job.get("workflow_name")),
        "job": text(job.get("name")),
        "created": github_time(job.get("created_at")),
        "conclusion": text(job.get("conclusion")),
        "runner": text(job.get("runner_name")),
        "job_started": github_time(first.get("started_at")) or github_time(job.get("started_at")),
        "job_completed": github_time(job.get("completed_at")),
    }
    return {key: value for key, value in fields.items() if value is not None}


def job_record(pending: JsonObject, job: JsonObject | None, runner: JsonObject | None) -> JsonObject:
    """A finished job's record: what was noted while it was pending, GitHub's
    last answer, and the times of the runner that ran it, which need not be
    the runner started for it.
    """
    record: JsonObject = {"v": TIMING_VERSION, "kind": "job"} | dict.fromkeys(JOB_FIELDS)
    record |= {key: value for key, value in pending.items() if key in JOB_FIELDS}
    if job is not None:
        record |= job_fields(job)
    if runner is not None:
        record |= {key: runner.get(key) for key in RUNNER_FIELDS}
    return record


class Timing:
    """Records where each job's time went, as JSON lines in a file per UTC
    day, root's alone. The jobs and runners still in flight are kept in
    pending.json, so a restart of the dispatcher loses none. Nothing here
    may stop a poll: a write that fails is logged once a day and dropped.
    """

    def __init__(self, directory: Path | None, keep_days: int, job_max_seconds: int) -> None:
        """Off without a directory or with keep_days 0. Reads what was pending."""
        self.directory = directory if directory is not None and keep_days > 0 else None
        self.keep_days = keep_days
        self.pending_max = timedelta(seconds=job_max_seconds) + PENDING_SLACK
        # By job id, as a string since they go to JSON as keys.
        self.jobs: dict[str, JsonObject] = {}
        # By runner name.
        self.runners: dict[str, JsonObject] = {}
        # What pending.json holds: nothing until something is pending.
        self.saved = json.dumps({"jobs": {}, "runners": {}}, sort_keys=True)
        self.failed_on: str | None = None
        self.pruned_on: str | None = None
        self.sampled: tuple[JsonObject, datetime] | None = None
        self.load()

    def fail(self, error: OSError) -> None:
        """Log a failed write, once a day."""
        day = f"{utc_now():%Y-%m-%d}"
        if self.failed_on != day:
            self.failed_on = day
            log(f"timing: {error}; not recorded")

    def load(self) -> None:
        """What was pending when the last dispatcher stopped."""
        if self.directory is None:
            return
        try:
            data = json.loads((self.directory / "pending.json").read_text())
        except FileNotFoundError:
            return
        except (OSError, ValueError) as error:
            log(f"timing: could not read what was pending: {error}")
            return
        if not isinstance(data, dict):
            return
        jobs, runners = data.get("jobs"), data.get("runners")
        if isinstance(jobs, dict):
            self.jobs = {key: entry for key, entry in jobs.items()
                         if isinstance(entry, dict) and key == f"{number(entry.get('job_id'))}"
                         and isinstance(entry.get("noted"), str)}
        if isinstance(runners, dict):
            self.runners = {name: entry for name, entry in runners.items()
                            if isinstance(entry, dict) and isinstance(entry.get("slot_given"), str)}
        self.saved = json.dumps({"jobs": self.jobs, "runners": self.runners}, sort_keys=True)

    def save(self) -> None:
        """Write what is pending, when it changed."""
        if self.directory is None:
            return
        content = json.dumps({"jobs": self.jobs, "runners": self.runners}, sort_keys=True)
        if content == self.saved:
            return
        partial = self.directory / ".pending.json"
        try:
            partial.unlink(missing_ok=True)
            fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "w") as handle:
                handle.write(content)
            os.replace(partial, self.directory / "pending.json")
        except OSError as error:
            self.fail(error)
            return
        self.saved = content

    def append(self, record: JsonObject) -> None:
        """Add a record to today's file."""
        if self.directory is None:
            return
        path = self.directory / f"{utc_now():%Y-%m-%d}.jsonl"
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "w") as handle:
                handle.write(json.dumps(record, sort_keys=False) + "\n")
        except OSError as error:
            self.fail(error)

    def prune(self) -> None:
        """Remove the days past keep_days, at the first poll of a day."""
        if self.directory is None:
            return
        today = utc_now().date()
        if self.pruned_on == f"{today}":
            return
        oldest = f"{today - timedelta(days=self.keep_days - 1)}"
        try:
            for path in self.directory.iterdir():
                match = TIMING_FILE.match(path.name)
                if match and match.group(1) < oldest:
                    path.unlink()
        except OSError as error:
            self.fail(error)
            return
        self.pruned_on = f"{today}"

    def note_jobs(self, repo: str, pools: dict[str, str], jobs: list[JsonObject]) -> None:
        """The jobs of this poll's listing: a job queued or running is
        pending, from the first time it is listed; one completed is written.
        A job first listed already running was not seen queued.
        """
        if self.directory is None:
            return
        for job in jobs:
            job_id = number(job.get("id"))
            if job_id is None:
                continue
            key = f"{job_id}"
            status = job.get("status")
            if key not in self.jobs:
                if status not in {"queued", "in_progress"}:
                    continue
                pool = next((pool for label, pool in pools.items() if label in job.get("labels", [])), None)
                now = utc_now()
                self.jobs[key] = {"repo": repo, "pool": pool, "noted": stamp(now),
                                  "seen": stamp(now) if status == "queued" else None}
            self.jobs[key] |= job_fields(job)
            if status == "completed":
                self.finish(job)

    def gone(self, repo: str, listed: list[JsonObject]) -> list[int]:
        """The repository's pending jobs this poll's listing no longer has:
        their runs ended, and GitHub is asked for each once.
        """
        ids = {number(job.get("id")) for job in listed}
        return [entry["job_id"] for entry in self.jobs.values()
                if entry.get("repo") == repo and entry.get("job_id") not in ids]

    def finish(self, job: JsonObject) -> None:
        """Write a pending job GitHub says is completed, with the times of
        the runner that ran it.
        """
        key = f"{number(job.get('id'))}"
        pending = self.jobs.pop(key, None)
        if pending is None:
            return
        runner = self.runners.pop(text(job.get("runner_name")) or "", None)
        self.append(job_record(pending, job, runner))

    def drop(self, job_id: int) -> None:
        """Write a pending job with what is known, as when GitHub no longer has it."""
        pending = self.jobs.pop(f"{job_id}", None)
        if pending is not None:
            self.append(job_record(pending, None, None))

    def expire(self) -> None:
        """Write and drop the jobs pending too long, and forget such runners."""
        if self.directory is None:
            return
        cutoff = stamp(utc_now() - self.pending_max) or ""
        for entry in [entry for entry in self.jobs.values() if entry["noted"] < cutoff]:
            self.drop(entry["job_id"])
        self.runners = {name: entry for name, entry in self.runners.items()
                        if (entry.get("slot_given") or "") >= cutoff}

    def started(self, name: str, slot: int, slot_given: datetime, registered: datetime) -> None:
        """A runner this dispatcher registered and started."""
        if self.directory is not None:
            self.runners[name] = {"slot": slot, "slot_given": stamp(slot_given), "registered": stamp(registered),
                                  "unit_started": stamp(utc_now())}

    def containers(self, running: list[Runner]) -> None:
        """When each runner's container started, as podman lists it."""
        for runner in running:
            if runner.name in self.runners:
                self.runners[runner.name]["container_started"] = stamp(runner.started)

    def idle(self, runner: Runner, why: str) -> None:
        """A runner stopped before it took a job, and why."""
        if self.directory is None:
            return
        self.runners.pop(runner.name, None)
        self.append({"v": TIMING_VERSION, "kind": "idle", "runner": runner.name, "pool": runner.pool,
                     "slot": runner.slot, "started": stamp(runner.started), "stopped": stamp(utc_now()),
                     "why": why})

    def sample(self, sample: JsonObject, took: float, extra: JsonObject) -> None:
        """A poll's sample, how long the poll took and what else it notes,
        written when the sample differs from the last one written, and at
        least every SAMPLE_EVERY.
        """
        if self.directory is None:
            return
        now = utc_now()
        if self.sampled is not None and self.sampled[0] == sample and now - self.sampled[1] < SAMPLE_EVERY:
            return
        self.sampled = (sample, now)
        self.append({"v": TIMING_VERSION, "kind": "poll", "t": stamp(now), "took_s": round(took, 1)} | sample | extra)


class Dispatcher:
    """Decides at each poll which runners start and which stop, and carries out the commands."""

    def __init__(self, config: Config, github: GitHub, account: Account) -> None:
        """Nothing is registered or claimed yet."""
        self.config = config
        self.github = github
        self.account = account
        self.env_dir = Path(config["env_dir"])
        self.pause_file = Path(config["pause_file"])
        # Runner names registered by this process, and when.
        self.recent: dict[str, datetime] = {}
        # Slots given out lately: when, and to which pool (CLAIM_HOLD).
        self.claims: dict[int, tuple[datetime, str]] = {}
        timing_dir = config.get("timing_dir")
        self.timing = Timing(Path(timing_dir) if timing_dir else None, int(config.get("timing_keep_days", 0)),
                             config["job_max_seconds"])
        # What the last poll saw, for its timing sample.
        self.seen: JsonObject = {}
        # Pending jobs whose runs ended, by repository, looked up after the
        # runners are started.
        self.ended: dict[str, list[int]] = {}

    def labels(self) -> dict[str, str]:
        """The label of each pool."""
        return {pool: self.config["label"] + suffix for pool, suffix in POOLS.items()}

    def poll(self, now: datetime | None = None) -> None:
        """One poll, then what is still pending kept for the next dispatcher."""
        self.seen = {"queued": dict.fromkeys(POOLS, 0), "busy": None, "slots": self.config["slots"],
                     "waiting": 0, "sandbox": None, "paused": None}
        self.ended = {}
        self.timing.prune()
        try:
            self.poll_steps(now or datetime.now(UTC))
        finally:
            self.look_up_ended()
            self.timing.expire()
            self.timing.save()

    def sample(self, took: float) -> None:
        """Record what the last poll saw and how long it took. Calls left
        change at nearly every poll, so only whether they are few makes a
        sample new.
        """
        left = getattr(self.github, "rate_remaining", None)
        low = left is not None and left < RATE_LOW
        self.timing.sample(self.seen | {"rate_low": low}, took, {"rate_left": left})

    # The steps of a poll in their order, which reads best in one piece.
    def poll_steps(self, now: datetime) -> None:  # noqa: C901, PLR0912
        """One poll, in the steps the module's docstring gives."""
        self.clean_env_files(now)
        try:
            running = self.account.runners()
        except POLL_ERRORS as error:
            log(f"could not list the runners: {error}")
            running = None
        if running is not None:
            self.seen["busy"] = len({runner.slot for runner in running})
            self.timing.containers(running)
            self.stop_overdue(running, now)

        # First, even without the runners listed, paused or unfiltered.
        private = []
        for repo, answer in zip(self.config["repos"], self.each_repo(self.github.is_private), strict=True):
            try:
                if isinstance(answer, BaseException):
                    raise answer
                if answer:
                    private.append(repo)
                else:
                    self.retire(repo, running or [])
            except POLL_ERRORS as error:
                log(f"{repo}: {error}")
        if running is None:
            return

        # Jobs to start, by repository and pool. Idle runners are stopped
        # here, also while paused, so they hold no slot. GitHub is asked for
        # every repository at once, the answers are acted on one by one.
        labels = list(self.labels().values())

        def ask(repo: str) -> tuple[list[JsonObject], list[JsonObject]]:
            return self.github.runners(repo, labels), self.github.jobs(repo, labels)

        wanted: dict[str, dict[str, int]] = {}
        for repo, answer in zip(private, self.each_repo(ask, private), strict=True):
            try:
                if isinstance(answer, BaseException):
                    raise answer
                wanted[repo] = self.survey(repo, running, now, *answer)
            except POLL_ERRORS as error:
                log(f"{repo}: {error}")
        self.github_forget_unused()

        # Before the pause: a paused dispatcher lets running jobs finish, and
        # none may go on outside the sandbox.
        self.seen["sandbox"] = sandbox_held(self.config)
        if not self.seen["sandbox"]:
            log("The host's sandbox for the CI account is not held. No runner starts until it is.")
            self.stop_unheld(running)
            return
        self.seen["paused"] = self.pause_file.exists()
        if self.seen["paused"]:
            return
        self.allocate(wanted, running, now)
        self.warn_token(now)

    def each_repo[T](self, call: Callable[[str], T], repos: list[str] | None = None) -> list[T | Exception]:
        """call(repo) for every repository at once, each answer or its error."""

        def safe(repo: str) -> T | Exception:
            try:
                return call(repo)
            except POLL_ERRORS as error:
                return error

        asked = cast("list[str]", self.config["repos"]) if repos is None else repos
        with ThreadPoolExecutor(max(1, len(asked))) as pool:
            return list(pool.map(safe, asked))

    def github_forget_unused(self) -> None:
        """Drop the cached answers this poll did not ask for, where the client keeps any."""
        forget = getattr(self.github, "forget_unused", None)
        if forget:
            forget()

    def survey(self, repo: str, running: list[Runner], now: datetime,
               listed: list[JsonObject], jobs: list[JsonObject]) -> dict[str, int]:
        """Stop the repository's runners no job waits for and remove its
        leftover registrations. Gives the runners to start, by pool.
        """
        labels = self.labels()
        queued = queued_counts(jobs, list(labels.values()))
        self.timing.note_jobs(repo, {label: pool for pool, label in labels.items()}, jobs)
        self.ended[repo] = self.timing.gone(repo, jobs)
        registered = {entry["name"]: entry for entry in listed}
        wanted = {}
        for pool, label in labels.items():
            self.seen["queued"][pool] += queued[label]
            free = free_runners(repo, pool, running, registered)
            stopping = to_stop(free, queued[label], now)
            for runner in stopping:
                self.account.stop(runner.container)
                self.timing.idle(runner, "idle" if now - runner.started >= IDLE_MAX else "surplus")
                log(f"{repo}: stopped {runner.name}, no job for it")
            wanted[pool] = to_start(queued[label], len(free) - len(stopping))
        for runner in leftovers(listed, running, self.recent, now):
            self.github.remove_runner(repo, runner["id"])
            log(f"{repo}: removed the offline runner {runner['name']}")
        return wanted

    def look_up_ended(self) -> None:
        """Ask GitHub for the pending jobs whose runs have ended, for their
        records. Only the records depend on it, so it comes after the
        runners are started, at most LOOK_UPS at once, the rest at the next
        polls, and not while few calls are left this hour: a cancelled
        matrix would otherwise hold the next poll for every one of its jobs.
        """
        left = getattr(self.github, "rate_remaining", None)
        if left is not None and left < RATE_LOW:
            return
        asked = [(repo, job_id) for repo, job_ids in self.ended.items() for job_id in job_ids][:LOOK_UPS]

        def ask(pair: tuple[str, int]) -> JsonObject | Exception:
            try:
                return self.github.job(*pair)
            except POLL_ERRORS as error:
                return error

        with ThreadPoolExecutor(API_PARALLEL) as pool:
            answers = list(pool.map(ask, asked))
        # The timing is the poll's thread's alone.
        for (repo, job_id), answer in zip(asked, answers, strict=True):
            if not isinstance(answer, Exception):
                self.timing.finish(answer)
            elif isinstance(answer, urllib.error.HTTPError) and answer.code == HTTPStatus.NOT_FOUND:
                self.timing.drop(job_id)
            else:
                log(f"{repo}: could not look up job {job_id}: {answer}")

    def allocate(self, wanted: dict[str, dict[str, int]], running: list[Runner], now: datetime) -> None:
        """Every repository's main jobs before any pull request's, since main's
        checks gate a release, and pull requests never take the last slot.
        Within a pool the repositories take turns, so one repository's many
        jobs do not hold back another's.
        """
        self.claims = {slot: claim for slot, claim in self.claims.items() if now - claim[0] < CLAIM_HOLD}
        slots = self.config["slots"]
        # A slot just given to a pull request counts before its container shows.
        pr_slots = {runner.slot for runner in running if runner.pool == "pr"}
        pr_slots |= {slot for slot, (_, pool) in self.claims.items() if pool == "pr"}
        pr_room = max(1, slots - 1) - len(pr_slots)
        # A slot whose start failed is not tried again in this poll.
        failed: set[int] = set()
        for pool, label in self.labels().items():
            left = {repo: counts.get(pool, 0) for repo, counts in wanted.items()}
            while any(left.values()):
                for repo in [name for name in left if left[name]]:
                    free = free_slots(slots, running, set(self.claims) | failed)
                    if not free or (pool == "pr" and pr_room <= 0):
                        waiting = sum(left.values())
                        self.seen["waiting"] += waiting
                        log(f"{waiting} {pool} jobs wait for a free slot")
                        left = {}
                        break
                    slot = free[0]
                    self.claims[slot] = (now, pool)
                    left[repo] -= 1
                    if pool == "pr":
                        pr_room -= 1
                    try:
                        self.start(repo, pool, label, slot, now)
                    except POLL_ERRORS as error:
                        del self.claims[slot]
                        failed.add(slot)
                        log(f"{repo}: could not start a {pool} runner: {error}")

    def retire(self, repo: str, running: list[Runner]) -> None:
        """A public repository: no runner of this host stays with it, and its
        workflows no longer name this host's label.
        """
        if self.github.delete_variable(repo, self.config["variable"]):
            log(f"{repo} is not private: removed its {self.config['variable']} variable")
        for runner in running:
            if runner.repo == repo:
                self.account.stop(runner.container)
                log(f"{repo} is not private: stopped {runner.name}")
        for runner in self.github.runners(repo, list(self.labels().values())):
            try:
                self.github.remove_runner(repo, runner["id"])
                log(f"{repo} is not private: removed the runner {runner['name']}")
            except urllib.error.HTTPError as error:
                # A busy runner cannot be removed until its container stops.
                log(f"{repo}: could not remove {runner['name']} yet: {error}")

    def start(self, repo: str, pool: str, label: str, slot: int, now: datetime) -> None:
        """Register a runner for the repository and start its container in the slot."""
        name = runner_name(repo, pool)
        slot_given = utc_now()
        jit = self.github.jit_config(repo, name, label)
        registered = utc_now()
        self.recent[name] = now
        env_file = self.env_dir / f"{name}.env"
        # Readable by the account alone, which podman reads it as. Removed on a
        # later poll, once the container has taken it.
        fd = os.open(env_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
        with os.fdopen(fd, "w") as handle:
            handle.write(f"JIT_CONFIG={jit}\n")
        os.chown(env_file, self.account.uid, -1)
        self.account.start(
            run_command(self.config, name, repo, pool, slot, str(env_file)),
            unit=slot_unit(pool, slot),
            max_seconds=self.config["job_max_seconds"],
        )
        self.timing.started(name, slot, slot_given, registered)
        log(f"{repo}: started {name} in {pool} slot {slot}")

    def stop_overdue(self, running: list[Runner], now: datetime) -> None:
        """Stop every container that has run longer than a job may."""
        limit = timedelta(seconds=self.config["job_max_seconds"])
        # Any container of the account's slots, a nameless one too, such as
        # one an interrupted play left.
        for runner in running:
            if now - runner.started > limit:
                self.account.stop(runner.container)
                log(f"{runner.repo}: stopped {runner.name}, over {limit}")

    def stop_unheld(self, running: list[Runner]) -> None:
        """Every runner, when the sandbox is not held: a job that is running
        is unreviewed code too, and the host's check failing says its packets
        may no longer be filtered. A unit that stops takes the account's
        manager with it, but rules flushed under a unit that still reads
        active take nothing down by themselves.
        """
        for runner in running:
            self.account.stop(runner.container)
            log(f"{runner.repo}: stopped {runner.name}, the sandbox is not held")

    def clean_env_files(self, now: datetime) -> None:
        """Remove the runners' config files once their containers have read
        them, and forget the registrations that are no longer new.
        """
        for path in self.env_dir.glob("*.env"):
            age = now - datetime.fromtimestamp(path.stat().st_mtime, UTC)
            if age > timedelta(minutes=2):
                path.unlink(missing_ok=True)
        cutoff = now - REGISTRATION_GRACE * 2
        self.recent = {name: stamp for name, stamp in self.recent.items() if stamp > cutoff}

    def warm(self) -> int:
        """Pull the jobs' images once and write them to the shared image
        files, from which each slot loads what its store lacks before a job.
        Runners keep running meanwhile.
        """
        try:
            self.account.podman(*warm_command(self.config), timeout=1800)
        except subprocess.TimeoutExpired:
            # Only runuser was killed: the container would go on pulling.
            self.account.podman("stop", "--time", "10", images_container(), check=False)
            log("the image files were not written in 30 minutes")
            return 1
        except subprocess.CalledProcessError:
            return 1
        try:
            publish_images(Path(self.config["images_staging"]), Path(self.config["images_dir"]))
        except (OSError, ValueError) as error:
            log(f"the image files were not published: {error}")
            return 1
        log("the shared image files are up to date")
        return 0

    def enable(self) -> int:
        """Point every private repository's workflows at this host's label."""
        failed = 0
        for repo in self.config["repos"]:
            try:
                if not self.github.is_private(repo):
                    log(f"{repo} is not private: left on GitHub's runners")
                    continue
                self.github.set_variable(repo, self.config["variable"], self.config["label"])
                log(f"{repo}: {self.config['variable']}={self.config['label']}")
            except POLL_ERRORS as error:
                log(f"{repo}: {error}")
                failed += 1
        return 1 if failed else 0

    def disable(self) -> int:
        """Send every repository's jobs back to GitHub's runners."""
        failed = 0
        for repo in self.config["repos"]:
            try:
                if self.github.delete_variable(repo, self.config["variable"]):
                    log(f"{repo}: removed {self.config['variable']}")
            except POLL_ERRORS as error:
                log(f"{repo}: {error}")
                failed += 1
        return 1 if failed else 0

    def stop_all(self) -> None:
        """Stop every runner and remove every registration that carries this host's labels."""
        for runner in self.account.runners():
            self.account.stop(runner.container)
            log(f"stopped {runner.container}")
        for repo in self.config["repos"]:
            for runner in self.github.runners(repo, list(self.labels().values())):
                try:
                    self.github.remove_runner(repo, runner["id"])
                except urllib.error.HTTPError as error:
                    log(f"{repo}: could not remove {runner['name']}: {error}")

    def warn_token(self, now: datetime) -> None:
        """Log a warning while the token is about to expire."""
        if token_expires_soon(self.github.token_expiry, now):
            log(f"warning: the GitHub token expires {self.github.token_expiry}, renew it")


def token_expires_soon(expiry: str | None, now: datetime) -> bool:
    """Whether the token runs out within TOKEN_WARN. GitHub sends the date in
    the github-authentication-token-expiration header, as "2027-01-31 12:00:00
    UTC" or with an offset. A token with no expiry sends none.
    """
    if expiry is None or not expiry:
        return False
    for form in ("%Y-%m-%d %H:%M:%S %Z", "%Y-%m-%d %H:%M:%S %z"):
        try:
            # The form with a zone's name gives no offset: UTC is set below.
            when = datetime.strptime(expiry, form)  # noqa: DTZ007
        except ValueError:
            continue
        return when.replace(tzinfo=when.tzinfo or UTC) - now < TOKEN_WARN
    return False


def installed_name(script: str) -> str:
    """The runners' name, from the name the script is installed under."""
    name = Path(script).name.removesuffix(SUFFIX)
    if not Path(script).name.endswith(SUFFIX) or not NAME_RE.match(name):
        raise SystemExit(f"Installed as {script!r}, not as <name>{SUFFIX}")
    return name


def load_config(path: Path) -> Config:
    """Read the config, and refuse one that names no sandbox unit or holds a
    name that is not of its kind.
    """
    config = json.loads(path.read_text())
    if not config.get("sandbox_unit"):
        raise SystemExit("No sandbox_unit: the host must name the unit that holds the CI account")
    check = config.get("sandbox_check", [])
    if not isinstance(check, list) or not all(isinstance(part, str) for part in check):
        raise SystemExit("sandbox_check must be a list of arguments")
    for key in config.get("job_env", {}):
        if not ENV_NAME.match(key):
            raise SystemExit(f"Not an environment variable: {key!r}")
    for repo in config["repos"]:
        if not REPO.match(repo):
            raise SystemExit(f"Not a repository: {repo!r}")
    if not re.match(r"^[A-Z_][A-Z0-9_]*$", config["variable"]):
        raise SystemExit(f"Not a variable name: {config['variable']!r}")
    if not LABEL_RE.match(config["label"]):
        raise SystemExit(f"Not a label: {config['label']!r}")
    if not 1 <= int(config["slots"]) <= MAX_SLOTS:
        raise SystemExit("slots must be 1–8")
    return config


# One branch for each command, plainer than a table of them.
def main(argv: list[str]) -> int:  # noqa: C901, PLR0911, PLR0912
    """Carry out the command given. For run, poll until stopped."""
    commands = {"run", "poll", "pause", "resume", "stop", "warm", "enable", "disable", "remove"}
    # The script's own name and one command.
    if len(argv) != 2 or argv[1] not in commands:  # noqa: PLR2004
        print(__doc__, file=sys.stderr)
        return 2
    global NAME  # noqa: PLW0603
    NAME = installed_name(argv[0])
    config = load_config(Path("/etc") / NAME / "dispatcher.json")
    command = argv[1]
    pause_file = Path(config["pause_file"])
    if command == "pause":
        pause_file.touch()
        log(f"Paused: no runner starts until '{NAME}{SUFFIX} resume'.")
        return 0
    if command == "resume":
        pause_file.unlink(missing_ok=True)
        log("Resumed.")
        return 0
    token = Path(config["token_file"]).read_text().strip()
    dispatcher = Dispatcher(config, GitHub(token), Account(config["account"]))
    if command == "stop":
        dispatcher.stop_all()
        return 0
    if command == "warm":
        return dispatcher.warm()
    if command == "enable":
        return dispatcher.enable()
    if command == "disable":
        return dispatcher.disable()
    if command == "remove":
        status = dispatcher.disable()
        dispatcher.stop_all()
        return status
    if command == "poll":
        dispatcher.poll()
        return 0
    watcher = exit_watcher(dispatcher.account)
    watcher.start()
    while True:
        started = time.monotonic()
        # A container that ends from here on wakes the next poll, one that
        # ended before is in this poll's listing.
        watcher.wake.clear()
        try:
            dispatcher.poll()
        except POLL_ERRORS as error:
            log(f"poll failed: {error}")
        took = time.monotonic() - started
        if took > config["poll_seconds"]:
            log(f"a poll took {took:.0f} s")
        dispatcher.sample(took)
        left = dispatcher.github.rate_remaining
        pause = pause_after(took, config["poll_seconds"], left)
        if left is not None and left < RATE_LOW:
            log(f"{left} GitHub calls left this hour: polling every minute")
            time.sleep(pause)
            continue
        # The least pause in any case, then until the rest is over or a
        # runner's container is gone.
        time.sleep(MIN_PAUSE)
        watcher.sleep(pause - MIN_PAUSE)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
