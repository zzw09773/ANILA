"""``alert_service.upsert_alert`` 的語意回歸(W2-6)。

W2-6 把 read-then-write 去重改成 ``INSERT ... ON CONFLICT (fingerprint)
DO UPDATE``。這裡釘住的是「換掉實作但語意不變」的部分;真正的併發證據在
``test_w26_startup_ddl_absorption_pg.py``(需要真 PostgreSQL,SQLite 的
`StaticPool` 單連線測不出跨交易競態)。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from app.models.alert import Alert
from app.services.alert_service import (
    acknowledge_alert,
    resolve_alert,
    resolve_alert_by_fingerprint,
    summarize_alerts,
    upsert_alert,
)
from app.models.user import User
from app.utils.security import hash_password


def _upsert(db, **overrides):
    payload = {
        "fingerprint": "health:model:1",
        "category": "health",
        "severity": "high",
        "title": "模型離線",
        "message": "無法連線",
        "source_type": "model",
        "source_id": 1,
    }
    payload.update(overrides)
    alert = upsert_alert(db, **payload)
    db.commit()
    return alert


def test_first_upsert_inserts_one_open_alert(db):
    alert = _upsert(db, metadata={"endpoint": "http://x"})

    assert alert.id is not None
    assert alert.status == "open"
    assert alert.first_seen_at is not None
    assert alert.last_seen_at is not None
    assert alert.source_id == "1"  # int 一律轉字串(ORM 宣告 String(100))
    assert json.loads(alert.metadata_json) == {"endpoint": "http://x"}
    assert db.query(Alert).count() == 1


def test_repeat_upsert_updates_in_place_without_new_row(db):
    first = _upsert(db)
    original_first_seen = first.first_seen_at
    original_id = first.id

    second = _upsert(db, severity="critical", title="模型離線(升級)")

    assert db.query(Alert).count() == 1
    assert second.id == original_id
    assert second.severity == "critical"
    assert second.title == "模型離線(升級)"
    # first_seen_at 是「第一次看到」,不得被覆寫
    assert second.first_seen_at == original_first_seen


def test_upsert_preserves_acknowledgement(db):
    """值班人員的簽收不得被下一輪健康檢查抹掉。"""
    actor = User(username="ops", hashed_password=hash_password("x"), role="admin")
    db.add(actor)
    db.commit()

    alert = _upsert(db)
    acknowledge_alert(db, alert, actor)
    db.commit()

    refreshed = _upsert(db, message="還是連不上")

    assert refreshed.status == "acknowledged"
    assert refreshed.acknowledged_by_user_id == actor.id
    assert refreshed.acknowledged_at is not None
    assert refreshed.message == "還是連不上"


def test_upsert_reopens_a_resolved_alert_and_clears_ack_fields(db):
    actor = User(username="ops2", hashed_password=hash_password("x"), role="admin")
    db.add(actor)
    db.commit()

    alert = _upsert(db)
    acknowledge_alert(db, alert, actor)
    resolve_alert(db, alert)
    db.commit()
    assert alert.status == "resolved"

    reopened = _upsert(db)

    assert reopened.status == "open"
    assert reopened.resolved_at is None
    assert reopened.acknowledged_at is None
    assert reopened.acknowledged_by_user_id is None
    assert db.query(Alert).count() == 1


def test_distinct_fingerprints_stay_distinct(db):
    _upsert(db, fingerprint="health:model:1")
    _upsert(db, fingerprint="health:agent:1")

    assert db.query(Alert).count() == 2


def test_resolve_by_fingerprint_and_summary(db):
    _upsert(db, fingerprint="health:model:1", severity="high")
    _upsert(db, fingerprint="health:model:2", severity="low")
    resolved = resolve_alert_by_fingerprint(db, "health:model:2")
    db.commit()

    assert resolved is not None
    assert resolve_alert_by_fingerprint(db, "health:model:2") is None

    summary = summarize_alerts(db)
    assert summary["open_count"] == 1
    assert summary["resolved_count"] == 1
    assert summary["high_count"] == 1


def test_upsert_refreshes_stale_identity_map_entry(db):
    """identity map 裡的舊物件必須被 populate_existing 刷新。"""
    first = _upsert(db, severity="high")
    assert first.severity == "high"

    # 直接用 SQL 改庫,模擬「別的 session 動過這一列」
    db.execute(Alert.__table__.update().values(severity="low"))
    db.commit()

    refreshed = _upsert(db, severity="critical")
    assert refreshed.severity == "critical"


def test_unknown_dialect_is_rejected_instead_of_silently_racing(db, monkeypatch):
    """未知方言不得靜默退回 read-then-write —— 那正是被修掉的競態。"""
    from app.services import alert_service

    class _FakeDialect:
        name = "oracle"

    class _FakeBind:
        dialect = _FakeDialect()

    monkeypatch.setattr(db, "get_bind", lambda: _FakeBind())
    with pytest.raises(NotImplementedError, match="ON CONFLICT"):
        alert_service.upsert_alert(
            db,
            fingerprint="x",
            category="health",
            severity="high",
            title="t",
            message="m",
        )


def test_last_seen_at_moves_forward_on_repeat(db):
    first = _upsert(db)
    before = first.last_seen_at

    # 把 last_seen_at 推回過去,確認下一次 upsert 真的把它往前帶
    db.execute(
        Alert.__table__.update().values(
            last_seen_at=datetime.now(timezone.utc) - timedelta(hours=1)
        )
    )
    db.commit()

    again = _upsert(db)
    assert again.last_seen_at is not None
    assert again.last_seen_at > (before - timedelta(hours=1))
