"""run.sh and build.sh behavior that the lab image depends on."""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

PKG = Path(__file__).resolve().parents[1]
REPO = PKG.parents[1]
CANONICAL = (
    REPO / "packages" / "anila-core" / "src" / "anila_core" / "contrib" / "anila_verify.py"
)
RUNTIME = ("server.py", "agent.py", "platform_io.py", "llm.py")
LOG_CAP = int(
    re.search(r"^MAX_LOG_BYTES=(\d+)", (PKG / "run.sh").read_text(encoding="utf-8"), re.M).group(1)
)


def _port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _write_ca(path: Path) -> None:
    import datetime as dt

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "run-sh-ca")])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(now)
        .not_valid_after(now + dt.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))


def _env_file(root: Path, **overrides: str | None) -> str:
    values: dict[str, str | None] = {
        "CSP_BASE_URL": "https://127.0.0.1:9",
        "ANILA_CA_FILE": str(root / "ca.pem"),
        "ANILA_AGENT_ID": "42",
        "LLM_BASE_URL": "https://127.0.0.1:9/v1",
        "LLM_MODEL": "smoke-model",
        "LLM_AUTH_REQUIRED": "false",
    }
    values.update(overrides)
    lines = [f"{key}={value}" for key, value in values.items() if value is not None]
    return "\n".join(lines) + "\n"


def _install_script(root: Path, env_text: str, *, server: str | None = None, real_app: bool = False) -> Path:
    script = root / "run.sh"
    shutil.copy(PKG / "run.sh", script)
    script.chmod(0o755)
    (root / "deployment.env").write_text(env_text, encoding="utf-8")
    (root / "bundle.json").write_text(
        json.dumps({"compatible_lab_image_version": "1"}) + "\n", encoding="utf-8"
    )
    if real_app:
        _write_ca(root / "ca.pem")
        for name in RUNTIME:
            shutil.copy(PKG / name, root / name)
        shutil.copy(CANONICAL, root / "anila_verify.py")
    elif server is not None:
        (root / "server.py").write_text(server, encoding="utf-8")
    return script


def _run_env(port: int) -> dict[str, str]:
    env = os.environ.copy()
    env["ANILA_LAB_IMAGE_VERSION"] = "1"
    env["AGENT_PORT"] = str(port)
    return env


def _run(script: Path, *args: str, env: dict[str, str], timeout: float = 20) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(script), *args],
        cwd=script.parent,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _stop(script: Path, env: dict[str, str]) -> None:
    subprocess.run(
        ["bash", str(script), "stop"],
        cwd=script.parent,
        env=env,
        capture_output=True,
        text=True,
        timeout=40,
        check=False,
    )


def _supervisor_pid(stdout: str) -> int:
    match = re.search(r"supervisor (\d+)", stdout)
    assert match, stdout
    return int(match.group(1))


def _cmdline(pid: int) -> str:
    raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    return raw.replace(b"\0", b" ").decode("utf-8", "replace")


def test_stale_supervisor_pid_is_not_a_foreign_process(tmp_path: Path):
    port = _port()
    script = _install_script(
        tmp_path,
        _env_file(tmp_path),
        server="from fastapi import FastAPI\napp = FastAPI()\n",
    )
    env = _run_env(port)
    sleeper = subprocess.Popen(["sleep", "300"])
    try:
        pidfile = tmp_path / ".anila-agent.pid"
        pidfile.write_text(f"{sleeper.pid}\n", encoding="utf-8")
        status = _run(script, "status", env=env)
        assert status.returncode == 3
        assert "status: stopped" in status.stdout
        assert "running" not in status.stdout
        assert not pidfile.exists()
        assert sleeper.poll() is None

        pidfile.write_text(f"{sleeper.pid}\n", encoding="utf-8")
        stopped = _run(script, "stop", env=env)
        assert stopped.returncode == 0
        assert "status: stopped" in stopped.stdout
        assert "forced" not in stopped.stdout
        assert sleeper.poll() is None
        assert not pidfile.exists()

        pidfile.write_text(f"{sleeper.pid}\n", encoding="utf-8")
        started = _run(script, "start", env=env, timeout=30)
        assert started.returncode == 0, started.stderr
        supervisor = _supervisor_pid(started.stdout)
        assert supervisor != sleeper.pid
        assert "run.sh" in _cmdline(supervisor)
        assert "_supervise" in _cmdline(supervisor)
        assert sleeper.poll() is None
    finally:
        _stop(script, env)
        if sleeper.poll() is None:
            sleeper.kill()
            sleeper.wait(timeout=5)


def _health(port: int, deadline_s: float = 20) -> tuple[int | None, bytes]:
    deadline = time.time() + deadline_s
    body = b""
    code = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/health", timeout=2
            ) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            time.sleep(0.2)
    return code, body


def test_unregistered_start_reports_not_registered(tmp_path: Path):
    """Empty ANILA_AGENT_ID still starts. Health and status name that reason."""
    port = _port()
    script = _install_script(
        tmp_path,
        _env_file(tmp_path, ANILA_AGENT_ID="", LLM_AUTH_REQUIRED="true"),
        real_app=True,
    )
    env = _run_env(port)
    env["LLM_API_KEY"] = "sk-lab-only"
    try:
        started = _run(script, "start", env=env, timeout=30)
        assert started.returncode == 0, started.stderr
        assert "status: running" in started.stdout
        code, body = _health(port)
        assert code == 503, (code, body)
        payload = json.loads(body)
        assert payload["status"] == "not_registered"
        assert payload["reason"] == "not_registered"
        assert "ANILA_AGENT_ID" in payload["hint"]
        status = _run(script, "status", env=env, timeout=20)
        assert status.returncode == 0, status.stderr
        assert "reason: not_registered" in status.stdout
        assert "hint:" in status.stdout
        assert "ANILA_AGENT_ID" in status.stdout
    finally:
        _stop(script, env)


def test_missing_llm_key_reports_llm_not_configured(tmp_path: Path):
    port = _port()
    script = _install_script(
        tmp_path,
        _env_file(tmp_path, LLM_AUTH_REQUIRED="true"),
        real_app=True,
    )
    env = _run_env(port)
    env.pop("LLM_API_KEY", None)
    try:
        started = _run(script, "start", env=env, timeout=30)
        assert started.returncode == 0, started.stderr
        code, body = _health(port)
        assert code == 503, (code, body)
        payload = json.loads(body)
        assert payload["reason"] == "llm_not_configured"
        assert "LLM_API_KEY" in payload["hint"]
        status = _run(script, "status", env=env, timeout=20)
        assert "reason: llm_not_configured" in status.stdout
    finally:
        _stop(script, env)


def test_blank_model_and_agent_id_do_not_block_start(tmp_path: Path):
    port = _port()
    script = _install_script(
        tmp_path,
        _env_file(tmp_path, ANILA_AGENT_ID="", LLM_MODEL=""),
        server="from fastapi import FastAPI\napp = FastAPI()\n",
    )
    env = _run_env(port)
    try:
        started = _run(script, "start", env=env, timeout=30)
        assert started.returncode == 0, started.stderr
        assert "拒絕啟動" not in started.stderr
        assert "status: running" in started.stdout
    finally:
        _stop(script, env)


def test_restart_replaces_the_supervisor(tmp_path: Path):
    port = _port()
    script = _install_script(
        tmp_path,
        _env_file(tmp_path),
        server="from fastapi import FastAPI\napp = FastAPI()\n",
    )
    env = _run_env(port)
    try:
        started = _run(script, "start", env=env, timeout=30)
        assert started.returncode == 0, started.stderr
        first = _supervisor_pid(started.stdout)
        restarted = _run(script, "restart", env=env, timeout=40)
        assert restarted.returncode == 0, restarted.stderr
        assert "status: stopped" in restarted.stdout
        assert "status: running" in restarted.stdout
        assert _supervisor_pid(restarted.stdout) != first
    finally:
        _stop(script, env)


@pytest.mark.parametrize("missing", ["CSP_BASE_URL", "ANILA_CA_FILE", "LLM_BASE_URL", "LLM_AUTH_REQUIRED"])
def test_start_refuses_other_missing_required_fields(tmp_path: Path, missing: str):
    port = _port()
    script = _install_script(tmp_path, _env_file(tmp_path, **{missing: None}))
    result = _run(script, "start", env=_run_env(port))
    assert result.returncode != 0
    assert missing in result.stderr
    assert "status: running" not in result.stdout
    assert not (tmp_path / ".anila-agent.pid").exists()


def test_single_log_line_larger_than_the_cap_is_truncated(tmp_path: Path):
    port = _port()
    huge = LOG_CAP + 4096
    script = _install_script(
        tmp_path,
        _env_file(tmp_path),
        server=(
            "print('X' * "
            f"{huge}"
            ", flush=True)\n"
            "from fastapi import FastAPI\n"
            "app = FastAPI()\n"
        ),
    )
    env = _run_env(port)
    log = tmp_path / "anila-agent.log"
    try:
        started = _run(script, "start", env=env, timeout=30)
        assert started.returncode == 0, started.stderr
        deadline = time.time() + 20
        size = None
        while time.time() < deadline:
            if log.is_file():
                data = log.read_bytes()
                size = len(data)
                if b"X" * 1000 in data and size <= LOG_CAP:
                    break
            time.sleep(0.1)
        else:
            raise AssertionError(f"log did not settle under the cap (size={size})")
        time.sleep(0.4)
        data = log.read_bytes()
        assert len(data) <= LOG_CAP
        assert b"X" * (LOG_CAP + 1) not in data
        assert data.endswith(b"\n")
    finally:
        _stop(script, env)


def test_build_sh_pins_linux_amd64_and_rejects_other_hosts(tmp_path: Path):
    text = (PKG / "mlsteam" / "build.sh").read_text(encoding="utf-8")
    logical = text.replace("\\\n", " ")
    docker_lines = [
        line
        for line in logical.splitlines()
        if "docker run " in line or line.strip().startswith("docker build")
    ]
    assert any("docker run " in line and "--platform=linux/amd64" in line for line in docker_lines)
    assert any(line.strip().startswith("docker build") and "--platform=linux/amd64" in line for line in docker_lines)
    assert '!= "linux/amd64"' in text

    # pytest's tmp is on noexec /tmp, so the stand-in uname has to live elsewhere.
    bin_dir = Path("/dev/shm") / f"anila-build-sh-test-{os.getpid()}"
    bin_dir.mkdir()
    uname = bin_dir / "uname"
    uname.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "-m" ]; then echo aarch64; exit 0; fi\n'
        'exec /usr/bin/uname "$@"\n',
        encoding="utf-8",
    )
    uname.chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    try:
        result = subprocess.run(
            ["bash", str(PKG / "mlsteam" / "build.sh")],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    finally:
        shutil.rmtree(bin_dir, ignore_errors=True)
    assert result.returncode != 0
    assert "refusing to build" in result.stderr
    assert "aarch64" in result.stderr
    assert "linux/amd64" in result.stderr
    assert "building" not in result.stdout
