# -*- coding: utf-8 -*-
"""export_records 容納對話匯出:artifact_id 放寬為 NULL + 新增 conversation_id。

Revision ID: r1_0038
Revises: r1_0037
Create Date: 2026-07-26

為什麼有這支(補救計畫 W1-1④)
------------------------------
`apps/anila-shell` 的 `exportConversation` 原本**完全沒有分類 gate、沒有密等
頁首、也不落任何稽核列**。一份離開平台的對話檔於是三件事都不成立:收檔者不知道
密等、沒人知道是誰帶出去的、`export_records` 一列都沒有。

新端點 `POST /api/conversations/{id}/export-record` 要把那一列補上,但既有的
`export_records` 是為 **artifact** 匯出設計的:

* ``artifact_id`` 是 ``NOT NULL`` —— 對話匯出沒有 artifact,寫不進去。
* 沒有任何欄位能指出「匯出的是哪一個對話」—— 只有 exporter / 密等 / 時間的話,
  這一列答不出「匯出了什麼」,稽核就只剩「有人匯出過某個東西」。這與 W1-1 想修的
  「稽核只剩『有人看過某個受控東西』」是同一種無用紀錄,所以一併補。

兩個欄位變更
------------
1. ``artifact_id``:``NOT NULL`` → ``NULL``。既有列全部有值,放寬不影響它們;
   反向收緊會失敗(見 downgrade 的守衛)。
2. 新增 ``conversation_id``(nullable, FK → ``conversations.id``,
   ``ondelete=SET NULL``)+ 索引。

**為什麼 SET NULL 而不是 CASCADE**:CASCADE 會讓「刪掉對話」等於「消滅該對話的
匯出紀錄」—— 想掩蓋外流的人第一件事就是刪對話。SET NULL 保留紀錄本體(exporter /
密等 / 時間 / 格式都還在),只失去指標。

**為什麼不順手把落列做成 NOT NULL 的二選一約束**(artifact_id 與 conversation_id
恰有一個非空):現行 artifact 路徑有 `artifact_id` 為真、`conversation_id` 為空的
列,新路徑相反,看似適合 CHECK 約束。但 doc 01 之後若出現第三種匯出來源
(例如知識庫整批匯出),那條約束會變成擋路的東西,而放寬約束是另一次 migration。
形狀約束留給 W3 的 artifact/export 統一重構一起決定。

downgrade
---------
對稱還原:先確認沒有「artifact_id 為空」的列(那是對話匯出的紀錄,收緊回
NOT NULL 只能靠刪掉它們 —— 刪稽核列不是 migration 該做的事,寧可失敗要求人來看),
再刪 ``conversation_id`` 並把 ``artifact_id`` 收回 NOT NULL。
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "r1_0038"
down_revision: Union[str, None] = "r1_0036"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_TABLE = "export_records"
_COLUMN = "conversation_id"
_INDEX = "ix_export_records_conversation_id"


def _columns(bind) -> set[str]:
    insp = sa.inspect(bind)
    if _TABLE not in insp.get_table_names():
        return set()
    return {c["name"] for c in insp.get_columns(_TABLE)}


def _indexes(bind) -> set[str]:
    insp = sa.inspect(bind)
    if _TABLE not in insp.get_table_names():
        return set()
    return {i["name"] for i in insp.get_indexes(_TABLE)}


def upgrade() -> None:
    bind = op.get_bind()
    if _TABLE not in sa.inspect(bind).get_table_names():
        # 表由 r1_0007 建立;不存在代表這個部署還沒跑到那支,沒有什麼要改。
        return

    # batch_alter_table:SQLite 不支援 ALTER COLUMN,批次模式會以「重建表」實作。
    # 生產是 PostgreSQL(原生支援),但開發/測試路徑上有 SQLite,兩邊都要能跑。
    with op.batch_alter_table(_TABLE) as batch:
        batch.alter_column(
            "artifact_id",
            existing_type=sa.Integer(),
            nullable=True,
        )

    if _COLUMN not in _columns(bind):
        op.add_column(
            _TABLE,
            sa.Column(
                _COLUMN, sa.Integer(),
                sa.ForeignKey("conversations.id", ondelete="SET NULL"),
                nullable=True,
            ),
        )
    if _INDEX not in _indexes(bind):
        op.create_index(_INDEX, _TABLE, [_COLUMN])


def downgrade() -> None:
    bind = op.get_bind()
    if _TABLE not in sa.inspect(bind).get_table_names():
        return

    orphan = bind.execute(
        sa.text(f"SELECT COUNT(*) FROM {_TABLE} WHERE artifact_id IS NULL")
    ).scalar_one()
    if orphan:
        raise RuntimeError(
            f"拒絕降版:{_TABLE} 有 {orphan} 列 artifact_id IS NULL(對話匯出的"
            "稽核紀錄)。收緊回 NOT NULL 只能靠刪掉它們,而刪稽核列不是 migration"
            "該做的事。請先由資料權責人決定這些列的處置,再執行降版。"
        )

    if _INDEX in _indexes(bind):
        op.drop_index(_INDEX, table_name=_TABLE)
    if _COLUMN in _columns(bind):
        op.drop_column(_TABLE, _COLUMN)
    with op.batch_alter_table(_TABLE) as batch:
        batch.alter_column(
            "artifact_id",
            existing_type=sa.Integer(),
            nullable=False,
        )
