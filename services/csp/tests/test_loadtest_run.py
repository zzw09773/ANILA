"""loadtest/run.sh must not widen the JWT private key, and must clean up on exit."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import tempfile
import textwrap
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "tools" / "loadtest" / "run.sh"


def _run(body: str, **env: str) -> subprocess.CompletedProcess[str]:
    merged = os.environ.copy()
    merged.update(env)
    merged["SCRIPT"] = str(SCRIPT)
    return subprocess.run(
        ["bash", "-c", textwrap.dedent(body)],
        env=merged,
        capture_output=True,
        text=True,
        check=False,
    )


def test_chown_failure_keeps_the_private_key_at_0600(tmp_path: Path):
    private = tmp_path / "jwt-private.pem"
    public = tmp_path / "jwt-public.pem"
    private.write_bytes(b"secret")
    public.write_bytes(b"public")
    private.chmod(0o644)
    proc = _run(
        """
        set -euo pipefail
        if ! grep -q 'install_loadtest_jwt()' "$SCRIPT"; then
          echo "install_loadtest_jwt 不存在" >&2
          exit 1
        fi
        source "$SCRIPT"
        chown() { return 1; }
        install_loadtest_jwt "$JWT"
        """,
        JWT=str(tmp_path),
    )
    mode = stat.S_IMODE(private.stat().st_mode)
    assert proc.returncode != 0, proc.stderr
    assert mode == 0o600, (oct(mode), proc.stdout, proc.stderr)
    assert "0600" in proc.stderr


def test_cleanup_removes_volumes_and_only_keys_this_run_created(tmp_path: Path):
    jwt = tmp_path / "jwt"
    jwt.mkdir()
    kept_private = jwt / "jwt-private.pem"
    kept_private.write_bytes(b"preexisting")
    extra = jwt / "notes.txt"
    extra.write_bytes(b"keep")
    created_public = jwt / "jwt-public.pem"
    created_public.write_bytes(b"new")
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"outside")
    manifest = tmp_path / "created.txt"
    manifest.write_text(f"{created_public}\n{outside}\n", encoding="utf-8")
    empty = tmp_path / "empty-jwt"
    empty.mkdir()
    only = empty / "jwt-private.pem"
    only.write_bytes(b"new")
    manifest_only = tmp_path / "created-only.txt"
    manifest_only.write_text(f"{only}\n", encoding="utf-8")
    log = tmp_path / "docker.log"
    proc = _run(
        """
        set -euo pipefail
        if ! grep -q 'cleanup_loadtest()' "$SCRIPT"; then
          echo "cleanup_loadtest 不存在" >&2
          exit 1
        fi
        source "$SCRIPT"
        docker() { printf '%s\\n' "$*" >> "$LOG"; }
        cleanup_loadtest "$ROOT" "$JWT" "$CREATED"
        cleanup_loadtest "$ROOT" "$EMPTY" "$CREATED_ONLY"
        """,
        ROOT=str(tmp_path),
        JWT=str(jwt),
        CREATED=str(manifest),
        EMPTY=str(empty),
        CREATED_ONLY=str(manifest_only),
        LOG=str(log),
    )
    assert proc.returncode == 0, proc.stderr
    text = log.read_text(encoding="utf-8")
    volume_cmds = [line for line in text.splitlines() if "--volumes" in line and "down" in line]
    assert volume_cmds, text
    for line in volume_cmds:
        assert "-p anila-loadtest" in line, line
        assert "anila-platform-dev" not in line
    assert kept_private.read_bytes() == b"preexisting"
    assert extra.read_bytes() == b"keep"
    assert jwt.is_dir()
    assert not created_public.exists()
    assert outside.read_bytes() == b"outside"
    assert not manifest.exists()
    assert not empty.exists()
    assert not only.exists()
    assert "拒絕刪除" in proc.stderr


def test_existing_private_key_is_reused_and_not_listed_for_deletion(tmp_path: Path):
    private = tmp_path / "jwt-private.pem"
    private.write_bytes(b"keep-me")
    manifest = tmp_path / "created.txt"
    manifest.write_text("", encoding="utf-8")
    proc = _run(
        """
        set -euo pipefail
        source "$SCRIPT"
        chown() { return 1; }
        install_loadtest_jwt "$JWT" "$CREATED"
        """,
        JWT=str(tmp_path),
        CREATED=str(manifest),
    )
    assert proc.returncode != 0, proc.stderr
    assert private.read_bytes() == b"keep-me"
    assert manifest.read_text(encoding="utf-8") == ""


def _isolated_loadtest_tree(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    tools = root / "tools" / "loadtest"
    tools.mkdir(parents=True)
    script = tools / "run.sh"
    shutil.copy(SCRIPT, script)
    script.chmod(0o755)
    return root


def test_script_exit_removes_only_keys_this_run_created():
    # /tmp is noexec, so the fake repo and stand-ins live on a normal
    # filesystem. Cleanup removes only this unique directory.
    workspace = Path(tempfile.mkdtemp(prefix="anila-loadtest-", dir=str(REPO.parent)))
    assert workspace.name.startswith("anila-loadtest-")
    assert workspace.resolve() != REPO.resolve()
    root = _isolated_loadtest_tree(workspace)
    script = root / "tools" / "loadtest" / "run.sh"
    bindir = workspace / "bin"
    bindir.mkdir()
    docker_log = workspace / "docker.log"
    chmod_log = workspace / "chmod.log"
    (bindir / "docker").write_text(
        "#!/bin/sh\n"
        'printf \'%s\\n\' "$*" >> "$LOADTEST_DOCKER_LOG"\n'
        'case " $* " in\n'
        '  *" up "*) exit 1 ;;\n'
        "esac\n"
        "exit 0\n",
        encoding="utf-8",
    )
    (bindir / "openssl").write_text(
        "#!/bin/sh\n"
        "out=\n"
        "prev=\n"
        'for arg in "$@"; do\n'
        '  if [ "$prev" = "-out" ]; then out=$arg; fi\n'
        "  prev=$arg\n"
        "done\n"
        'if [ -n "$out" ]; then printf \'%s\\n\' stub > "$out"; fi\n'
        "exit 0\n",
        encoding="utf-8",
    )
    (bindir / "chown").write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    (bindir / "chmod").write_text(
        "#!/bin/sh\n"
        'printf \'%s\\n\' "$*" >> "$LOADTEST_CHMOD_LOG"\n'
        'exec /bin/chmod "$@"\n',
        encoding="utf-8",
    )
    for name in ("docker", "openssl", "chown", "chmod"):
        (bindir / name).chmod(0o755)
    jwt = root / "tools" / "loadtest" / ".jwt"
    jwt.mkdir()
    notes = jwt / "notes.txt"
    notes.write_text("keep-me", encoding="utf-8")
    keep = root / "tools" / "loadtest" / "keep.txt"
    keep.write_text("keep", encoding="utf-8")
    repo_sidecars = {
        "jwt": (REPO / "tools" / "loadtest" / ".jwt").exists(),
        "bin": (REPO / "tools" / "loadtest" / ".test-bin").exists(),
    }
    env = os.environ.copy()
    env["PATH"] = f"{bindir}:{env.get('PATH', '')}"
    env["LOADTEST_DOCKER_LOG"] = str(docker_log)
    env["LOADTEST_CHMOD_LOG"] = str(chmod_log)
    try:
        proc = subprocess.run(
            ["timeout", "20", "bash", str(script)],
            env=env,
            capture_output=True,
            text=True,
            check=False,
            cwd=str(root),
        )
        assert proc.returncode != 0, proc.stdout
        assert "0600" in proc.stderr, proc.stderr
        private_chmods = [
            line
            for line in chmod_log.read_text(encoding="utf-8").splitlines()
            if "jwt-private.pem" in line
        ]
        assert private_chmods, chmod_log.read_text(encoding="utf-8")
        assert all("644" not in line for line in private_chmods), private_chmods
        assert any(line.split()[0] == "600" for line in private_chmods), private_chmods
        text = docker_log.read_text(encoding="utf-8")
        volume_cmds = [line for line in text.splitlines() if "--volumes" in line and "down" in line]
        assert volume_cmds, text
        for line in volume_cmds:
            assert "-p anila-loadtest" in line, line
            assert "anila-platform-dev" not in line
        assert notes.read_text(encoding="utf-8") == "keep-me"
        assert keep.read_text(encoding="utf-8") == "keep"
        assert jwt.is_dir()
        assert not (jwt / "jwt-private.pem").exists()
        assert not (jwt / "jwt-public.pem").exists()
    finally:
        shutil.rmtree(workspace, ignore_errors=True)
        assert not workspace.exists()
        assert (REPO / "tools" / "loadtest" / ".jwt").exists() == repo_sidecars["jwt"]
        assert (REPO / "tools" / "loadtest" / ".test-bin").exists() == repo_sidecars["bin"]
