# -*- coding: utf-8 -*-
"""messages.parent_id + conversations.active_leaf_message_id + legal_hold —— W2-3 / C3.

Revision ID: r1_0043
Revises: r1_0042
Create Date: 2026-07-27

``down_revision`` 指向 ``r1_0042``(W2-11 的抽查帳表),於整合時由整合者
設定完成——兩包無資料相依,先後順序不影響結果。

════════════════════════════════════════════════════════════════════════════
C3 設計依據(``docs/planning/platform-remediation-plan-2026-07-26.md`` §C3)
════════════════════════════════════════════════════════════════════════════

① ``messages.parent_id`` —— 訊息改為樹。編輯 user / 重生 assistant 都是在原
   parent 下長**兄弟**節點,舊子樹保留。刪除語意從「截斷硬刪」改成「長枝」。
② ``conversations.active_leaf_message_id`` —— 標定目前顯示的葉;NULL = 最新葉
   (向後相容:舊 client / 未編輯過的對話行為不變)。
③ ``conversations.legal_hold`` —— 編輯路徑的保全閘門(C3 §c / W2-3 驗收②)。
   保全中的對話拒絕 edit(4xx);保留/reaper 以**全樹**為單位時也讀這欄。
   ``legal_hold_reason`` 留給後續 W3-12d 生命周期欄位批次,本包只做布林閘門。

回填:per conversation 依 ``(created_at, id)`` 排序,``parent_id`` = 前一則 id
(現況線性對話 = 單鏈)。**只在「本次 upgrade 親手建出 parent_id 欄」時執行**
—— 欄已存在 = 這是重跑,樹結構已是權威資料,一個 byte 都不准動。理由見
``_backfill_parent_ids``。

⚠ ``downgrade`` 有損:見該函式 docstring。
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "r1_0043"
down_revision: Union[str, None] = "r1_0042"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_BACKFILL_BATCH = 2000
_BACKFILL_DONE_TABLE = "anila_r1_0043_parent_backfill_done"


def _columns(bind, table: str) -> set[str]:
    insp = sa.inspect(bind)
    if table not in insp.get_table_names():
        return set()
    return {c["name"] for c in insp.get_columns(table)}


def _indexes(bind, table: str) -> set[str]:
    insp = sa.inspect(bind)
    if table not in insp.get_table_names():
        return set()
    return {i["name"] for i in insp.get_indexes(table)}


def _fk_names(bind, table: str) -> set[str]:
    insp = sa.inspect(bind)
    if table not in insp.get_table_names():
        return set()
    return {fk["name"] for fk in insp.get_foreign_keys(table) if fk.get("name")}


def upgrade() -> None:
    bind = op.get_bind()
    msg_cols = _columns(bind, "messages")
    conv_cols = _columns(bind, "conversations")
    msg_fks = _fk_names(bind, "messages")
    conv_fks = _fk_names(bind, "conversations")
    msg_idx = _indexes(bind, "messages")

    # 唯一可信的「這是初次遷移、不是重跑」訊號:欄是不是本次親手建出來的。
    # marker 表不夠力 —— 任何在 marker 機制出現前就升級過的資料庫都沒有它。
    column_created = "parent_id" not in msg_cols
    if column_created:
        op.add_column(
            "messages",
            sa.Column("parent_id", sa.Integer(), nullable=True),
        )
    if "fk_messages_parent_id" not in msg_fks:
        op.create_foreign_key(
            "fk_messages_parent_id",
            "messages",
            "messages",
            ["parent_id"],
            ["id"],
            ondelete="SET NULL",
        )
    if "ix_messages_conversation_id_parent_id" not in msg_idx:
        op.create_index(
            "ix_messages_conversation_id_parent_id",
            "messages",
            ["conversation_id", "parent_id"],
        )

    if "active_leaf_message_id" not in conv_cols:
        op.add_column(
            "conversations",
            sa.Column("active_leaf_message_id", sa.Integer(), nullable=True),
        )
    if "fk_conversations_active_leaf_message_id" not in conv_fks:
        op.create_foreign_key(
            "fk_conversations_active_leaf_message_id",
            "conversations",
            "messages",
            ["active_leaf_message_id"],
            ["id"],
            ondelete="SET NULL",
        )

    if "legal_hold" not in conv_cols:
        op.add_column(
            "conversations",
            sa.Column(
                "legal_hold",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("false"),
            ),
        )

    _backfill_parent_ids(column_created=column_created)


def _backfill_parent_ids(*, column_created: bool) -> None:
    """線性歷史 → 單鏈。可分段。

    **主閘門 ``column_created``**:回填只在「``messages.parent_id`` 是本次
    upgrade 建出來的」那一次合法。欄已存在 → 這是重跑(alembic version 被
    rewind、``stamp`` 過、或多次執行同一版),樹結構已是應用層權威資料,直接
    返回,不寫任何一列。

    為什麼不能靠 per-conversation 的 ``HAVING`` 過濾:「整段對話沒有任何非
    NULL parent_id」既是「尚未回填的線性歷史」,也是「編輯首則使用者訊息後留
    下的兩個合法 NULL 根兄弟」(``PUT .../messages/1/edit`` 走一般 API 就會
    產生 ``[(1,None),(2,None)]``)。兩者在 SQL 層無從分辨,把後者接成單鏈就是
    資料毀損。有非 NULL parent 的對話能被過濾掉只是巧合,全 NULL 的擋不住。

    次要閘門:完成後寫入 ``anila_r1_0043_parent_backfill_done`` marker,同一次
    建欄流程內重入時短路。marker **不是**重跑防線(舊資料庫沒有它)。
    """
    if not column_created:
        return
    conn = op.get_bind()
    insp = sa.inspect(conn)
    if _BACKFILL_DONE_TABLE in insp.get_table_names():
        return

    # PostgreSQL: 用 ctid 批次,避免一次鎖整張 messages。
    # 外層逐對話處理,避免「對話內已有部分 parent_id」時批次半途被擋下。
    while True:
        cid_row = conn.execute(
            sa.text(
                """
                SELECT conversation_id
                FROM messages
                GROUP BY conversation_id
                HAVING COUNT(*) FILTER (WHERE parent_id IS NOT NULL) = 0
                   AND COUNT(*) > 1
                LIMIT 1
                """
            )
        ).first()
        if cid_row is None:
            break
        cid = int(cid_row[0])
        while True:
            result = conn.execute(
                sa.text(
                    """
                    WITH candidates AS (
                        SELECT m.ctid AS ctid
                        FROM messages m
                        WHERE m.conversation_id = :cid
                          AND m.parent_id IS NULL
                          AND EXISTS (
                              SELECT 1 FROM messages prev
                              WHERE prev.conversation_id = m.conversation_id
                                AND (prev.created_at, prev.id)
                                    < (m.created_at, m.id)
                          )
                        LIMIT :batch
                    ),
                    updated AS (
                        UPDATE messages m
                        SET parent_id = (
                            SELECT prev.id
                            FROM messages prev
                            WHERE prev.conversation_id = m.conversation_id
                              AND (prev.created_at, prev.id)
                                  < (m.created_at, m.id)
                            ORDER BY prev.created_at DESC, prev.id DESC
                            LIMIT 1
                        )
                        FROM candidates c
                        WHERE m.ctid = c.ctid
                        RETURNING 1
                    )
                    SELECT count(*) FROM updated
                    """
                ),
                {"cid": cid, "batch": _BACKFILL_BATCH},
            )
            n = int(result.scalar_one())
            if n == 0:
                break

    op.create_table(
        _BACKFILL_DONE_TABLE,
        sa.Column(
            "completed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )


def downgrade() -> None:
    """⚠ 有損 —— 樹結構無法還原。

    ``parent_id`` / ``active_leaf_message_id`` 一旦 drop,分枝關係就永久消失
    (欄裡的值沒有別處備份)。之後再 ``upgrade`` 會被當成初次遷移,把整段對話
    重新線性化:合法的 NULL 根兄弟(編輯首則產生的)會被接成單鏈,子樹順序改
    以 ``(created_at, id)`` 重排。有真實編輯/重生歷史的資料庫在 downgrade 前
    務必先備份;要保住樹就別 downgrade。
    """
    bind = op.get_bind()
    insp = sa.inspect(bind)
    conv_cols = _columns(bind, "conversations")
    msg_cols = _columns(bind, "messages")
    conv_fks = _fk_names(bind, "conversations")
    msg_fks = _fk_names(bind, "messages")
    msg_idx = _indexes(bind, "messages")

    if _BACKFILL_DONE_TABLE in insp.get_table_names():
        op.drop_table(_BACKFILL_DONE_TABLE)

    if "fk_conversations_active_leaf_message_id" in conv_fks:
        op.drop_constraint(
            "fk_conversations_active_leaf_message_id",
            "conversations",
            type_="foreignkey",
        )
    if "active_leaf_message_id" in conv_cols:
        op.drop_column("conversations", "active_leaf_message_id")
    if "legal_hold" in conv_cols:
        op.drop_column("conversations", "legal_hold")

    if "ix_messages_conversation_id_parent_id" in msg_idx:
        op.drop_index("ix_messages_conversation_id_parent_id", table_name="messages")
    if "fk_messages_parent_id" in msg_fks:
        op.drop_constraint("fk_messages_parent_id", "messages", type_="foreignkey")
    if "parent_id" in msg_cols:
        op.drop_column("messages", "parent_id")
