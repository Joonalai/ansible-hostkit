#!/usr/bin/python3 -I
"""Stop every process of the job's account that is not this step's own.

    claude-job-strays

The workflow runs it after Claude, before each step that mints a token or
uses one: the next part's, and the finish's. A step's leftover processes
live until the job ends, so a process Claude started in the background,
which its rules allow, could otherwise read the next step's environment
from /proc: create-github-app-token's INPUT_PRIVATE-KEY, the App's key,
which mints a token for every repository it is on, or a later GH_TOKEN.
What stays is this process and its ancestors, the runner among them. It
looks again until none is left, and fails the job when some will not stop.

It cannot reach a container started over the podman socket: that is
another namespace, and the socket's own reach is the role's notes' to say.

Standard library only, on the image's python3.
"""

import os
import signal
import sys
import time
from collections.abc import Callable

# How often the strays are killed and looked for again: one that forks
# while /proc is read is missed by a single pass.
PASSES, PASS_WAIT = 50, 0.1


def strays(processes: dict[int, tuple[int, int]], me: int, uid: int) -> list[int]:
    """The processes of uid that are neither me nor an ancestor of mine.
    processes maps pid to (ppid, uid).
    """
    keep, pid = set(), me
    while pid in processes and pid not in keep:
        keep.add(pid)
        pid = processes[pid][0]
    return sorted(pid_number for pid_number, (_, owner) in processes.items() if owner == uid and pid_number not in keep)


def read_processes(proc: str = "/proc") -> dict[int, tuple[int, int]]:
    """Every process but the dead ones waiting for their parent, which
    hold nothing any more.
    """
    found = {}
    for name in os.listdir(proc):
        if not name.isdecimal():
            continue
        try:
            with open(f"{proc}/{name}/status") as status_file:
                fields = dict(line.split(":", 1) for line in status_file if ":" in line)
            if fields["State"].strip().startswith("Z"):
                continue
            found[int(name)] = (int(fields["PPid"]), int(fields["Uid"].split()[0]))
        except (OSError, KeyError, ValueError):
            continue
    return found


def main(
    kill: Callable[[int, int], object] = os.kill,
    processes: Callable[[], dict[int, tuple[int, int]]] = read_processes,
    sleep: Callable[[float], object] = time.sleep,
) -> int:
    """Kill what the steps before left running, until nothing is left, and
    fail when some will not stop.
    """
    stopped = set()
    for _ in range(PASSES):
        found = strays(processes(), os.getpid(), os.getuid())
        if not found:
            break
        for pid in found:
            try:
                kill(pid, signal.SIGKILL)
                stopped.add(pid)
            except OSError:
                pass
        sleep(PASS_WAIT)
    else:
        if left := strays(processes(), os.getpid(), os.getuid()):
            print(f"::error::{len(left)} process(es) left from the steps before would not stop.")
            return 1
    print(f"Stopped {len(stopped)} process(es) left from the steps before." if stopped else "Nothing was left running.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
