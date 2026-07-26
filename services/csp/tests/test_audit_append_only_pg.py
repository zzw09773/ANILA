# -*- coding: utf-8 -*-
"""三張稽核帳真的 append-only —— W3-12a,需要真 PG。

改動前「append-only」只是註解:`0014:129` 是 `GRANT ALL ON ALL TABLES ... TO
csp_app`,三張表零 trigger。2026-06-02 的稽核把後果寫得很清楚 ——

> an attacker (or admin) with DB write access can silently delete or edit audit
> rows … defeating the whole point of the classified-access trail.

而 W1-1 剛讓「讀取營業秘密對話」開始落稽核列。那些列若能被靜默改掉,那項工作的
價值是零:稽核的意義完全建立在「事後改不動」上。

這支只在真 PG 下跑 —— trigger 與 role 權限都不存在於 SQLite 的測試迴路,所以
SQLite 在**物理上**測不到這個不變式(與 legal-hold TOCTOU 是同一類限制)。
"""
from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import create_engine, text

_DSN = os.environ.get("ANILA_TEST_PG_DSN") or os.environ.get("TEST_POSTGRES_URL")
pytestmark = pytest.mark.skipif(
    not _DSN,
    reason="ANILA_TEST_PG_DSN / TEST_POSTGRES_URL 未設 —— trigger 與 role 權限只有真 PG 有",
)

LEDGERS = ("audit_logs", "policy_decisions", "classification_events")


@pytest.fixture(scope="module")
def engine():
    eng = create_engine(_DSN)
    yield eng
    eng.dispose()


def _insert_audit(conn) -> int:
    return conn.execute(
        text(
            "INSERT INTO audit_logs (action, resource_type, status, detail) "
            "VALUES ('probe', 'test', 'success', '原始內容') RETURNING id"
        )
    ).scalar_one()


# ── UPDATE / DELETE 一律拒絕 ─────────────────────────────────────────────────

def test_cannot_update_audit_row_content(engine):
    with engine.begin() as conn:
        rid = _insert_audit(conn)
    with engine.begin() as conn:
        with pytest.raises(Exception) as exc:
            conn.execute(
                text("UPDATE audit_logs SET detail = '被改過的內容' WHERE id = :i"),
                {"i": rid},
            )
    assert "append-only" in str(exc.value), str(exc.value)


def test_cannot_delete_audit_row(engine):
    with engine.begin() as conn:
        rid = _insert_audit(conn)
    with engine.begin() as conn:
        with pytest.raises(Exception) as exc:
            conn.execute(text("DELETE FROM audit_logs WHERE id = :i"), {"i": rid})
    assert "append-only" in str(exc.value), str(exc.value)


def test_row_survives_the_rejected_attempts(engine):
    """拒絕之後那一列還在,而且內容沒變 —— 否則「擋下來了」是假的。"""
    with engine.begin() as conn:
        rid = _insert_audit(conn)
    for stmt in (
        "UPDATE audit_logs SET detail = 'x' WHERE id = :i",
        "DELETE FROM audit_logs WHERE id = :i",
    ):
        with engine.begin() as conn:
            with pytest.raises(Exception):
                conn.execute(text(stmt), {"i": rid})
    with engine.begin() as conn:
        detail = conn.execute(
            text("SELECT detail FROM audit_logs WHERE id = :i"), {"i": rid}
        ).scalar_one()
    assert detail == "原始內容"


@pytest.mark.parametrize("table", LEDGERS)
def test_every_ledger_has_both_triggers(engine, table):
    with engine.begin() as conn:
        names = {
            row[0]
            for row in conn.execute(
                text(
                    "SELECT tgname FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
                    "WHERE c.relname = :t AND NOT t.tgisinternal"
                ),
                {"t": table},
            )
        }
    assert f"anila_{table}_no_update" in names, names
    assert f"anila_{table}_no_delete" in names, names


@pytest.mark.parametrize("table", LEDGERS)
def test_truncate_is_also_blocked_by_privilege(engine, table):
    """TRUNCATE 不會觸發 row-level trigger —— 所以它靠 REVOKE 擋。

    只有 `csp_app` role 存在時這條才有意義(乾淨測試庫可能沒有那個 role)。
    """
    with engine.begin() as conn:
        role_exists = conn.execute(
            text("SELECT 1 FROM pg_roles WHERE rolname = 'csp_app'")
        ).first()
        if not role_exists:
            pytest.skip("此測試庫沒有 csp_app role —— REVOKE 無標的")
        granted = {
            row[0]
            for row in conn.execute(
                text(
                    "SELECT privilege_type FROM information_schema.role_table_grants "
                    "WHERE grantee = 'csp_app' AND table_name = :t"
                ),
                {"t": table},
            )
        }
    assert "TRUNCATE" not in granted, granted
    assert "DELETE" not in granted, granted


# ── 唯一允許的例外:actor 外鍵轉 NULL ────────────────────────────────────────

def test_nulling_the_actor_fk_is_allowed(engine):
    """硬刪使用者時要保留稽核列,只把 actor 參照設 NULL —— 這不是內容變動。"""
    with engine.begin() as conn:
        uid = conn.execute(
            text(
                "INSERT INTO users (username, hashed_password, role, is_active, "
                "is_approved) VALUES ('appendonly-' || :uniq, 'x', 'user', true, true) "
                "RETURNING id"
            ),
            # 每次呼叫都唯一:上一輪若在清理前就失敗,殘留的使用者名會讓下一輪
            # 撞 unique 而看起來像功能壞掉。
            {"uniq": uuid.uuid4().hex[:8]},
        ).scalar_one()
        rid = conn.execute(
            text(
                "INSERT INTO audit_logs (action, resource_type, status, detail, "
                "actor_user_id) VALUES ('probe', 'test', 'success', '保留我', :u) "
                "RETURNING id"
            ),
            {"u": uid},
        ).scalar_one()

    with engine.begin() as conn:
        conn.execute(
            text("UPDATE audit_logs SET actor_user_id = NULL WHERE id = :i"),
            {"i": rid},
        )

    with engine.begin() as conn:
        row = conn.execute(
            text("SELECT detail, actor_user_id FROM audit_logs WHERE id = :i"),
            {"i": rid},
        ).one()
    assert row[0] == "保留我", "內容被動到了"
    assert row[1] is None
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM users WHERE id = :u"), {"u": uid})


def test_nulling_the_actor_plus_content_change_is_rejected(engine):
    """例外必須**只**是那一欄 —— 否則就成了改內容的後門。

    這條是這支測試裡最重要的一條:一個「允許 actor 轉 NULL」的 trigger 如果沒有
    同時檢查其餘欄位不變,攻擊者只要在同一個 UPDATE 裡順便改 detail 就繞過了。
    """
    with engine.begin() as conn:
        uid = conn.execute(
            text(
                "INSERT INTO users (username, hashed_password, role, is_active, "
                "is_approved) VALUES ('appendonly-' || :uniq, 'x', 'user', true, true) "
                "RETURNING id"
            ),
            # 每次呼叫都唯一:上一輪若在清理前就失敗,殘留的使用者名會讓下一輪
            # 撞 unique 而看起來像功能壞掉。
            {"uniq": uuid.uuid4().hex[:8]},
        ).scalar_one()
        rid = conn.execute(
            text(
                "INSERT INTO audit_logs (action, resource_type, status, detail, "
                "actor_user_id) VALUES ('probe', 'test', 'success', '原始內容', :u) "
                "RETURNING id"
            ),
            {"u": uid},
        ).scalar_one()

    with engine.begin() as conn:
        with pytest.raises(Exception) as exc:
            conn.execute(
                text(
                    "UPDATE audit_logs SET actor_user_id = NULL, detail = '偷改的' "
                    "WHERE id = :i"
                ),
                {"i": rid},
            )
    assert "append-only" in str(exc.value), str(exc.value)

    with engine.begin() as conn:
        detail = conn.execute(
            text("SELECT detail FROM audit_logs WHERE id = :i"), {"i": rid}
        ).scalar_one()
        assert detail == "原始內容"
        conn.execute(text("UPDATE audit_logs SET actor_user_id = NULL WHERE id = :i"), {"i": rid})
        conn.execute(text("DELETE FROM users WHERE id = :u"), {"u": uid})


def test_classification_events_ondelete_set_null_still_works(engine):
    """`classification_events` 的 actor FK 是 ON DELETE SET NULL。

    BEFORE UPDATE trigger 對 **referential action** 也會觸發,所以如果 trigger 沒
    放行那條路徑,刪使用者就會失敗 —— 而那是一個很難查的 500。
    """
    with engine.begin() as conn:
        uid = conn.execute(
            text(
                "INSERT INTO users (username, hashed_password, role, is_active, "
                "is_approved) VALUES ('appendonly-' || :uniq, 'x', 'user', true, true) "
                "RETURNING id"
            ),
            # 每次呼叫都唯一:上一輪若在清理前就失敗,殘留的使用者名會讓下一輪
            # 撞 unique 而看起來像功能壞掉。
            {"uniq": uuid.uuid4().hex[:8]},
        ).scalar_one()
        eid = conn.execute(
            text(
                "INSERT INTO classification_events (resource_type, resource_id, "
                "previous_level, new_level, reason, actor_user_id) "
                "VALUES ('conversation', '1', '無機密', '機密', 'manual', :u) "
                "RETURNING id"
            ),
            {"u": uid},
        ).scalar_one()

    with engine.begin() as conn:
        conn.execute(text("DELETE FROM users WHERE id = :u"), {"u": uid})

    with engine.begin() as conn:
        row = conn.execute(
            text(
                "SELECT actor_user_id, new_level FROM classification_events WHERE id = :i"
            ),
            {"i": eid},
        ).one()
    assert row[0] is None, "referential action 沒有生效"
    assert row[1] == "機密", "分類事件的內容被動到了"
