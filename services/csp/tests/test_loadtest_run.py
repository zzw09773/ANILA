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
    """Private key that already exists is not recorded. A missing public key is."""
    jwt = tmp_path / "jwt"
    jwt.mkdir()
    jwt.chmod(0o755)
    private = jwt / "jwt-private.pem"
    private.write_bytes(b"keep-me")
    manifest = tmp_path / "created.txt"
    manifest.write_text("", encoding="utf-8")
    proc = _run(
        """
        set -euo pipefail
        source "$SCRIPT"
        openssl() {
          local out="" prev=""
          for arg in "$@"; do
            if [ "$prev" = "-out" ]; then out=$arg; fi
            prev=$arg
          done
          if [ -n "$out" ]; then printf 'public\\n' > "$out"; fi
        }
        chown() { return 0; }
        install_loadtest_jwt "$JWT" "$CREATED"
        """,
        JWT=str(jwt),
        CREATED=str(manifest),
    )
    assert proc.returncode == 0, proc.stderr
    assert private.read_bytes() == b"keep-me"
    public = jwt / "jwt-public.pem"
    assert public.read_bytes() == b"public\n"
    assert manifest.read_text(encoding="utf-8").splitlines() == [str(public)]
    assert stat.S_IMODE(jwt.stat().st_mode) == 0o770


def test_created_manifest_lists_private_and_public_separately(tmp_path: Path):
    jwt = tmp_path / "jwt"
    jwt.mkdir()
    jwt.chmod(0o755)
    manifest = tmp_path / "created.txt"
    proc = _run(
        """
        set -euo pipefail
        source "$SCRIPT"
        openssl() {
          local out="" prev=""
          for arg in "$@"; do
            if [ "$prev" = "-out" ]; then out=$arg; fi
            prev=$arg
          done
          if [ -n "$out" ]; then printf 'made\\n' > "$out"; fi
        }
        chown() { return 0; }
        install_loadtest_jwt "$JWT" "$CREATED"
        """,
        JWT=str(jwt),
        CREATED=str(manifest),
    )
    assert proc.returncode == 0, proc.stderr
    private = jwt / "jwt-private.pem"
    public = jwt / "jwt-public.pem"
    assert manifest.read_text(encoding="utf-8").splitlines() == [str(private), str(public)]
    assert stat.S_IMODE(jwt.stat().st_mode) == 0o770


def test_cleanup_matches_key_paths_by_string_equality(tmp_path: Path):
    jwt = tmp_path / "keys*"
    jwt.mkdir()
    real = jwt / "jwt-private.pem"
    real.write_bytes(b"real")
    decoy_dir = tmp_path / "keysX"
    decoy_dir.mkdir()
    decoy = decoy_dir / "jwt-private.pem"
    decoy.write_bytes(b"decoy")
    manifest = tmp_path / "created.txt"
    manifest.write_text(f"{decoy}\n{real}\n", encoding="utf-8")
    proc = _run(
        """
        set -euo pipefail
        source "$SCRIPT"
        _remove_created_keys "$JWT" "$CREATED"
        """,
        JWT=str(jwt),
        CREATED=str(manifest),
    )
    assert proc.returncode == 0, proc.stderr
    assert decoy.read_bytes() == b"decoy"
    assert not real.exists()
    assert "拒絕刪除" in proc.stderr
    body = SCRIPT.read_text(encoding="utf-8")
    start = body.index("_remove_created_keys()")
    end = body.index("\ninstall_loadtest_jwt()", start)
    fn = body[start:end]
    assert "case " not in fn
    assert '[ "$path" = "$private" ]' in fn
    assert '[ "$path" = "$public" ]' in fn


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


def test_secrets_directory_is_chowned_to_the_container_user(tmp_path: Path):
    """目錄 0770、擁有者 10001、群組是執行者，容器才進得去，執行者也刪得了檔。"""
    jwt = tmp_path / "jwt"
    jwt.mkdir()
    jwt.chmod(0o755)
    chown_log = tmp_path / "chown.log"
    chmod_log = tmp_path / "chmod.log"
    proc = _run(
        """
        set -euo pipefail
        source "$SCRIPT"
        chmod() { printf '%s\\n' "$*" >> "$CHMOD_LOG"; command chmod "$@"; }
        openssl() {
          local out="" prev=""
          for arg in "$@"; do
            if [ "$prev" = "-out" ]; then out=$arg; fi
            prev=$arg
          done
          if [ -n "$out" ]; then printf 'made\\n' > "$out"; fi
        }
        chown() { printf '%s\\n' "$*" >> "$CHOWN_LOG"; return 0; }
        install_loadtest_jwt "$JWT" "$CREATED"
        """,
        JWT=str(jwt),
        CREATED=str(tmp_path / "created.txt"),
        CHOWN_LOG=str(chown_log),
        CHMOD_LOG=str(chmod_log),
    )
    assert proc.returncode == 0, proc.stderr
    assert stat.S_IMODE(jwt.stat().st_mode) == 0o770
    chown_lines = chown_log.read_text(encoding="utf-8").splitlines()
    assert chown_lines, "沒有呼叫 chown"
    gid = str(os.getgid())
    assert any(
        line.split()[:1] == [f"10001:{gid}"] and str(jwt) in line.split()
        for line in chown_lines
    ), chown_lines
    private = str(jwt / "jwt-private.pem")
    private_lines = [line for line in chown_lines if private in line.split()]
    assert private_lines
    assert all(line.split()[0] == f"10001:{gid}" for line in private_lines)
    for line in chmod_log.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if str(jwt) in parts:
            assert parts[0] in {"770", "0770"}, line


def test_chown_failure_does_not_widen_the_secrets_directory(tmp_path: Path):
    jwt = tmp_path / "jwt"
    jwt.mkdir()
    jwt.chmod(0o755)
    chmod_log = tmp_path / "chmod.log"
    proc = _run(
        """
        set -euo pipefail
        source "$SCRIPT"
        chmod() { printf '%s\\n' "$*" >> "$CHMOD_LOG"; command chmod "$@"; }
        openssl() {
          local out="" prev=""
          for arg in "$@"; do
            if [ "$prev" = "-out" ]; then out=$arg; fi
            prev=$arg
          done
          if [ -n "$out" ]; then printf 'made\\n' > "$out"; fi
        }
        chown() { return 1; }
        install_loadtest_jwt "$JWT"
        """,
        JWT=str(jwt),
        CHMOD_LOG=str(chmod_log),
    )
    assert proc.returncode != 0, proc.stderr
    assert "0600" in proc.stderr
    assert stat.S_IMODE(jwt.stat().st_mode) == 0o770
    private = jwt / "jwt-private.pem"
    assert stat.S_IMODE(private.stat().st_mode) == 0o600
    for line in chmod_log.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if str(jwt) in parts:
            assert parts[0] not in {"755", "775", "777", "711"}, line


def test_new_private_key_regenerates_a_stale_public_key(tmp_path: Path):
    """私鑰是這次新產生的時候，留下的舊公鑰必須重算，清理仍只刪這次建立的檔。"""
    jwt = tmp_path / "jwt"
    jwt.mkdir()
    public = jwt / "jwt-public.pem"
    public.write_bytes(b"stale-public")
    manifest = tmp_path / "created.txt"
    openssl_log = tmp_path / "openssl.log"
    proc = _run(
        """
        set -euo pipefail
        source "$SCRIPT"
        openssl() {
          printf '%s\\n' "$*" >> "$OPENSSL_LOG"
          local out="" prev=""
          for arg in "$@"; do
            if [ "$prev" = "-out" ]; then out=$arg; fi
            prev=$arg
          done
          if [ -z "$out" ]; then return 0; fi
          case "$out" in
            *jwt-private.pem) printf 'new-private\\n' > "$out" ;;
            *jwt-public.pem) printf 'new-public\\n' > "$out" ;;
          esac
        }
        chown() { return 0; }
        docker() { return 0; }
        install_loadtest_jwt "$JWT" "$CREATED"
        cleanup_loadtest "$ROOT" "$JWT" "$CREATED"
        """,
        JWT=str(jwt),
        CREATED=str(manifest),
        OPENSSL_LOG=str(openssl_log),
        ROOT=str(tmp_path),
    )
    assert proc.returncode == 0, proc.stderr
    private = jwt / "jwt-private.pem"
    assert not private.exists()
    assert public.read_bytes() == b"new-public\n"
    commands = openssl_log.read_text(encoding="utf-8")
    assert "pubout" in commands
    assert "jwt-private.pem" in commands
    assert not manifest.exists()


def test_repeat_install_continues_when_directory_is_already_shared_with_the_caller(tmp_path: Path):
    """目錄已屬 10001 時 chmod 會 EPERM。0770 且群組是執行者就繼續，不改成更寬。"""
    jwt = tmp_path / "jwt"
    jwt.mkdir()
    (jwt / "jwt-private.pem").write_bytes(b"keep")
    (jwt / "jwt-public.pem").write_bytes(b"keep-pub")
    proc = _run(
        """
        set -euo pipefail
        source "$SCRIPT"
        chmod() { return 1; }
        chown() { return 1; }
        stat() {
          local fmt="" path=""
          while [ $# -gt 0 ]; do
            case "$1" in
              -c) fmt=$2; shift 2 ;;
              *) path=$1; shift ;;
            esac
          done
          local mode=770
          case "$path" in
            *jwt-private.pem) mode=600 ;;
            *jwt-public.pem) mode=644 ;;
          esac
          case "$fmt" in
            %a) printf '%s\\n' "$mode" ;;
            %u) printf '10001\\n' ;;
            %g) printf '%s\\n' "$GID" ;;
            *) echo "stat $fmt" >&2; return 1 ;;
          esac
        }
        install_loadtest_jwt "$JWT" "$CREATED"
        """,
        JWT=str(jwt),
        CREATED=str(tmp_path / "created.txt"),
        GID=str(os.getgid()),
    )
    assert proc.returncode == 0, proc.stderr
    assert (jwt / "jwt-private.pem").read_bytes() == b"keep"


def test_chmod_failure_rejects_a_wider_directory(tmp_path: Path):
    jwt = tmp_path / "jwt"
    jwt.mkdir()
    proc = _run(
        """
        set -euo pipefail
        source "$SCRIPT"
        chmod() { return 1; }
        stat() {
          local fmt=""
          while [ $# -gt 0 ]; do
            case "$1" in
              -c) fmt=$2; shift 2 ;;
              *) shift ;;
            esac
          done
          case "$fmt" in
            %a) printf '755\\n' ;;
            %u) printf '10001\\n' ;;
            %g) printf '%s\\n' "$GID" ;;
            *) return 1 ;;
          esac
        }
        install_loadtest_jwt "$JWT"
        """,
        JWT=str(jwt),
        GID=str(os.getgid()),
    )
    assert proc.returncode != 0, proc.stdout
    assert "0770" in proc.stderr
    assert "放寬" in proc.stderr


def test_cleanup_unlinks_a_key_the_caller_cannot_open(tmp_path: Path):
    """刪檔只靠目錄的 w+x。檔案 000 打不開，目錄 0770 仍刪得掉。沒有 root 時以此代替 uid 10001。"""
    jwt = tmp_path / "jwt"
    jwt.mkdir()
    jwt.chmod(0o770)
    private = jwt / "jwt-private.pem"
    private.write_bytes(b"secret")
    private.chmod(0o000)
    try:
        private.read_bytes()
        raise AssertionError("mode 000 的金鑰不該讀得到")
    except PermissionError:
        pass
    manifest = tmp_path / "created.txt"
    manifest.write_text(f"{private}\n", encoding="utf-8")
    proc = _run(
        """
        set -euo pipefail
        source "$SCRIPT"
        docker() { return 0; }
        cleanup_loadtest "$ROOT" "$JWT" "$CREATED"
        """,
        ROOT=str(tmp_path),
        JWT=str(jwt),
        CREATED=str(manifest),
    )
    assert proc.returncode == 0, proc.stderr
    assert not private.exists()
