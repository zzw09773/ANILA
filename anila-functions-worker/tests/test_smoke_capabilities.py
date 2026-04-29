"""Sprint 2.5 prototype gate — capability landing smoke tests.

Run inside ``anila-functions-sandbox-exec`` / ``-extract`` containers
after ``docker compose up``. All 6 tests must pass before Sprint 3
implementation begins (see spec §5.8 + plan Sprint 2.5).

Invocation::

    docker compose exec anila-functions-sandbox-exec \\
        pytest /app/tests/test_smoke_capabilities.py -v
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(
    os.geteuid() != 65533, reason="run as sandbox uid (entrypoint dropped)"
)
def test_smoke_1_bounding_set_has_setuid_setgid_chown() -> None:
    """Container's bounding set retains the three caps we cap_added."""
    out = subprocess.run(
        ["capsh", "--print"], capture_output=True, text=True, check=True
    )
    bounding = next(
        line for line in out.stdout.splitlines() if line.startswith("Bounding")
    )
    assert "cap_setuid" in bounding
    assert "cap_setgid" in bounding
    assert "cap_chown" in bounding


@pytest.mark.skipif(
    os.geteuid() != 65533, reason="run as sandbox uid"
)
def test_smoke_2_daemon_has_setuid_setgid_effective() -> None:
    out = subprocess.run(
        ["capsh", "--print"], capture_output=True, text=True, check=True
    )
    current = next(
        line for line in out.stdout.splitlines() if line.startswith("Current:")
    )
    assert "cap_setuid" in current
    assert "cap_setgid" in current


@pytest.mark.skipif(
    os.geteuid() != 65533, reason="run as sandbox uid"
)
def test_smoke_3_can_spawn_subprocess_as_subproc() -> None:
    """Daemon can drop privileges to subproc(65534)."""
    proc = subprocess.run(
        ["python3", "-c", "import os; print(os.getuid())"],
        user=65534,
        group=65534,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, f"spawn failed: {proc.stderr}"
    assert proc.stdout.strip() == "65534"


@pytest.mark.skipif(
    os.geteuid() != 65534, reason="run as subproc uid"
)
def test_smoke_4_subprocess_cannot_setuid_zero() -> None:
    with pytest.raises(PermissionError):
        os.setuid(0)


@pytest.mark.skipif(
    os.geteuid() != 65534, reason="run as subproc uid"
)
def test_smoke_5_subprocess_cannot_connect_control_sock() -> None:
    import socket

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    socket_path = str(Path(os.environ["JOBS_DIR"]) / "control.sock")
    with pytest.raises(PermissionError):
        sock.connect(socket_path)


@pytest.mark.skipif(
    os.geteuid() != 65534, reason="run as subproc uid"
)
def test_smoke_6_subprocess_cannot_listdir_jobs() -> None:
    with pytest.raises(PermissionError):
        os.listdir(os.environ["JOBS_DIR"])
