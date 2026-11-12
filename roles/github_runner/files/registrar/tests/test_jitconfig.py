import base64
import email.message
import io
import json
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Never, override

import jitconfig
import pytest

type ConfigFile = tuple[dict[str, Any], Path]


def unb64url(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


@pytest.fixture
def key(tmp_path: Path) -> Path:
    path = tmp_path / "app.pem"
    subprocess.run(
        ["openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048", "-out", str(path)],
        check=True,
        capture_output=True,
    )
    return path


@pytest.fixture
def config(key: Path, tmp_path: Path) -> ConfigFile:
    config = {"app_id": "12345", "key": str(key), "labels": ["server"], "repos": {"shop": {"repo": "octo/shop", "runners": 3}}}
    path = tmp_path / "registrar.json"
    path.write_text(json.dumps(config))
    return config, path


class GitHub:
    """Answers the calls a registration makes, and records them."""

    def __init__(self, runners: list[dict[str, Any]] | None = None) -> None:
        self.calls: list[tuple[str, str, str | None, dict[str, Any] | None]] = []
        self.runners = runners

    def __call__(self, req: urllib.request.Request, timeout: float) -> io.BytesIO:
        body = json.loads(req.data) if isinstance(req.data, bytes) and req.data else None
        self.calls.append((req.get_method(), req.full_url, req.headers["Authorization"], body))
        path = req.full_url.removeprefix(jitconfig.API).split("?", 1)[0]
        if req.get_method() == "DELETE":
            return io.BytesIO(b"")
        if self.runners is not None and path == "/repos/octo/shop/actions/runners":
            return io.BytesIO(json.dumps({"total_count": len(self.runners), "runners": self.runners}).encode())
        answers = {
            "/repos/octo/shop/installation": {"id": 99},
            "/app/installations/99/access_tokens": {"token": "ghs_installation"},
            "/repos/octo/shop/actions/runners/generate-jitconfig": {"encoded_jit_config": "JITCONFIG"},
            "/repos/octo/shop/actions/runners": {
                "total_count": 1,
                "runners": [
                    {
                        "id": 1,
                        "name": "github-runner-shop-1-1",
                        "status": "online",
                        "busy": False,
                        "labels": [{"id": 1, "name": "self-hosted"}, {"id": 2, "name": "server"}],
                    },
                ],
            },
        }
        return io.BytesIO(json.dumps(answers[path]).encode())


def test_the_jwt_verifies_against_the_public_key(key: Path, tmp_path: Path) -> None:
    token = jitconfig.app_jwt("12345", str(key), now=1_000_000)
    header, payload, signature = token.split(".")

    assert json.loads(unb64url(header)) == {"alg": "RS256", "typ": "JWT"}
    assert json.loads(unb64url(payload)) == {"iat": 999_940, "exp": 1_000_540, "iss": "12345"}

    public = tmp_path / "app.pub"
    subprocess.run(["openssl", "pkey", "-in", str(key), "-pubout", "-out", str(public)], check=True)
    sig = tmp_path / "sig"
    sig.write_bytes(unb64url(signature))
    verified = subprocess.run(
        ["openssl", "dgst", "-sha256", "-verify", str(public), "-signature", str(sig)],
        input=f"{header}.{payload}".encode(),
        capture_output=True,
        check=False,
    )
    assert verified.returncode == 0, verified.stderr


def test_register_prints_the_jit_config_for_the_environment_file(config: ConfigFile, capsys: pytest.CaptureFixture[str]) -> None:
    _, path = config
    github = GitHub()

    assert jitconfig.main(["register", "shop", "2"], str(path), github) == 0
    assert capsys.readouterr().out == "RUNNER_JITCONFIG=JITCONFIG\n"


def test_register_narrows_the_token_and_registers_with_it(config: ConfigFile) -> None:
    conf, _ = config
    github = GitHub()

    jitconfig.register(conf, "shop", 2, github, now=1_700_000_000)

    (_, _, lookup_auth, _), (_, _, mint_auth, mint_body), _listing, (method, url, jit_auth, jit_body) = github.calls
    assert lookup_auth == mint_auth != "Bearer ghs_installation"
    assert mint_body == {"repositories": ["shop"], "permissions": {"administration": "write"}}
    assert (method, url, jit_auth) == (
        "POST",
        f"{jitconfig.API}/repos/octo/shop/actions/runners/generate-jitconfig",
        "Bearer ghs_installation",
    )
    assert jit_body == {
        "name": "github-runner-shop-2-1700000000",
        "runner_group_id": 1,
        "labels": ["server"],
        "work_folder": "_work",
    }


def test_the_work_folder_is_the_slots_own_host_directory(config: ConfigFile) -> None:
    conf, _ = config
    github = GitHub()

    jitconfig.register({**conf, "work": "/home/coder/work"}, "shop", 2, github, now=1_700_000_000)

    assert (github.calls[-1][3] or {})["work_folder"] == "/home/coder/work/shop-2"


def test_status_lists_the_runners(config: ConfigFile, capsys: pytest.CaptureFixture[str]) -> None:
    _, path = config

    assert jitconfig.main(["status", "shop"], str(path), GitHub()) == 0
    assert json.loads(capsys.readouterr().out) == [
        {"name": "github-runner-shop-1-1", "status": "online", "busy": False, "labels": ["self-hosted", "server"], "slot": 1},
    ]


@pytest.mark.parametrize(
    ("registration", "slot"),
    [
        ("github-runner-shop-2-1700000000", 2),
        ("earlier-prefix-shop-2-1700000000", 2),
        ("github-runner-shop-hq-2-1700000000", None),
        ("github-runner-workshop-2-1700000000", None),
        ("github-runner-shop-2", None),
        ("someone-elses", None),
    ],
)
def test_find_slot_reads_the_slot_after_the_name_whatever_the_prefix(registration: str, slot: int | None) -> None:
    assert jitconfig.find_slot("shop", registration) == slot


def test_status_counts_a_busy_runner_of_an_earlier_prefix_in_its_slot(config: ConfigFile, capsys: pytest.CaptureFixture[str]) -> None:
    conf, _ = config
    busy = {**runner(1, "earlier-prefix-shop-2-100", "online"), "busy": True}
    github = GitHub([busy, runner(2, "github-runner-shop-hq-1-200", "online")])

    listed = jitconfig.status(conf, "shop", github)

    assert [(r["name"], r["busy"], r["slot"]) for r in listed] == [
        ("earlier-prefix-shop-2-100", True, 2),
        ("github-runner-shop-hq-1-200", False, None),
    ]


def test_a_name_not_in_the_config_is_refused_before_any_call(config: ConfigFile, capsys: pytest.CaptureFixture[str]) -> None:
    _, path = config
    github = GitHub()

    assert jitconfig.main(["register", "octo/other", "1"], str(path), github) == 2
    assert github.calls == []
    assert "not a runner this box registers" in capsys.readouterr().err


@pytest.mark.parametrize("slot", ["0", "4", "-1", "+1", " 1", "1.0", "\u0661", "two"])
def test_a_slot_outside_the_repositorys_is_refused_before_any_call(
    config: ConfigFile, slot: str, capsys: pytest.CaptureFixture[str],
) -> None:
    _, path = config
    github = GitHub()

    assert jitconfig.main(["register", "shop", slot], str(path), github) == 2
    assert github.calls == []
    assert "has runner slots 1 to 3" in capsys.readouterr().err


@pytest.mark.parametrize(
    "argv",
    [[], ["register"], ["register", "shop"], ["delete", "shop"], ["register", "shop", "1", "extra"], ["status", "shop", "1"]],
)
def test_anything_else_is_a_usage_error(config: ConfigFile, argv: list[str]) -> None:
    _, path = config
    assert jitconfig.main(argv, str(path), GitHub()) == 2


def test_a_refusal_from_github_is_one_line_naming_it(config: ConfigFile, capsys: pytest.CaptureFixture[str]) -> None:
    _, path = config

    def forbidden(req: urllib.request.Request, timeout: float) -> Never:
        raise urllib.error.HTTPError(
            req.full_url, 403, "Forbidden", email.message.Message(), io.BytesIO(b'{"message":"Resource not accessible"}'),
        )

    assert jitconfig.main(["register", "shop", "1"], str(path), forbidden) == 1
    err = capsys.readouterr().err
    assert err.startswith("GitHub answered 403 for https://api.github.com/repos/octo/shop/installation")
    assert "Resource not accessible" in err


def test_a_key_openssl_cannot_read_is_one_line_naming_it(config: ConfigFile, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    conf, path = config
    conf["key"] = str(tmp_path / "missing.pem")
    path.write_text(json.dumps(conf))

    assert jitconfig.main(["register", "shop", "1"], str(path), GitHub()) == 1
    assert capsys.readouterr().err.startswith(f"openssl could not sign with {conf['key']}: ")


def online(number: int) -> dict[str, Any]:
    labels = [{"id": 1, "name": "self-hosted"}]
    return {"id": number, "name": f"github-runner-shop-1-{number}", "status": "online", "busy": False, "labels": labels}


class Paged(GitHub):
    """Lists 250 runners, a page at a time, as GitHub does."""

    @override
    def __call__(self, req: urllib.request.Request, timeout: float) -> io.BytesIO:
        if "/actions/runners?" not in req.full_url:
            return super().__call__(req, timeout)
        self.calls.append(("GET", req.full_url, req.headers["Authorization"], None))
        query = dict(part.split("=") for part in req.full_url.split("?", 1)[1].split("&"))
        size, page = int(query["per_page"]), int(query["page"])
        runners = [online(n) for n in range(1, 251)][(page - 1) * size : page * size]
        return io.BytesIO(json.dumps({"total_count": 250, "runners": runners}).encode())


def test_status_reads_every_page(config: ConfigFile, capsys: pytest.CaptureFixture[str]) -> None:
    _, path = config
    github = Paged()

    assert jitconfig.main(["status", "shop"], str(path), github) == 0

    listed = json.loads(capsys.readouterr().out)
    assert [runner["name"] for runner in listed] == [f"github-runner-shop-1-{n}" for n in range(1, 251)]
    pages = [url for _, url, _, _ in github.calls if "/actions/runners?" in url]
    assert len(pages) == 3


def runner(number: int, name: str, status: str) -> dict[str, Any]:
    return {"id": number, "name": name, "status": status, "busy": False, "labels": []}


def test_register_deletes_only_the_slots_offline_registrations_first(config: ConfigFile) -> None:
    conf, _ = config
    github = GitHub(
        [
            runner(1, "github-runner-shop-2-100", "offline"),
            runner(2, "github-runner-shop-2-200", "offline"),
            runner(3, "github-runner-shop-2-300", "online"),
            runner(4, "github-runner-shop-1-100", "offline"),
            runner(5, "github-runner-shop-20-100", "offline"),
            runner(6, "someone-elses", "offline"),
            runner(7, "earlier-prefix-shop-2-50", "offline"),
            runner(8, "earlier-prefix-shop-2-60", "online"),
            runner(9, "github-runner-shop-hq-2-100", "offline"),
        ],
    )

    jitconfig.register(conf, "shop", 2, github, now=1_700_000_000)

    deleted = [url for method, url, _, _ in github.calls if method == "DELETE"]
    assert deleted == [f"{jitconfig.API}/repos/octo/shop/actions/runners/{n}" for n in (1, 2, 7)]
    assert github.calls[-1][1].endswith("/generate-jitconfig")


def test_a_delete_github_refuses_is_said_and_the_slot_still_registers(config: ConfigFile, capsys: pytest.CaptureFixture[str]) -> None:
    _, path = config
    github = GitHub([runner(1, "github-runner-shop-1-100", "offline")])

    def refusing(req: urllib.request.Request, timeout: float) -> io.BytesIO:
        if req.get_method() == "DELETE":
            github.calls.append(("DELETE", req.full_url, None, None))
            raise urllib.error.HTTPError(req.full_url, 422, "Busy", email.message.Message(), io.BytesIO(b"{}"))
        return github(req, timeout)

    assert jitconfig.main(["register", "shop", "1"], str(path), refusing) == 0
    out, err = capsys.readouterr()
    assert out == "RUNNER_JITCONFIG=JITCONFIG\n"
    assert err == "Could not delete github-runner-shop-1-100: GitHub answered 422\n"


def test_a_list_that_times_out_is_said_and_the_slot_still_registers(config: ConfigFile, capsys: pytest.CaptureFixture[str]) -> None:
    _, path = config
    github = GitHub()

    def timing_out(req: urllib.request.Request, timeout: float) -> io.BytesIO:
        if req.get_method() == "GET" and "/actions/runners?" in req.full_url:
            raise urllib.error.URLError("timed out")
        return github(req, timeout)

    assert jitconfig.main(["register", "shop", "1"], str(path), timing_out) == 0
    out, err = capsys.readouterr()
    assert out == "RUNNER_JITCONFIG=JITCONFIG\n"
    assert "Could not list the earlier registrations" in err


def test_the_rebase_slot_registers_with_the_rebase_labels_alone(config: ConfigFile) -> None:
    conf, _ = config
    conf = {**conf, "rebase_labels": ["self-hosted", "claude-rebase"],
            "repos": {"shop": {"repo": "octo/shop", "runners": 3, "rebase_slot": 3}}}

    rebase = GitHub()
    jitconfig.register(conf, "shop", 3, rebase, now=1)
    work = GitHub()
    jitconfig.register(conf, "shop", 2, work, now=1)

    assert (rebase.calls[-1][3] or {})["labels"] == ["self-hosted", "claude-rebase"]
    assert (work.calls[-1][3] or {})["labels"] == ["server"]
