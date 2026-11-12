import os
from pathlib import Path

import job_strays
import pytest


def test_strays_are_everything_of_the_account_off_my_line() -> None:
    processes = {1: (0, 1000), 7: (1, 1000), 8: (7, 1000), 9: (8, 1000), 20: (1, 1000), 21: (20, 1000), 30: (1, 0)}

    assert job_strays.strays(processes, 9, 1000) == [20, 21]


def test_main_kills_only_the_strays(capsys: pytest.CaptureFixture[str]) -> None:
    me, parent = os.getpid(), os.getppid()
    uid = os.getuid()
    processes = {me: (parent, uid), parent: (1, uid), 1: (0, uid), 4242: (1, uid), 5000: (1, uid + 1)}
    killed: list[int] = []

    def kill(pid: int, sig: int) -> None:
        killed.append(pid)
        del processes[pid]

    assert job_strays.main(kill=kill, processes=lambda: processes, sleep=lambda s: None) == 0

    assert killed == [4242]
    assert "Stopped 1 process(es)" in capsys.readouterr().out


def test_this_box_s_proc_is_read() -> None:
    assert os.getpid() in job_strays.read_processes()


def table(*strays: int) -> dict[int, tuple[int, int]]:
    me, parent, uid = os.getpid(), os.getppid(), os.getuid()
    return {me: (parent, uid), parent: (1, uid), 1: (0, uid)} | dict.fromkeys(strays, (1, uid))


def test_a_stray_that_forked_meanwhile_is_killed_on_the_next_pass(capsys: pytest.CaptureFixture[str]) -> None:
    rounds = [table(4242), table(4243), table()]
    killed: list[int] = []

    code = job_strays.main(kill=lambda pid, sig: killed.append(pid), processes=lambda: rounds.pop(0), sleep=lambda s: None)

    assert code == 0 and killed == [4242, 4243]
    assert "Stopped 2 process(es)" in capsys.readouterr().out


def test_strays_that_will_not_stop_fail_the_step(capsys: pytest.CaptureFixture[str]) -> None:
    code = job_strays.main(kill=lambda pid, sig: None, processes=lambda: table(4242), sleep=lambda s: None)

    assert code == 1
    assert "would not stop" in capsys.readouterr().out


def test_a_zombie_is_no_stray(tmp_path: Path) -> None:
    for pid, state in (("10", "S (sleeping)"), ("11", "Z (zombie)")):
        (tmp_path / pid).mkdir()
        (tmp_path / pid / "status").write_text(f"Name:\tx\nState:\t{state}\nPPid:\t1\nUid:\t1000\t1000\t1000\t1000\n")

    assert job_strays.read_processes(str(tmp_path)) == {10: (1, 1000)}
