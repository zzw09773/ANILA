"""備份腳本：保留政策，以及寫入時先暫存再改名。"""
from __future__ import annotations

import io
import json
import os
import stat
import subprocess
import tarfile
from pathlib import Path

from app.services.backup_status import parse_backup_status_text

ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = ROOT / "infra" / "deployment" / "scripts"
LOOP = SCRIPTS / "backup-loop.sh"
PRUNE = SCRIPTS / "backup-prune.sh"
RESTORE = SCRIPTS / "restore-all.sh"
OLD = SCRIPTS / "backup-csp-db.sh"

SOURCES = (
    "uploads",
    "attachments",
    "static",
    "pki",
    "quickstart",
    "studio-artifacts",
    "router-sessions",
    "n8n",
)


def _run(script: Path, env: dict, *args: str) -> subprocess.CompletedProcess[str]:
    merged = os.environ.copy()
    for key in (
        "ANILA_BACKUP_FAIL_BEFORE_PUBLISH",
        "ANILA_BACKUP_NOW",
        "ANILA_BACKUP_STAMP",
        "ANILA_BACKUP_MONTH",
        "ANILA_PG_DUMP_CMD",
        "BACKUP_FILE_UID",
        "BACKUP_FILE_GID",
    ):
        merged.pop(key, None)
    merged.update(env)
    return subprocess.run(
        ["sh", str(script), *args],
        env=merged,
        capture_output=True,
        text=True,
        check=False,
    )


def _fake_dump(path: Path) -> None:
    path.write_text(
        """#!/bin/sh
out=$1
case "$out" in
  */.incoming/*/db.dump.partial) ;;
  *)
    printf '%s\\n' "dump path is not temporary: $out" >&2
    exit 2
    ;;
esac
python3 - "$out" << 'PY'
import os, sys
stamp = os.environ["ANILA_BACKUP_STAMP"].encode()
open(sys.argv[1], "wb").write(b"PGDMP" + stamp + b"x" * 2000)
PY
""",
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def _sources(root: Path) -> None:
    for name in SOURCES:
        directory = root / name
        directory.mkdir(parents=True)
        (directory / "marker.txt").write_text(name, encoding="utf-8")
    secret = root / "router-sessions" / "state"
    secret.mkdir()
    (secret / "token").write_text("do-not-backup", encoding="utf-8")
    (root / "router-sessions" / "sessions.db").write_text("sessions", encoding="utf-8")


def _backup_env(tmp: Path, src: Path, dest: Path, fake: Path, **extra: str) -> dict[str, str]:
    env = {
        "ANILA_BACKUP_DIR": str(dest),
        "ANILA_BACKUP_SRC": str(src),
        "ANILA_PG_DUMP_CMD": str(fake),
        "ANILA_BACKUP_MAX_RUNS": "1",
        "ANILA_BACKUP_INTERVAL": "0",
        "ANILA_BACKUP_NOW": "2026-09-27T02:15:00Z",
        "ANILA_BACKUP_STAMP": "20260927-021500",
        "ANILA_BACKUP_MONTH": "202609",
    }
    env.update(extra)
    return env


def test_prune_keeps_14_daily_and_6_monthly(tmp_path: Path):
    daily = tmp_path / "daily"
    monthly = tmp_path / "monthly"
    daily.mkdir()
    monthly.mkdir()
    names = [f"2026{(i // 28) + 1:02d}{(i % 28) + 1:02d}-030000" for i in range(20)]
    for name in names:
        directory = daily / name
        directory.mkdir()
        (directory / "db.dump").write_bytes(b"x" * 1000)
    months = [f"2026{m:02d}" for m in range(1, 9)]
    for name in months:
        directory = monthly / name
        directory.mkdir()
        (directory / "db.dump").write_bytes(b"m" * 1000)
    (daily / "not-a-stamp").mkdir()
    (monthly / "notes").mkdir()
    incoming = tmp_path / ".incoming" / "20260927-010000"
    incoming.mkdir(parents=True)
    (incoming / "db.dump.partial").write_text("partial", encoding="utf-8")
    (tmp_path / "LATEST").write_text("daily/keep\n", encoding="utf-8")
    (tmp_path / "status.json").write_text("{}\n", encoding="utf-8")

    proc = _run(PRUNE, {"ANILA_BACKUP_DIR": str(tmp_path)}, str(tmp_path))
    assert proc.returncode == 0, proc.stderr

    left_daily = sorted(p.name for p in daily.iterdir())
    assert left_daily == [*sorted(names)[-14:], "not-a-stamp"]
    left_monthly = sorted(p.name for p in monthly.iterdir())
    assert left_monthly == [*sorted(months)[-6:], "notes"]
    assert incoming.is_dir()
    assert (tmp_path / "LATEST").read_text(encoding="utf-8") == "daily/keep\n"
    assert (tmp_path / "status.json").is_file()


def test_backup_loop_writes_atomically(tmp_path: Path):
    src = tmp_path / "src"
    dest = tmp_path / "backups"
    dest.mkdir()
    _sources(src)
    fake = tmp_path / "fake-dump.sh"
    _fake_dump(fake)

    proc = _run(LOOP, _backup_env(tmp_path, src, dest, fake))
    assert proc.returncode == 0, proc.stderr + proc.stdout

    assert not (dest / ".incoming").exists()
    assert list(dest.rglob("*.partial")) == []
    assert list(dest.rglob("*.tmp")) == []
    snap = dest / "daily" / "20260927-021500"
    assert (snap / "db.dump").is_file()
    assert (snap / "db.dump").stat().st_size >= 1000
    assert not (snap / ".sources").exists()
    assert (dest / "LATEST").read_text(encoding="utf-8").strip() == "daily/20260927-021500"
    status = json.loads((dest / "status.json").read_text(encoding="utf-8"))
    parsed = parse_backup_status_text((dest / "status.json").read_text(encoding="utf-8"))
    assert parsed.last_result == "success"
    assert parsed.last_success_at is not None
    assert status["snapshot"] == "daily/20260927-021500"
    assert (dest / "monthly" / "202609" / "db.dump").read_bytes() == (snap / "db.dump").read_bytes()

    with tarfile.open(snap / "files-router-sessions.tar", "r:gz") as archive:
        names = archive.getnames()
    assert any(name.endswith("sessions.db") for name in names)
    assert all("state" not in Path(name).parts for name in names)

    mode = (dest / "status.json").stat().st_mode
    assert mode & stat.S_IROTH, "CSP 的 uid 必須讀得到狀態檔"


def test_failed_publish_does_not_replace_latest(tmp_path: Path):
    src = tmp_path / "src"
    dest = tmp_path / "backups"
    dest.mkdir()
    _sources(src)
    previous = dest / "daily" / "20260926-021500"
    previous.mkdir(parents=True)
    (previous / "db.dump").write_bytes(b"old" * 400)
    (dest / "LATEST").write_text("daily/20260926-021500\n", encoding="utf-8")
    (dest / "status.json").write_text(
        json.dumps(
            {
                "schema": 1,
                "last_run_at": "2026-09-26T02:15:00Z",
                "last_result": "success",
                "last_size_bytes": 1200,
                "last_success_at": "2026-09-26T02:15:00Z",
                "last_success_size_bytes": 1200,
                "snapshot": "daily/20260926-021500",
                "error": "",
            }
        ),
        encoding="utf-8",
    )
    fake = tmp_path / "fake-dump.sh"
    _fake_dump(fake)
    proc = _run(
        LOOP,
        _backup_env(
            tmp_path,
            src,
            dest,
            fake,
            ANILA_BACKUP_FAIL_BEFORE_PUBLISH="1",
            ANILA_BACKUP_STAMP="20260927-021500",
        ),
    )
    assert proc.returncode == 1, proc.stderr
    assert not (dest / "daily" / "20260927-021500").exists()
    assert not (dest / ".incoming").exists()
    assert list(dest.rglob("*.partial")) == []
    assert (dest / "LATEST").read_text(encoding="utf-8").strip() == "daily/20260926-021500"
    parsed = parse_backup_status_text((dest / "status.json").read_text(encoding="utf-8"))
    assert parsed.last_result == "failure"
    assert parsed.error == "publish_failed"
    assert parsed.last_success_at is not None
    assert parsed.last_success_at.isoformat().startswith("2026-09-26T02:15:00")


def test_tiny_dump_is_not_published(tmp_path: Path):
    src = tmp_path / "src"
    dest = tmp_path / "backups"
    dest.mkdir()
    _sources(src)
    fake = tmp_path / "fake-dump.sh"
    fake.write_text(
        "#!/bin/sh\n"
        "case \"$1\" in\n"
        "  */.incoming/*/db.dump.partial) ;;\n"
        "  *) exit 2 ;;\n"
        "esac\n"
        "printf 'tiny' > \"$1\"\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    proc = _run(LOOP, _backup_env(tmp_path, src, dest, fake))
    assert proc.returncode == 1, proc.stderr
    assert not (dest / "daily").exists() or not any((dest / "daily").iterdir())
    parsed = parse_backup_status_text((dest / "status.json").read_text(encoding="utf-8"))
    assert parsed.error == "dump_too_small"
    assert not (dest / "LATEST").exists()


def _tar_with_marker(path: Path, marker: str) -> None:
    data = marker.encode()
    info = tarfile.TarInfo("marker.txt")
    info.size = len(data)
    with tarfile.open(path, "w:gz") as archive:
        archive.addfile(info, io.BytesIO(data))


def test_restore_files_and_refuses_without_confirm(tmp_path: Path):
    snap = tmp_path / "snap"
    snap.mkdir()
    (snap / "db.dump").write_bytes(b"d" * 1000)
    markers = {
        "files-uploads.tar": "uploads",
        "files-attachments.tar": "attachments",
        "files-static.tar": "static",
        "files-pki.tar": "pki",
        "files-quickstart.tar": "quickstart",
        "files-studio-artifacts.tar": "studio",
        "files-router-sessions.tar": "router",
        "files-n8n.tar": "n8n",
    }
    for name, marker in markers.items():
        _tar_with_marker(snap / name, marker)

    refused = subprocess.run(
        ["bash", str(RESTORE), str(snap)],
        env={**os.environ, "ANILA_RESTORE_CONFIRM": ""},
        capture_output=True,
        text=True,
        check=False,
    )
    assert refused.returncode != 0

    share = tmp_path / "share"
    restored = subprocess.run(
        ["bash", str(RESTORE), str(snap)],
        env={
            **os.environ,
            "ANILA_RESTORE_CONFIRM": "yes",
            "ANILA_RESTORE_SKIP_DB": "1",
            "ANILA_RESTORE_SKIP_VOLUMES": "1",
            "ANILA_SHARE_ROOT": str(share),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert restored.returncode == 0, restored.stderr
    assert "RESTORE_ALL_OK" in restored.stdout
    for folder, marker in (
        ("uploads", "uploads"),
        ("attachments", "attachments"),
        ("static", "static"),
        ("pki", "pki"),
        ("quickstart", "quickstart"),
    ):
        assert (share / folder / "marker.txt").read_text(encoding="utf-8") == marker


def test_manual_backup_script_is_retired():
    proc = _run(OLD, {})
    assert proc.returncode == 1
    assert "share/backups" in proc.stderr
    assert "crontab" in proc.stderr
