"""Tests for the dispatcher's unit: nothing in it gives the service a
namespace of its own, which the account's podman pause process would keep.
"""

import re
from pathlib import Path

import pytest

UNIT = Path(__file__).parents[2] / "templates" / "dispatcher.service.j2"

# Each of these makes systemd give the service a namespace of its own, a
# mount namespace for most.
NAMESPACE_SETTINGS = (
    "PrivateTmp",
    "PrivateDevices",
    "PrivateMounts",
    "PrivateUsers",
    "PrivateIPC",
    "PrivateNetwork",
    "ProtectSystem",
    "ProtectHome",
    "ProtectKernelTunables",
    "ProtectKernelModules",
    "ProtectKernelLogs",
    "ProtectControlGroups",
    "ProtectClock",
    "ProtectHostname",
    "ProtectProc",
    "ProcSubset",
    "ReadOnlyPaths",
    "ReadWritePaths",
    "InaccessiblePaths",
    "ExecPaths",
    "NoExecPaths",
    "TemporaryFileSystem",
    "BindPaths",
    "BindReadOnlyPaths",
    "MountImages",
    "RootDirectory",
    "RootImage",
    "MountFlags",
    "JoinsNamespaceOf",
    "NetworkNamespacePath",
)


def settings() -> list[str]:
    """The names of the unit's settings, comments and template tags left out."""
    lines = UNIT.read_text().splitlines()
    return [match.group(1) for line in lines if (match := re.match(r"\s*([A-Za-z]+)\s*=", line))]


def test_unit_has_settings() -> None:
    assert "ExecStart" in settings()


@pytest.mark.parametrize("setting", NAMESPACE_SETTINGS)
def test_unit_gives_no_mount_namespace(setting: str) -> None:
    assert setting not in settings()
