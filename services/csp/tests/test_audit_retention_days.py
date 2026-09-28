"""稽核保留期是 365 天（2026-09-27）。觸發器與常數必須同一個窗口。"""
from __future__ import annotations

from pathlib import Path

from app.services.audit_ledger import AUDIT_RETENTION_DAYS


def test_audit_retention_is_365_days():
    assert AUDIT_RETENTION_DAYS == 365
    migration = (
        Path(__file__).resolve().parents[1]
        / "migrations"
        / "versions"
        / "r1_0056_alert_mail_settings.py"
    )
    text = migration.read_text(encoding="utf-8")
    upgrade = text.split("def downgrade", 1)[0]
    assert "365 days" in upgrade
