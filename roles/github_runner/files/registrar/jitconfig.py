#!/usr/bin/python3 -I
"""Mint a single-use GitHub Actions runner registration for one repository.

Runs as the registrar user, the only account that can read the GitHub App's
private key, and is reached from the coder account through one sudo rule:

    sudo -n -u ghregistrar /usr/local/libexec/github-runner-jitconfig register app 2

which prints RUNNER_JITCONFIG=<config> for the runner unit's environment
file, after deleting the slot's earlier registrations GitHub still lists
offline. The arguments are a name from the config file, never a repository,
and which of that repository's runner slots asks, so whoever can run this
can register runners for the repositories listed there, as many at a time
as they have slots, and for nothing else. "status <name>" lists that
repository's runners instead, which is how the play checks the whole chain
end to end.

The App token never leaves this process: what is printed is a JIT config,
good for registering exactly one ephemeral runner. Its work folder is the
slot's own directory under the config's `work`, which the unit mounts at
that same path: the compose CLI in a job hands the podman socket host paths
for its bind mounts, so a checkout only inside the container would reach a
container it starts as an empty or missing directory.

Standard library only, plus the openssl binary for the RS256 signature, so
it runs on the system python3 with nothing installed.
"""

import base64
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Any, Protocol, cast

CONFIG = "/etc/github-runner/registrar.json"
API = "https://api.github.com"
# GitHub's largest page of runners.
PAGE = 100
# How many arguments each action takes, its own name among them.
ARGUMENTS = {"register": 3, "status": 2}


class Answer(Protocol):
    """What an opened request gives back: its body, to read once."""

    def read(self) -> bytes:
        """The whole body."""


# urllib.request.urlopen, or what stands in for it under test.
type Opener = Callable[..., AbstractContextManager[Answer]]


def b64url(data: bytes) -> str:
    """Base64 as a JWT wants it: the URL alphabet, without padding."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def app_jwt(app_id: str, key_path: str, now: float | None = None) -> str:
    """The JWT that authenticates as the App itself, valid for nine minutes.

    Backdated a minute against clock drift, which GitHub's docs ask for; the
    ten-minute ceiling is theirs too.
    """
    now = int(time.time() if now is None else now)
    header = b64url(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    payload = b64url(json.dumps({"iat": now - 60, "exp": now + 540, "iss": str(app_id)}).encode())
    signing_input = f"{header}.{payload}".encode()
    signature = subprocess.run(
        ["openssl", "dgst", "-sha256", "-sign", key_path],
        input=signing_input,
        capture_output=True,
        check=True,
    ).stdout
    return f"{header}.{payload}.{b64url(signature)}"


# Any, as whatever JSON GitHub answers with is the caller's to know.
def request(method: str, path: str, token: str, body: dict[str, Any] | None = None, opener: Opener = urllib.request.urlopen) -> Any:  # noqa: ANN401
    """One call to GitHub's API with the token, and its JSON answer, or None when it has no body."""
    # API is https and a constant, so no other scheme can be opened.
    req = urllib.request.Request(  # noqa: S310
        API + path,
        method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "github-runner",
        },
    )
    with opener(req, timeout=30) as response:
        # A DELETE answers 204 with no body.
        data = response.read()
        return json.loads(data) if data else None


def repo_token(config: dict[str, Any], repo: str, opener: Opener = urllib.request.urlopen) -> str:
    """An installation token for one repository, narrowed to administration.

    Narrowed even though the App asks for nothing else: a token that can only
    manage runners on one repository is what this process holds, whatever the
    App is later given.
    """
    jwt = app_jwt(config["app_id"], config["key"])
    installation = request("GET", f"/repos/{repo}/installation", jwt, opener=opener)
    answer = request(
        "POST",
        f"/app/installations/{installation['id']}/access_tokens",
        jwt,
        {"repositories": [repo.split("/", 1)[1]], "permissions": {"administration": "write"}},
        opener=opener,
    )
    return cast("str", answer["token"])


def runner_name(name: str, slot: int, now: float | None = None) -> str:
    """Unique per registration: GitHub refuses a JIT config for a name in use,
    and an ephemeral runner killed while idle stays listed for a while. The
    slot keeps two slots registering in the same second apart, and names the
    unit to look at.
    """
    return f"github-runner-{name}-{slot}-{int(time.time() if now is None else now)}"


def find_slot(name: str, registration: str) -> int | None:
    """The slot a registration of the name is for, read from the end of its
    name (-<name>-<slot>-<time>) whatever prefix it carries, so one made
    under an earlier prefix still counts, or None for another name's: the
    slot must follow -<name>- at once, so "app-hq-1-5" is not app's.
    """
    match = re.search(rf"-{re.escape(name)}-([0-9]+)-[0-9]+$", registration)
    return int(match.group(1)) if match else None


def list_runners(repo: str, token: str, opener: Opener = urllib.request.urlopen) -> list[dict[str, Any]]:
    """Every runner GitHub lists for the repository, page by page: an
    ephemeral runner killed while idle stays listed offline for a while,
    and with hundreds of those, the online ones fall past the first page,
    and the play waits for them in vain.
    """
    runners: list[dict[str, Any]] = []
    page = 1
    while True:
        answer = request("GET", f"/repos/{repo}/actions/runners?per_page={PAGE}&page={page}", token, opener=opener)
        runners += answer["runners"]
        if len(answer["runners"]) < PAGE or len(runners) >= answer.get("total_count", 0):
            break
        page += 1
    return runners


def forget_earlier(repo: str, name: str, slot: int, token: str, opener: Opener = urllib.request.urlopen) -> None:
    """Delete the slot's earlier registrations that GitHub lists offline.

    The unit mints a registration only once the slot's last container is
    gone, so every earlier one of the slot is dead, but a runner killed
    while idle stays listed until GitHub drops it a day later. A slot that
    restarting every ten seconds leaves hundreds of them in an evening. Only
    this slot's, by the end of the name (find_slot), so those named under an
    earlier prefix go too, and only offline ones.
    A delete that fails is said and skipped: the registration matters more.
    """
    try:
        runners = list_runners(repo, token, opener)
    except (OSError, ValueError, KeyError) as e:
        print(f"Could not list the earlier registrations: {e}", file=sys.stderr)
        return
    for runner in runners:
        if find_slot(name, runner["name"]) == slot and runner["status"] == "offline":
            try:
                request("DELETE", f"/repos/{repo}/actions/runners/{runner['id']}", token, opener=opener)
            except urllib.error.HTTPError as e:
                print(f"Could not delete {runner['name']}: GitHub answered {e.code}", file=sys.stderr)
            except OSError as e:
                print(f"Could not delete {runner['name']}: {e}", file=sys.stderr)


def register(config: dict[str, Any], name: str, slot: int, opener: Opener = urllib.request.urlopen, now: float | None = None) -> str:
    """A JIT config for one new ephemeral runner in the slot, its earlier registrations forgotten first."""
    repo = config["repos"][name]["repo"]
    token = repo_token(config, repo, opener)
    forget_earlier(repo, name, slot, token, opener)
    jit = request(
        "POST",
        f"/repos/{repo}/actions/runners/generate-jitconfig",
        token,
        {
            "name": runner_name(name, slot, now),
            # Repository runners have only the default group.
            "runner_group_id": 1,
            # The rebase runner's labels share none with the others', so it
            # takes only what the workflow sends to it.
            "labels": config["rebase_labels"] if slot == config["repos"][name].get("rebase_slot") else config["labels"],
            "work_folder": f"{config['work']}/{name}-{slot}" if config.get("work") else "_work",
        },
        opener=opener,
    )
    return cast("str", jit["encoded_jit_config"])


def status(config: dict[str, Any], name: str, opener: Opener = urllib.request.urlopen) -> list[dict[str, Any]]:
    """The repository's runners as GitHub lists them: name, status, busy and
    labels, and the slot each is for (find_slot), None for another name's.
    """
    repo = config["repos"][name]["repo"]
    runners = list_runners(repo, repo_token(config, repo, opener), opener)
    return [
        {
            **{k: r[k] for k in ("name", "status", "busy")},
            "labels": [label["name"] for label in r["labels"]],
            "slot": find_slot(name, r["name"]),
        }
        for r in runners
    ]


USAGE = "usage: github-runner-jitconfig register <name> <slot> | status <name>"


def main(argv: list[str], config_path: str = CONFIG, opener: Opener = urllib.request.urlopen) -> int:
    """Print a slot's registration or a repository's runners, refusing any name or slot the config does not list."""
    if not argv or len(argv) != ARGUMENTS.get(argv[0]):
        print(USAGE, file=sys.stderr)
        return 2
    action, name, *rest = argv
    with open(config_path) as f:
        config = json.load(f)
    if name not in config["repos"]:
        print(f"{name!r} is not a runner this box registers; known: {', '.join(sorted(config['repos']))}", file=sys.stderr)
        return 2
    runners = config["repos"][name]["runners"]
    # isdecimal, so "+1", " 1" and "١" are refused rather than read as 1.  # noqa: RUF003 -- the digit is the example
    if rest and not (rest[0].isascii() and rest[0].isdecimal() and 1 <= int(rest[0]) <= runners):
        print(f"{name!r} has runner slots 1 to {runners}, not {rest[0]!r}", file=sys.stderr)
        return 2
    # One line for the journal, where the unit's restarts would otherwise
    # bury the reason under a traceback a minute.
    try:
        if action == "register":
            print(f"RUNNER_JITCONFIG={register(config, name, int(rest[0]), opener)}")
        else:
            print(json.dumps(status(config, name, opener)))
    except urllib.error.HTTPError as e:
        print(f"GitHub answered {e.code} for {e.url}: {e.read().decode(errors='replace')}", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as e:
        print(f"openssl could not sign with {config['key']}: {e.stderr.decode(errors='replace').strip()}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
