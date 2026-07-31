"""P3.2 alert detectors — quiet day, fire, resolve, and notifier visibility.

Each firing test states what was reverted to confirm the quiet path goes
green again (acceptance: stuck open alert is its own failure).
"""
from __future__ import annotations

import asyncio
import logging

import pytest

from app.models.alert import Alert
from app.services import alert_detectors as ad
from app.services.alert_detectors import (
    AGENT_FAIL_STREAK,
    DB_FAIL_STREAK,
    DISK_CRIT_PCT,
    DISK_WARN_PCT,
    FP_DB_POOL,
    FP_DB_SERVER,
    FP_PLATFORM_INGRESS,
    GATEWAY_FAIL_STREAK,
    PLATFORM_INGRESS_STREAK,
    DatabaseProbeOutcome,
    DiskSample,
    PlatformProbeOutcome,
    evaluate_database,
    evaluate_disk,
    evaluate_platform_ingress,
    record_proxy_outcome,
    reset_streaks_for_tests,
)
from app.services.alert_notifier import (
    AlertNotification,
    UnwiredSmtpNotifier,
    set_notifier,
)


class CapturingNotifier:
    def __init__(self) -> None:
        self.sent: list[AlertNotification] = []

    def send(self, notification: AlertNotification) -> None:
        self.sent.append(notification)


@pytest.fixture(autouse=True)
def _clean_streaks_and_notifier():
    reset_streaks_for_tests()
    capture = CapturingNotifier()
    previous = set_notifier(capture)
    yield capture
    set_notifier(previous)
    reset_streaks_for_tests()


def _open_alerts(db) -> list[Alert]:
    return (
        db.query(Alert)
        .filter(Alert.status != "resolved")
        .order_by(Alert.fingerprint)
        .all()
    )


def _by_fp(db, fingerprint: str) -> Alert | None:
    return db.query(Alert).filter(Alert.fingerprint == fingerprint).first()


# ── Notifier stub visibility ─────────────────────────────────────────────────


def test_unwired_notifier_logs_warning_not_silent(caplog):
    """Absence of SMTP must be visible in console, not quiet."""
    set_notifier(UnwiredSmtpNotifier())
    with caplog.at_level(logging.WARNING, logger="app.services.alert_notifier"):
        UnwiredSmtpNotifier().send(
            AlertNotification(
                fingerprint="test:fp",
                category="test",
                severity="high",
                title="標題",
                message="內容不可含 http://secret:9",
            )
        )
    assert any("ALERT_SMTP_UNWIRED" in r.message for r in caplog.records)
    assert any("test:fp" in r.message for r in caplog.records)


# ── 1. Platform ingress ──────────────────────────────────────────────────────


def test_platform_ingress_quiet_when_reachable(db, _clean_streaks_and_notifier, monkeypatch):
    """Normal day: nginx probe healthy → no alert.

    Revert check: swap reachable→False for PLATFORM_INGRESS_STREAK rounds
    (see firing test) then back to True → alert resolves.
    """
    async def _ok():
        return PlatformProbeOutcome(True, "ok")

    monkeypatch.setattr(ad, "probe_platform_ingress", _ok)
    for _ in range(PLATFORM_INGRESS_STREAK + 2):
        streak = asyncio.run(evaluate_platform_ingress(db=db))
        assert streak == 0
    db.commit()
    assert _open_alerts(db) == []
    assert _clean_streaks_and_notifier.sent == []


def test_platform_ingress_fires_then_resolves(db, _clean_streaks_and_notifier, monkeypatch):
    """Fire after consecutive unreachable; resolve when reachable returns.

    Reverted: reachable flag False→True after the open alert.
    """
    state = {"ok": False}

    async def _probe():
        return PlatformProbeOutcome(state["ok"], "unreachable" if not state["ok"] else "ok")

    monkeypatch.setattr(ad, "probe_platform_ingress", _probe)

    for i in range(PLATFORM_INGRESS_STREAK - 1):
        streak = asyncio.run(evaluate_platform_ingress(db=db))
        assert streak == i + 1
        assert _open_alerts(db) == []

    streak = asyncio.run(evaluate_platform_ingress(db=db))
    assert streak == PLATFORM_INGRESS_STREAK
    db.commit()
    alert = _by_fp(db, FP_PLATFORM_INGRESS)
    assert alert is not None and alert.status == "open"
    assert alert.severity == "critical"
    assert "http://" not in alert.message
    assert _clean_streaks_and_notifier.sent
    assert _clean_streaks_and_notifier.sent[-1].fingerprint == FP_PLATFORM_INGRESS

    # Revert condition → must self-resolve
    state["ok"] = True
    asyncio.run(evaluate_platform_ingress(db=db))
    db.commit()
    alert = _by_fp(db, FP_PLATFORM_INGRESS)
    assert alert is not None and alert.status == "resolved"


# ── 2. Database discrimination ───────────────────────────────────────────────


def test_database_quiet_when_ok(db, _clean_streaks_and_notifier):
    """Normal day: SELECT 1 ok → no alert.

    Revert check: inject server_gone / pool_exhausted (firing tests) then ok.
    """
    for _ in range(DB_FAIL_STREAK + 2):
        evaluate_database(db=db, outcome=DatabaseProbeOutcome(True, "ok", "select_1_ok"))
    db.commit()
    assert _open_alerts(db) == []
    assert _clean_streaks_and_notifier.sent == []


def test_database_server_gone_fires_not_pool(db, _clean_streaks_and_notifier):
    """Server gone must not be labelled pool exhausted.

    Reverted: outcome kind server_gone → ok.
    """
    gone = DatabaseProbeOutcome(False, "server_gone", "operational_error")
    evaluate_database(db=db, outcome=gone)
    assert _open_alerts(db) == []
    evaluate_database(db=db, outcome=gone)
    db.commit()
    alert = _by_fp(db, FP_DB_SERVER)
    assert alert is not None and alert.status == "open"
    assert alert.severity == "critical"
    assert _by_fp(db, FP_DB_POOL) is None or _by_fp(db, FP_DB_POOL).status == "resolved"
    assert "連線池" not in alert.title

    evaluate_database(db=db, outcome=DatabaseProbeOutcome(True, "ok", "select_1_ok"))
    db.commit()
    assert _by_fp(db, FP_DB_SERVER).status == "resolved"


def test_database_pool_exhausted_fires_not_server(db, _clean_streaks_and_notifier):
    """Pool timeout must not send the maintainer to restart postgres.

    Reverted: outcome kind pool_exhausted → ok.
    """
    pool = DatabaseProbeOutcome(False, "pool_exhausted", "pool_timeout")
    evaluate_database(db=db, outcome=pool)
    evaluate_database(db=db, outcome=pool)
    db.commit()
    alert = _by_fp(db, FP_DB_POOL)
    assert alert is not None and alert.status == "open"
    assert alert.severity == "high"
    assert "不要先重啟 postgres" in alert.message
    assert _by_fp(db, FP_DB_SERVER) is None or _by_fp(db, FP_DB_SERVER).status == "resolved"

    evaluate_database(db=db, outcome=DatabaseProbeOutcome(True, "ok", "select_1_ok"))
    db.commit()
    assert _by_fp(db, FP_DB_POOL).status == "resolved"


def test_database_ledger_failure_still_notifies(monkeypatch, _clean_streaks_and_notifier):
    """When DB is dead the ledger write fails — notifier must still be called."""

    def _boom_session():
        raise RuntimeError("no db")

    monkeypatch.setattr(ad, "SessionLocal", _boom_session)
    opened = ad._emit_alert_autocommit(
        fingerprint=FP_DB_SERVER,
        category="database",
        severity="critical",
        title="資料庫連不上",
        message="ledger 寫不進去時仍要喊",
        source_type="database",
        source_id="server",
    )
    assert opened is True
    assert any(n.fingerprint == FP_DB_SERVER for n in _clean_streaks_and_notifier.sent)


# ── 3. Gateway consecutive traffic failures ──────────────────────────────────


def test_gateway_quiet_below_threshold_and_resets(db, _clean_streaks_and_notifier):
    """Normal day: blips below GATEWAY_FAIL_STREAK, or a success mid-streak → quiet.

    Revert check: after N-1 failures a success zeroes the streak.
    """
    for _ in range(GATEWAY_FAIL_STREAK - 1):
        n = record_proxy_outcome(
            model_id=42,
            model_name="gpt-oss",
            model_type="llm",
            display_name="院內模型",
            success=False,
            db=db,
        )
    assert n == GATEWAY_FAIL_STREAK - 1
    db.commit()
    assert _open_alerts(db) == []

    # Revert: one success clears streak
    assert (
        record_proxy_outcome(
            model_id=42,
            model_name="gpt-oss",
            model_type="llm",
            success=True,
            db=db,
        )
        == 0
    )
    for _ in range(GATEWAY_FAIL_STREAK - 1):
        record_proxy_outcome(
            model_id=42,
            model_name="gpt-oss",
            model_type="llm",
            success=False,
            db=db,
        )
    db.commit()
    assert _open_alerts(db) == []
    assert _clean_streaks_and_notifier.sent == []


def test_gateway_fires_at_threshold_then_resolves(db, _clean_streaks_and_notifier):
    """Fire at GATEWAY_FAIL_STREAK; resolve on success.

    Reverted: success=False×N → success=True.
    """
    for _ in range(GATEWAY_FAIL_STREAK):
        record_proxy_outcome(
            model_id=7,
            model_name="gpt-oss",
            model_type="llm",
            display_name="院內模型",
            success=False,
            db=db,
        )
    db.commit()
    fp = "gateway:traffic:7"
    alert = _by_fp(db, fp)
    assert alert is not None and alert.status == "open"
    assert alert.severity == "high"
    assert "http://" not in alert.message
    assert "endpoint" not in alert.message.lower()
    assert _clean_streaks_and_notifier.sent[-1].fingerprint == fp

    record_proxy_outcome(
        model_id=7, model_name="gpt-oss", model_type="llm", success=True, db=db
    )
    db.commit()
    assert _by_fp(db, fp).status == "resolved"


# ── 4. Agent dispatch consecutive failures ───────────────────────────────────


def test_agent_quiet_below_threshold(db, _clean_streaks_and_notifier):
    """Normal day: fewer than AGENT_FAIL_STREAK failures → quiet.

    Revert check: success mid-streak (same as gateway).
    """
    for _ in range(AGENT_FAIL_STREAK - 1):
        record_proxy_outcome(
            model_id=99,
            model_name="rag-agent",
            model_type="agent",
            success=False,
            db=db,
        )
    db.commit()
    assert _open_alerts(db) == []
    assert _clean_streaks_and_notifier.sent == []


def test_agent_fires_at_threshold_then_resolves(db, _clean_streaks_and_notifier):
    """Fire at AGENT_FAIL_STREAK; resolve on success.

    Reverted: success=False×N → success=True.
    """
    for _ in range(AGENT_FAIL_STREAK):
        record_proxy_outcome(
            model_id=99,
            model_name="rag-agent",
            model_type="agent",
            display_name="知識庫",
            success=False,
            db=db,
        )
    db.commit()
    fp = "agent:dispatch:99"
    alert = _by_fp(db, fp)
    assert alert is not None and alert.status == "open"
    assert "派工" in alert.title
    assert _clean_streaks_and_notifier.sent[-1].fingerprint == fp

    record_proxy_outcome(
        model_id=99, model_name="rag-agent", model_type="agent", success=True, db=db
    )
    db.commit()
    assert _by_fp(db, fp).status == "resolved"


# ── 5. Disk fullness ─────────────────────────────────────────────────────────


def _sample(label: str, used_pct: float) -> DiskSample:
    total = 1000 * 1024**3
    free = int(total * (1.0 - used_pct / 100.0))
    return DiskSample(
        label=label,
        path=f"/tmp/fake-{label}",
        used_pct=used_pct,
        free_bytes=free,
        total_bytes=total,
    )


def test_disk_quiet_under_warn(db, _clean_streaks_and_notifier):
    """Normal day: well under DISK_WARN_PCT → quiet.

    Revert check: used_pct 50 → 90 (firing) → 50 (resolve).
    """
    for _ in range(DB_FAIL_STREAK + 1):
        evaluate_disk([_sample("ingestion", 50.0)], db=db)
    db.commit()
    assert _open_alerts(db) == []
    assert _clean_streaks_and_notifier.sent == []


def test_disk_warn_and_critical_then_resolve(db, _clean_streaks_and_notifier):
    """85% → high; 95% → critical; drop below warn → resolve.

    Reverted: used_pct from critical back under DISK_WARN_PCT.
    """
    # Below confirm streak — no alert yet
    evaluate_disk([_sample("ingestion", DISK_WARN_PCT + 1)], db=db)
    assert _open_alerts(db) == []

    evaluate_disk([_sample("ingestion", DISK_WARN_PCT + 1)], db=db)
    db.commit()
    alert = _by_fp(db, "disk:usage:ingestion")
    assert alert is not None and alert.status == "open"
    assert alert.severity == "high"
    assert "/tmp/" not in alert.message  # path stays in metadata only

    # Escalate to critical
    reset_streaks_for_tests()
    evaluate_disk([_sample("ingestion", DISK_CRIT_PCT)], db=db)
    evaluate_disk([_sample("ingestion", DISK_CRIT_PCT)], db=db)
    db.commit()
    alert = _by_fp(db, "disk:usage:ingestion")
    assert alert.severity == "critical"

    # Revert: free space returns
    evaluate_disk([_sample("ingestion", 40.0)], db=db)
    db.commit()
    assert _by_fp(db, "disk:usage:ingestion").status == "resolved"


def test_disk_message_mentions_log_rotation_context(db, _clean_streaks_and_notifier):
    evaluate_disk([_sample("attachments", 90.0)], db=db)
    evaluate_disk([_sample("attachments", 90.0)], db=db)
    db.commit()
    alert = _by_fp(db, "disk:usage:attachments")
    assert "50MB" in alert.message or "輪替" in alert.message


# ── Threshold constants sanity (documentation lock) ──────────────────────────


def test_resolve_after_restart_with_empty_streak(db, _clean_streaks_and_notifier):
    """Ledger open + empty in-memory streak (post-restart) must still resolve."""
    from app.services.alert_service import upsert_alert

    upsert_alert(
        db,
        fingerprint="gateway:traffic:55",
        category="gateway",
        severity="high",
        title="模型 gateway 連續失敗：x",
        message="pre-seeded open alert",
        source_type="model",
        source_id=55,
    )
    db.commit()
    reset_streaks_for_tests()  # simulate process restart
    record_proxy_outcome(
        model_id=55,
        model_name="gpt-oss",
        model_type="llm",
        success=True,
        db=db,
    )
    db.commit()
    assert _by_fp(db, "gateway:traffic:55").status == "resolved"


def test_threshold_constants_match_report():
    assert PLATFORM_INGRESS_STREAK == 3
    assert DB_FAIL_STREAK == 2
    assert GATEWAY_FAIL_STREAK == 5
    assert AGENT_FAIL_STREAK == 3
    assert DISK_WARN_PCT == 85.0
    assert DISK_CRIT_PCT == 95.0
