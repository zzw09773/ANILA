"""Trigram GIN index on ``audit_logs.detail`` for fast ILIKE audit search.

為什麼需要
----------
合規調查的入口是「上週三誰改了那個模型設定」——`GET /api/audit-logs?q=...`
用 ILIKE '%q%' 搜 ``detail``(中文沒有 CJK tokenizer,只能子字串比對)。
``messages.content`` 與 ``conversations.title`` 早在 0044 就加了
``gin_trgm_ops``,同一份能力一直沒補到稽核側,而 ``audit_logs`` 是實測最大的
表 —— 等於每次合規搜尋都是全表掃。

為什麼 CONCURRENTLY
-------------------
普通 ``CREATE INDEX`` 會在 ``audit_logs`` 上拿 SHARE lock,擋掉整個建索引期間
的**所有稽核寫入**;而 ``ANILA_AUDIT_STRICT=1``(formal posture 契約值)下,
稽核寫不進去 = 請求 503。也就是說在最大的表上做普通 CREATE INDEX,等於部署期
間對外服務直接不可用。``CREATE INDEX CONCURRENTLY`` 不能在交易區塊內執行,
所以這裡用 alembic 的 ``autocommit_block()``。

代價要講清楚:``autocommit_block()`` 會把此前的 migration 一起 commit(本 repo
的 ``env.py`` 是整條 chain 一個交易),所以這支之後若有 migration 失敗,前面的
不會一起 rollback。本支是 chain 末端,實務上不受影響。

Revision ID: r1_0035
Revises: r1_0034
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op


revision: str = "r1_0035"
down_revision: Union[str, None] = "r1_0034"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

INDEX_NAME = "ix_audit_logs_detail_trgm"


def _drop_invalid_leftover(bind) -> None:
    """清掉上一次 CONCURRENTLY 失敗留下的 INVALID index。

    ``CREATE INDEX CONCURRENTLY`` 失敗(lock timeout / 連線斷)會留下一個
    **存在但 indisvalid=false** 的索引,查詢用不到它,而重跑時
    ``IF NOT EXISTS`` 會把它當成「已經有了」直接跳過 —— 索引永遠壞著而
    migration 每次都顯示成功。所以重建前先明確清除。
    """
    leftover = bind.exec_driver_sql(
        "SELECT 1 FROM pg_index i "
        "JOIN pg_class c ON c.oid = i.indexrelid "
        f"WHERE c.relname = '{INDEX_NAME}' AND NOT i.indisvalid"
    ).first()
    if leftover is not None:
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {INDEX_NAME}")


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        # SQLite(單元測試以 create_all 建 schema)沒有 pg_trgm。
        return
    with op.get_context().autocommit_block():
        op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
        _drop_invalid_leftover(op.get_bind())
        op.execute(
            f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {INDEX_NAME} "
            "ON audit_logs USING gin (detail gin_trgm_ops)"
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    with op.get_context().autocommit_block():
        # 只丟索引,不丟 pg_trgm extension —— 0044 的 messages.content /
        # conversations.title 也靠它,連坐拔掉會把對話搜尋一起弄壞。
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {INDEX_NAME}")
