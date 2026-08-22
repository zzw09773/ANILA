"""DK-4 — 首頁「請立即處理」的高嚴重度計數語意。

`/api/alerts/summary` 的 `high_count` 只該算「未處理」(open) 的高/critical
告警。已確認 (acknowledged) 的告警＝使用者已靜音，不該再被首頁逼「立即處理」；
已解決 (resolved) 的從頭到尾都不算。
用純 service 層呼叫 + SQLite 驗（showcase DB 語意無關的交易行為）。
"""
from __future__ import annotations

from app.models.alert import Alert
from app.services.alert_service import (
    acknowledge_alert,
    resolve_alert,
    summarize_alerts,
    upsert_alert,
)


def _seed(db, fingerprint, severity="high", status="open"):
    alert = upsert_alert(
        db,
        fingerprint=fingerprint,
        category="model",
        severity=severity,
        title=f"告警 {fingerprint}",
        message="seed",
        source_type="model",
        source_id=1,
    )
    if status == "acknowledged":
        acknowledge_alert(db, alert)
    elif status == "resolved":
        resolve_alert(db, alert)
    db.commit()
    return alert


def test_high_count_counts_only_open(db):
    """open 的高嚴重度才進 high_count；已確認/已解決都不進。"""
    _seed(db, "m1", "high", "open")
    _seed(db, "m2", "high", "acknowledged")
    _seed(db, "m3", "critical", "resolved")
    _seed(db, "m4", "high", "open")

    summary = summarize_alerts(db)

    assert summary["high_count"] == 2
    # raw/status counts 不受影響,仍然透明可對帳
    assert summary["open_count"] == 2
    assert summary["acknowledged_count"] == 1
    assert summary["resolved_count"] == 1


def test_high_count_zero_when_nothing_open(db):
    """全是已確認時,首頁不得報「請立即處理」。"""
    _seed(db, "m5", "high", "acknowledged")
    _seed(db, "m6", "critical", "acknowledged")

    summary = summarize_alerts(db)

    assert summary["high_count"] == 0
    assert summary["acknowledged_count"] == 2
