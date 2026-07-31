"""P2.7 稽核帳 — 不需要 PostgreSQL 的部分。

DB 級的證據(權限被拒、觸發器、竄改偵測)在 ``test_audit_ledger_pg.py``,
那些需要真的 role 分離,SQLite 上做不出來也不該假裝做得出來。
"""
from __future__ import annotations

import inspect
from datetime import date, datetime, timezone

import pytest

from app.services import audit_ledger


# ── 摘要正規化:同一列在任何 session timezone 下都要算出同一個雜湊 ──────────

def test_canon_normalises_timezone_to_utc():
    """psycopg2 回傳的 datetime 帶 session 的 TimeZone GUC。

    如果正規化沒把它換算成 UTC,同一列在不同連線會算出不同雜湊,
    驗證器就會對沒被動過的資料報竄改(狼來了 → 控制被無視)。
    """
    utc = datetime(2026, 7, 30, 23, 30, tzinfo=timezone.utc)
    taipei = utc.astimezone(timezone(__import__("datetime").timedelta(hours=8)))
    assert taipei.isoformat() != utc.isoformat()  # 前提:兩者字面不同
    assert audit_ledger._canon(taipei) == audit_ledger._canon(utc)


def test_canon_naive_datetime_treated_as_utc():
    naive = datetime(2026, 7, 30, 23, 30)
    aware = datetime(2026, 7, 30, 23, 30, tzinfo=timezone.utc)
    assert audit_ledger._canon(naive) == audit_ledger._canon(aware)


def test_canon_jsonb_key_order_does_not_change_digest():
    """JSONB 不保證欄位順序;摘要不能因為順序不同就變。"""
    assert audit_ledger._canon({"b": 1, "a": 2}) == audit_ledger._canon(
        {"a": 2, "b": 1}
    )


def test_row_line_distinguishes_shifted_field_values():
    """相鄰欄位互換內容必須算出不同的行 —— 否則竄改可以在欄位間搬運。"""
    cols = ("id", "action", "resource_type")
    assert audit_ledger._row_line("audit_logs", cols, (1, "a", "b")) != \
        audit_ledger._row_line("audit_logs", cols, (1, "b", "a"))


# ── 鏈結 ────────────────────────────────────────────────────────────────────

def test_link_depends_on_every_input():
    base = audit_ledger.link("0" * 64, date(2026, 7, 30), "a" * 64)
    assert base != audit_ledger.link("1" * 64, date(2026, 7, 30), "a" * 64)
    assert base != audit_ledger.link("0" * 64, date(2026, 7, 31), "a" * 64)
    assert base != audit_ledger.link("0" * 64, date(2026, 7, 30), "b" * 64)
    assert len(base) == 64


# ── 熱路徑成本 ──────────────────────────────────────────────────────────────

def test_hot_path_does_no_per_row_ledger_work():
    """``log_audit_event`` 不得碰帳本 —— 日級鏈的全部賣點就是這一條。

    這是結構性檢查而不是計時:計時會在忙碌的 CI 上飄,而「原始碼裡根本
    沒有那個呼叫」是量得死的。任何人想把鏈搬到寫入路徑上都會撞到這裡。
    """
    from app.services import audit_service

    source = inspect.getsource(audit_service)
    for forbidden in ("audit_ledger", "sha256", "hashlib", "chain_head"):
        assert forbidden not in source, (
            f"audit_service 出現 {forbidden!r} —— 稽核寫入的熱路徑被加上了"
            "每列成本,違反 P2.7 的日級鏈設計"
        )


# ── 排程 ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "now, expected_hours",
    [
        (datetime(2026, 7, 30, 0, 0, tzinfo=timezone.utc), 5 / 60),
        (datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc), 12 + 5 / 60),
        (datetime(2026, 7, 30, 0, 5, tzinfo=timezone.utc), 24),
    ],
)
def test_next_seal_is_always_in_the_future(now, expected_hours):
    seconds = audit_ledger._seconds_until_next_seal(now)
    assert seconds > 0
    assert seconds == pytest.approx(expected_hours * 3600, abs=1)


# ── 受保護集合 ──────────────────────────────────────────────────────────────

def test_protected_set_covers_the_three_event_tables_and_itself():
    assert audit_ledger.AUDIT_EVENT_TABLES == (
        "audit_logs", "policy_decisions", "classification_events",
    )
    assert set(audit_ledger.AUDIT_LEDGER_TABLES) == set(
        audit_ledger.AUDIT_EVENT_TABLES
    ) | {"audit_checkpoints"}


def test_digest_columns_match_the_actual_models():
    """摘要欄位清單漏掉某欄 = 那一欄可以被改而驗不出來。"""
    from app.models.audit_log import AuditLog
    from app.models.classification import ClassificationEvent
    from app.models.policy_decision import PolicyDecision

    columns = audit_ledger.digest_columns(audit_ledger.DIGEST_VERSION)
    for model in (AuditLog, PolicyDecision, ClassificationEvent):
        actual = {c.name for c in model.__table__.columns}
        covered = set(columns[model.__tablename__])
        assert actual == covered, (
            f"{model.__tablename__} 的欄位與摘要涵蓋範圍不一致:"
            f"漏掉 {sorted(actual - covered)}、多算 {sorted(covered - actual)}。"
            "加欄位時要同時升 DIGEST_VERSION 並補進 _DIGEST_COLUMNS。"
        )


def test_unknown_digest_version_is_refused_not_guessed():
    with pytest.raises(ValueError):
        audit_ledger.digest_columns(99)


# ── 稽核表不得有 FK(否則 ON DELETE SET NULL 會對 append-only 表發 UPDATE)──

def test_audit_tables_carry_no_foreign_keys():
    """有 FK 就有一條 DB 自己發動的 UPDATE/DELETE 通道。

    ``audit_logs.actor_user_id`` 曾經是 users.id 的 FK,於是硬刪帳號必須先
    把稽核歸屬清成 NULL(= 刪帳號就能洗掉自己)。``classification_events``
    與 ``policy_decisions`` 的 ``ON DELETE SET NULL`` 更糟:UPDATE 由 DB
    自己發出,會撞上 append-only 觸發器,讓刪使用者/task 整個炸掉。
    """
    from app.models.audit_checkpoint import AuditCheckpoint
    from app.models.audit_log import AuditLog
    from app.models.classification import ClassificationEvent
    from app.models.policy_decision import PolicyDecision

    for model in (AuditLog, PolicyDecision, ClassificationEvent, AuditCheckpoint):
        assert not model.__table__.foreign_keys, (
            f"{model.__tablename__} 又長出 FK "
            f"{[str(fk.target_fullname) for fk in model.__table__.foreign_keys]}"
        )


# ── 匯出檔:鏈頭必須真的印在裡面(否則錨定是假的) ──────────────────────────

def test_export_embeds_chain_head_and_verify_instructions(client, db):
    from tests.conftest import login, make_user

    make_user(db, username="root-owner", role="owner")
    login(client, "root-owner")
    resp = client.get("/api/audit-logs/export")
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/csv")
    body = resp.text
    anchor = audit_ledger.current_anchor(db)
    assert anchor.chain_head in body, "匯出檔沒有印出鏈頭 —— 錨定不成立"
    assert resp.headers.get("X-Anila-Audit-Chain-Head") == anchor.chain_head
    assert "verify_audit_chain" in body, "匯出檔沒有告訴收件者怎麼驗"
    assert "id,created_at,actor_user_id" in body


def test_export_requires_admin(client, db):
    from tests.conftest import login, make_user

    make_user(db, username="plain-jane")
    login(client, "plain-jane")
    resp = client.get("/api/audit-logs/export")
    assert resp.status_code in (401, 403), resp.text
    assert "chain" not in resp.text.lower()


# ── 開機守衛:查不出來也算不合格 ─────────────────────────────────────────

def test_startup_guard_refuses_to_boot_when_it_cannot_check(monkeypatch):
    """查不出來 ≠ 沒問題。

    這支檢查以前包在 try/except 裡「查詢失敗就 warning 然後照樣開機」——
    看不見就放行的門衛。它守的是「稽核帳有沒有在保護中」,而「不知道」正是
    最不該放行的狀況。production 拒絕啟動,dev 只警告。
    """
    import app.services.startup_security as ss

    class _Boom:
        class dialect:
            name = "postgresql"

        def connect(self):
            raise OSError("could not connect to server")

    monkeypatch.setattr("app.database.engine", _Boom(), raising=False)

    monkeypatch.setattr(ss, "_is_dev_mode", lambda: False)
    with pytest.raises(RuntimeError) as exc:
        ss.assert_audit_ledger_locked_down()
    assert "無法確認" in str(exc.value)

    # dev:只警告,不擋開機。
    monkeypatch.setattr(ss, "_is_dev_mode", lambda: True)
    ss.assert_audit_ledger_locked_down()
