# -*- coding: utf-8 -*-
"""平台層級設定表（一列一個 key）。

第一個住戶是院內規章檢索的分數門檻。它之所以不能是常數：「找不到」與「找到了」
之間隔著的那個數字取決於嵌入模型，而嵌入模型隨時可能換。寫死成常數就等於把
「重新量一次」變成要改程式碼、重建映像、重啟容器才做得到的事。

⚠ 這張表**不預先塞任何列**。「沒有列」本身就是資訊：表示目前生效的是那個用
替代模型量出來的預設值（``KB_THRESHOLD_DEFAULT``），還沒有人拿上線的模型量過。
在這裡 seed 一列 0.3 進去，就會讓 API 回報 ``calibrated: true``，而事實上沒有
任何人看過任何一個分數 —— 那是這個功能唯一會出的假控制項。

⚠ ``down_revision = r1_0033``：r1_0033 是同一條線上的 ``anila_searchable`` 標記。

Revision ID: r1_0034
Revises: r1_0033
Create Date: 2026-08-07
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "r1_0034"
down_revision: Union[str, None] = "r1_0033"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "platform_settings"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("key", sa.String(length=120), primary_key=True, nullable=False),
        # 型別由各個 getter 自己解讀 —— 見 app/models/platform_setting.py 的
        # docstring（異質設定共用一張表，不為每種型別開一個欄位）。
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        # 帳號刪掉時設定要留著：設定不是那個人的財產。
        sa.Column(
            "updated_by_user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_table(_TABLE)
