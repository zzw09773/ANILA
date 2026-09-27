"""讀備份狀態檔，判斷最近一次成功是否超過 36 小時。

backup 服務把 ``status.json`` 寫在宿主機備份目錄。CSP 以唯讀掛載看固定路徑
``/var/anila/backup-status/status.json``，不另設環境變數。

目錄本身不存在（單元測試、或這支程式不是從 compose 起來）時理由是
``unwired``：偵測器不開告警，避免沒掛載的行程一直叫。目錄在但沒有狀態檔，
代表服務起來了卻還沒成功備份，那要告警。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

BACKUP_STATUS_PATH = Path("/var/anila/backup-status/status.json")
BACKUP_STALE_AFTER = timedelta(hours=36)

_RESULTS = frozenset({"success", "failure"})


@dataclass(frozen=True, slots=True)
class ParsedBackup:
    last_run_at: datetime
    last_result: str
    last_size_bytes: int
    last_success_at: datetime | None
    last_success_size_bytes: int
    snapshot: str | None
    error: str


@dataclass(frozen=True, slots=True)
class BackupAssessment:
    """``reason`` 是 ok / failed / stale / missing / unreadable / unwired。"""

    parsed: ParsedBackup | None
    reason: str


def _parse_ts(value: object) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ValueError("timestamp")
    text = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _parse_size(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("size")
    if value < 0:
        raise ValueError("size")
    return value


def parse_backup_status_text(text: str) -> ParsedBackup:
    """解析 backup 服務寫出的單行 JSON。壞掉就丟 ValueError。"""
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("object")
    if data.get("schema") != 1:
        raise ValueError("schema")
    result = data.get("last_result")
    if result not in _RESULTS:
        raise ValueError("last_result")
    last_run_at = _parse_ts(data.get("last_run_at"))
    if last_run_at is None:
        raise ValueError("last_run_at")
    snapshot = data.get("snapshot")
    if snapshot is not None and not isinstance(snapshot, str):
        raise ValueError("snapshot")
    error = data.get("error", "")
    if not isinstance(error, str):
        raise ValueError("error")
    return ParsedBackup(
        last_run_at=last_run_at,
        last_result=result,
        last_size_bytes=_parse_size(data.get("last_size_bytes")),
        last_success_at=_parse_ts(data.get("last_success_at")),
        last_success_size_bytes=_parse_size(data.get("last_success_size_bytes")),
        snapshot=snapshot,
        error=error,
    )


def backup_alert_reason(parsed: ParsedBackup | None, now: datetime) -> str:
    """最近一輪失敗，或最後一次成功早於 36 小時，都要告警。

    剛好 36 小時還不算過期。``now`` 與時間戳都應是有時區的。
    """
    if parsed is None:
        return "missing"
    if parsed.last_result != "success" or parsed.last_success_at is None:
        return "failed"
    if now - parsed.last_success_at > BACKUP_STALE_AFTER:
        return "stale"
    return "ok"


def assess_backup_path(path: Path, now: datetime | None = None) -> BackupAssessment:
    now = now if now is not None else datetime.now(timezone.utc)
    if not path.parent.exists():
        return BackupAssessment(None, "unwired")
    if not path.is_file():
        return BackupAssessment(None, "missing")
    try:
        parsed = parse_backup_status_text(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        return BackupAssessment(None, "unreadable")
    return BackupAssessment(parsed, backup_alert_reason(parsed, now))


def assess_backup(now: datetime | None = None, path: Path | None = None) -> BackupAssessment:
    return assess_backup_path(BACKUP_STATUS_PATH if path is None else path, now)


def public_backup_view(assessment: BackupAssessment) -> dict:
    """治理中心看的欄位。沒有路徑、沒有錯誤原文。"""
    parsed = assessment.parsed
    reason = "missing" if assessment.reason == "unwired" else assessment.reason

    def iso(value: datetime | None) -> str | None:
        if value is None:
            return None
        return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    return {
        "last_run_at": iso(parsed.last_run_at) if parsed else None,
        "last_result": parsed.last_result if parsed else "none",
        "last_size_bytes": parsed.last_size_bytes if parsed else None,
        "last_success_at": iso(parsed.last_success_at) if parsed else None,
        "stale": assessment.reason == "stale",
        "reason": reason,
    }
