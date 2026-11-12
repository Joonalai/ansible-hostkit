import json
import subprocess
from pathlib import Path
from typing import Any

import clean_runners
import pytest

GB = 1024**3
HOUR = 3600
NOW = 1_800_000_000


def done(stdout: str = "", returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


class Podman(list[tuple[str, ...]]):
    """Records calls; units in busy are working a job, units in gone have no container."""

    def __init__(
        self,
        busy: set[str] | None = None,
        gone: set[str] | None = None,
        prune_ok: bool = True,
        containers: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__()
        self.busy, self.gone, self.prune_ok = set(busy or ()), set(gone or ()), prune_ok
        self.containers = containers or []

    def __call__(self, *args: str) -> subprocess.CompletedProcess[str]:
        self.append(args)
        if args[:2] == ("image", "prune"):
            return done("sha1\nsha2\n") if self.prune_ok else done(returncode=125, stderr="locked")
        if args[0] == "images":
            return done("sha1\nsha2\nsha3\n")
        if args[0] == "ps":
            return done(json.dumps(self.containers))
        if args[0] == "rm":
            return done()
        if args[0] == "top":
            if args[1] in self.gone:
                return done(returncode=125, stderr="no such container")
            return done("COMMAND\nRunner.Listener\n" + ("Runner.Worker\n" if args[1] in self.busy else ""))
        raise AssertionError(args)


class Wipes(list[str]):
    def __call__(self, path: str) -> None:
        self.append(path)


def run(
    argv: list[str], podman: Podman, sizes: dict[str, int], capsys: pytest.CaptureFixture[str],
) -> tuple[int, Wipes, Any]:
    wipes = Wipes()
    code = clean_runners.main(argv, run=podman, measure=sizes.__getitem__, wipe=wipes, now=NOW)
    return code, wipes, capsys.readouterr()


def test_prunes_dangling_images_and_leaves_small_caches(capsys: pytest.CaptureFixture[str]) -> None:
    podman = Podman()
    code, wipes, out = run(["--cache-max-gb", "5", "/c/shop=u1,u2"], podman, {"/c/shop": 2 * GB}, capsys)
    assert code == 0
    assert podman[0] == ("image", "prune", "--force")
    assert "removed 2 dangling" in out.out
    assert "under the limit" in out.out
    assert wipes == []
    assert not any(call[0] == "top" for call in podman)


def test_empties_a_large_cache_when_its_runners_are_idle(capsys: pytest.CaptureFixture[str]) -> None:
    code, wipes, out = run(
        ["--cache-max-gb", "5", "/c/shop=u1,u2"], Podman(gone={"u2"}), {"/c/shop": 6 * GB}, capsys,
    )
    assert code == 0
    assert wipes == ["/c/shop"]
    assert "emptied" in out.out


def test_keeps_a_large_cache_while_a_job_runs(capsys: pytest.CaptureFixture[str]) -> None:
    code, wipes, out = run(
        ["--cache-max-gb", "5", "/c/shop=u1,u2", "/c/other=u3"],
        Podman(busy={"u2"}),
        {"/c/shop": 6 * GB, "/c/other": 6 * GB},
        capsys,
    )
    assert code == 0
    assert wipes == ["/c/other"]
    assert "a job is running" in out.out


def test_dry_run_changes_nothing(capsys: pytest.CaptureFixture[str]) -> None:
    podman = Podman()
    code, wipes, out = run(["--dry-run", "--cache-max-gb", "5", "/c/shop=u1"], podman, {"/c/shop": 6 * GB}, capsys)
    assert code == 0
    assert wipes == []
    assert ("image", "prune", "--force") not in podman
    assert "would remove 3 dangling" in out.out
    assert "would empty" in out.out


def test_a_failed_prune_fails_the_run(capsys: pytest.CaptureFixture[str]) -> None:
    code, wipes, out = run(["--cache-max-gb", "5", "/c/shop=u1"], Podman(prune_ok=False), {"/c/shop": 6 * GB}, capsys)
    assert code == 1
    assert wipes == []
    assert "locked" in out.err


def test_empty_and_size_on_disk(tmp_path: Path) -> None:
    (tmp_path / "uv" / "wheels").mkdir(parents=True)
    (tmp_path / "uv" / "wheels" / "a.whl").write_bytes(b"x" * 1000)
    (tmp_path / "npm").mkdir()
    (tmp_path / "link").symlink_to("/etc")
    assert clean_runners.size(str(tmp_path)) >= 1000
    clean_runners.empty(str(tmp_path))
    assert list(tmp_path.iterdir()) == []
    assert tmp_path.exists()


def container(
    container_id: str, project: str | None, age_hours: int, name: str | None = None, labels: dict[str, str] | None = None,
) -> dict[str, Any]:
    labels = dict(labels or {}, **({"com.docker.compose.project": project} if project is not None else {}))
    return {"Id": container_id, "Names": [name or container_id], "Created": NOW - age_hours * HOUR, "Labels": labels}


def test_a_stray_is_an_old_container_nothing_else_owns() -> None:
    containers = [
        container("s1", None, 20, name="pg184"),
        container("s2", None, 1, name="blog2-pg"),
        container("p1", "blog-9-tests", 20),
        container("r1", None, 20, name="github-runner-blog-1"),
        container("u1", None, 20, name="other", labels={"PODMAN_SYSTEMD_UNIT": "x.service"}),
    ]

    assert clean_runners.strays(containers, 12 * HOUR, NOW) == {"pg184": "s1"}


def test_strays_are_removed_with_their_volumes_kept(capsys: pytest.CaptureFixture[str]) -> None:
    podman = Podman(containers=[container("s1", None, 20, name="pg184")])

    code, _, out = run(["--cache-max-gb", "5", "--strays-max-age-hours", "12"], podman, {}, capsys)

    assert code == 0
    assert ("rm", "--force", "--volumes=false", "s1") in podman
    assert "pg184" in out.out


def test_strays_are_left_alone_unless_asked_for(capsys: pytest.CaptureFixture[str]) -> None:
    podman = Podman(containers=[container("s1", None, 20, name="pg184")])

    run(["--cache-max-gb", "5"], podman, {}, capsys)

    assert not any(call[0] in ("ps", "rm") for call in podman)


def test_a_dry_run_removes_no_stray(capsys: pytest.CaptureFixture[str]) -> None:
    podman = Podman(containers=[container("s1", None, 20, name="pg184")])

    _, _, out = run(["--dry-run", "--cache-max-gb", "5", "--strays-max-age-hours", "12"], podman, {}, capsys)

    assert not any(call[0] == "rm" for call in podman)
    assert "would remove" in out.out
