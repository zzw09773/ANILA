"""UID/GID isolation assertions for the sandbox container.

These tests must run **inside** a built ``anila-functions-sandbox``
container (or via ``docker compose exec sandbox-exec pytest ...``),
not from the host. They verify the runtime invariants from spec §5.8:

* daemon runs as ``sandbox`` (uid 65533), in ``anila-jobs`` group
* user-code subprocess runs as ``subproc`` (uid 65534), NOT in
  ``anila-jobs``
* subprocess cannot connect to ``/jobs-*/control.sock``
* subprocess cannot ``listdir`` / ``open`` the jobs dir
* subprocess ``setuid(0)`` raises ``PermissionError``

If any of these fail at deploy time, the network and capability
isolation is broken — STOP and re-investigate the entrypoint setpriv
chain before opening the worker stack to real Function authors.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest


JOBS_DIR = Path(os.environ.get("JOBS_DIR", "/jobs-exec"))
SOCKET = JOBS_DIR / "control.sock"


# ── Daemon-side checks (run as sandbox uid) ─────────────────────────────


@pytest.mark.skipif(
    os.geteuid() != 65533, reason="must run as sandbox uid (65533)"
)
def test_daemon_uid_is_65533() -> None:
    assert os.getuid() == 65533


@pytest.mark.skipif(
    os.geteuid() != 65533, reason="must run as sandbox uid (65533)"
)
def test_daemon_in_anila_jobs_group() -> None:
    """Sandbox uid is in `anila-jobs` (gid 65530) supplementary group."""
    assert 65530 in os.getgroups()


# ── Subprocess-side checks (run as subproc uid) ─────────────────────────


@pytest.mark.skipif(
    os.geteuid() != 65534, reason="must run as subproc uid (65534)"
)
def test_subprocess_uid_is_65534() -> None:
    assert os.getuid() == 65534


@pytest.mark.skipif(
    os.geteuid() != 65534, reason="must run as subproc uid (65534)"
)
def test_subprocess_only_in_own_group() -> None:
    """`subproc` user is NOT in `anila-jobs` — that's the linchpin of
    the IPC isolation. If this fires, audit the Dockerfile useradd."""
    assert os.getgroups() == [65534]


@pytest.mark.skipif(
    os.geteuid() != 65534, reason="must run as subproc uid (65534)"
)
def test_subprocess_cannot_setuid_zero() -> None:
    """no-new-privileges + cap_drop:ALL must prevent privilege re-acquisition."""
    with pytest.raises(PermissionError):
        os.setuid(0)


@pytest.mark.skipif(
    os.geteuid() != 65534, reason="must run as subproc uid (65534)"
)
def test_subprocess_cannot_connect_control_sock() -> None:
    """User code must not be able to talk to the IPC channel directly."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    with pytest.raises(PermissionError):
        sock.connect(str(SOCKET))


@pytest.mark.skipif(
    os.geteuid() != 65534, reason="must run as subproc uid (65534)"
)
def test_subprocess_cannot_listdir_jobs_dir() -> None:
    with pytest.raises(PermissionError):
        os.listdir(JOBS_DIR)


@pytest.mark.skipif(
    os.geteuid() != 65534, reason="must run as subproc uid (65534)"
)
def test_subprocess_cannot_open_socket_file() -> None:
    with pytest.raises(PermissionError):
        with open(SOCKET, "rb"):
            pass


# ── Capability inspection (run as sandbox uid) ──────────────────────────


@pytest.mark.skipif(
    os.geteuid() != 65533, reason="must run as sandbox uid (65533)"
)
def test_daemon_has_setuid_setgid_in_effective_set() -> None:
    """Daemon needs CAP_SETUID + CAP_SETGID effective so Popen(user=...)
    can drop privileges. capsh --print is the cleanest way to read.
    """
    out = subprocess.run(
        ["capsh", "--print"], capture_output=True, text=True, check=True
    )
    eff_line = next(
        (
            line
            for line in out.stdout.splitlines()
            if line.startswith("Current:")
        ),
        "",
    )
    # Format: "Current: cap_setuid,cap_setgid=eip"
    assert "cap_setuid" in eff_line
    assert "cap_setgid" in eff_line


@pytest.mark.skipif(
    os.geteuid() != 65533, reason="must run as sandbox uid (65533)"
)
def test_daemon_does_not_have_chown() -> None:
    """CHOWN was used by entrypoint to chown the volume; setpriv must
    drop it before exec'ing the daemon. If CHOWN leaked through, the
    daemon could re-chown the IPC dir and break the isolation model.
    """
    out = subprocess.run(
        ["capsh", "--print"], capture_output=True, text=True, check=True
    )
    eff_line = next(
        (
            line
            for line in out.stdout.splitlines()
            if line.startswith("Current:")
        ),
        "",
    )
    # CHOWN may still appear in Bounding (container cap_add) but must
    # NOT appear in Current (effective).
    assert "cap_chown" not in eff_line
