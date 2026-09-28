"""備份狀態檔解析，以及過期／失敗告警。"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from app.models.alert import Alert
from app.services import alert_detectors as ad
from app.services.alert_detectors import FP_BACKUP, evaluate_backup, reset_streaks_for_tests
from app.services.alert_notifier import AlertNotification, set_notifier
from app.services.auth_service import create_tokens
from app.services.backup_status import (
    BACKUP_STALE_AFTER,
    assess_backup_path,
    backup_alert_reason,
    parse_backup_status_text,
    public_backup_view,
)
from tests.conftest import make_user

NOW = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)


class CapturingNotifier:
    def __init__(self) -> None:
        self.sent: list[AlertNotification] = []

    def send(self, notification: AlertNotification) -> None:
        self.sent.append(notification)


@pytest.fixture(autouse=True)
def _clean_notifier():
    reset_streaks_for_tests()
    capture = CapturingNotifier()
    previous = set_notifier(capture)
    yield capture
    set_notifier(previous)
    reset_streaks_for_tests()


def _status(**overrides) -> str:
    body = {
        "schema": 1,
        "last_run_at": "2026-09-27T02:15:00Z",
        "last_result": "success",
        "last_size_bytes": 4096,
        "last_success_at": "2026-09-27T02:15:00Z",
        "last_success_size_bytes": 4096,
        "snapshot": "daily/20260927-021500",
        "error": "",
    }
    body.update(overrides)
    return json.dumps(body)


def _parsed(**overrides):
    return parse_backup_status_text(_status(**overrides))


def _open(db):
    return (
        db.query(Alert)
        .filter(Alert.fingerprint == FP_BACKUP, Alert.status != "resolved")
        .all()
    )


def test_parse_success_and_sizes():
    parsed = _parsed()
    assert parsed.last_result == "success"
    assert parsed.last_size_bytes == 4096
    assert parsed.last_success_at == datetime(2026, 9, 27, 2, 15, tzinfo=timezone.utc)


def test_fresh_success_is_not_stale():
    assert backup_alert_reason(_parsed(), NOW) == "ok"


def test_exactly_36_hours_is_not_stale():
    success_at = NOW - BACKUP_STALE_AFTER
    parsed = _parsed(
        last_run_at=success_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        last_success_at=success_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    assert backup_alert_reason(parsed, NOW) == "ok"


def test_older_than_36_hours_is_stale():
    success_at = NOW - BACKUP_STALE_AFTER - timedelta(seconds=1)
    parsed = _parsed(
        last_run_at=success_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        last_success_at=success_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    assert backup_alert_reason(parsed, NOW) == "stale"


def test_failed_run_alerts_even_when_previous_success_is_recent():
    parsed = _parsed(
        last_run_at="2026-09-27T11:00:00Z",
        last_result="failure",
        last_size_bytes=0,
        last_success_at="2026-09-27T10:00:00Z",
        last_success_size_bytes=4096,
        snapshot=None,
        error="pg_dump_failed",
    )
    assert backup_alert_reason(parsed, NOW) == "failed"


def test_garbage_and_missing_file(tmp_path):
    status = tmp_path / "status.json"
    status.write_text("{", encoding="utf-8")
    bad = assess_backup_path(status, NOW)
    assert bad.reason == "unreadable"
    assert bad.parsed is None

    missing = tmp_path / "absent.json"
    assert assess_backup_path(missing, NOW).reason == "missing"

    unwired = tmp_path / "no-such-dir" / "status.json"
    assert assess_backup_path(unwired, NOW).reason == "unwired"


def test_public_view_whitelist(tmp_path):
    path = tmp_path / "status.json"
    path.write_text(
        _status(error="pg_dump_failed", last_result="failure", last_size_bytes=0, snapshot=None),
        encoding="utf-8",
    )
    view = public_backup_view(assess_backup_path(path, NOW))
    assert set(view) == {
        "last_run_at",
        "last_result",
        "last_size_bytes",
        "last_success_at",
        "stale",
        "reason",
    }
    assert view["reason"] == "failed"
    assert "snapshot" not in view
    assert "error" not in view
    assert "pg_dump" not in json.dumps(view)


def test_stale_backup_opens_then_fresh_success_resolves(db, _clean_notifier, tmp_path):
    path = tmp_path / "status.json"
    old = (NOW - BACKUP_STALE_AFTER - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    path.write_text(
        _status(last_run_at=old, last_success_at=old),
        encoding="utf-8",
    )
    assert evaluate_backup(db=db, path=path, now=NOW) == "stale"
    db.commit()
    alert = _open(db)
    assert len(alert) == 1
    assert alert[0].severity == "high"
    assert "36" in alert[0].title or "36" in alert[0].message
    assert "http://" not in alert[0].message
    assert "http://" not in alert[0].title
    assert _clean_notifier.sent[-1].fingerprint == FP_BACKUP

    path.write_text(_status(), encoding="utf-8")
    assert evaluate_backup(db=db, path=path, now=NOW) == "ok"
    db.commit()
    assert _open(db) == []


def test_failed_run_opens_alert(db, _clean_notifier, tmp_path):
    path = tmp_path / "status.json"
    path.write_text(
        _status(last_result="failure", last_size_bytes=0, error="tar_failed", snapshot=None),
        encoding="utf-8",
    )
    assert evaluate_backup(db=db, path=path, now=NOW) == "failed"
    db.commit()
    assert _open(db)[0].title.startswith("最近一次備份")


def test_unwired_directory_does_not_alert(db, _clean_notifier, tmp_path):
    missing_parent = tmp_path / "missing" / "status.json"
    assert evaluate_backup(db=db, path=missing_parent, now=NOW) == "unwired"
    db.commit()
    assert _open(db) == []
    assert _clean_notifier.sent == []


def test_missing_status_file_alerts_when_directory_exists(db, tmp_path):
    path = tmp_path / "status.json"
    assert evaluate_backup(db=db, path=path, now=NOW) == "missing"
    db.commit()
    assert _open(db)[0].category == "backup"


def _bearer(user) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_tokens(user)['access_token']}"}


def test_backup_status_requires_admin(client, db, tmp_path, monkeypatch):
    path = tmp_path / "status.json"
    # The HTTP handler uses the wall clock. A fixed timestamp goes stale
    # 36 hours after it, so this file is always one hour old.
    stamp = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    path.write_text(
        _status(last_run_at=stamp, last_success_at=stamp),
        encoding="utf-8",
    )
    monkeypatch.setattr("app.services.backup_status.BACKUP_STATUS_PATH", path)

    assert client.get("/api/admin/backup-status").status_code == 401
    user = make_user(db, username="backup-user", role="user")
    assert client.get("/api/admin/backup-status", headers=_bearer(user)).status_code == 403

    admin = make_user(db, username="backup-admin", role="admin")
    response = client.get("/api/admin/backup-status", headers=_bearer(admin))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["reason"] == "ok"
    assert body["last_result"] == "success"
    assert body["last_size_bytes"] == 4096
    assert body["stale"] is False
