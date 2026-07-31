# -*- coding: utf-8 -*-
"""messages.rating_score —— 拇指旁的 1–5／6–10 細分分數。

Revision ID: r1_0030
Revises: r1_0029
Create Date: 2026-07-31

既有 ``messages.rating`` 只存 ``up``／``down``。維運者要區分「完全錯」與
「差不多、差一點」時,二元值不夠用,所以在旁邊加可空的整數欄
``rating_score``:

- 讚 → 6–10;爛 → 1–5。兩個五分尺,不是一條十分尺。
- ``NULL`` = 只按了拇指、沒選數字 —— 既有列升級後全部是這個狀態,
  拇指訊號不丟。
- CheckConstraint 把「拇指 ↔ 分數」配對鎖在 DB 層,API 呼叫者繞不過。

⚠ ``down_revision = r1_0029``:本 worktree 撰寫時 r1_0029 由另一包同步寫入,
  若合併時鏈斷了,先對齊那支再升本支。

downgrade 砍欄位與約束;掉的只是細分分數,``rating`` 拇指值仍在。
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "r1_0030"
down_revision: Union[str, None] = "r1_0029"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_CK = "ck_messages_rating_score_matches_thumb"


def upgrade() -> None:
    op.add_column(
        "messages",
        sa.Column("rating_score", sa.Integer(), nullable=True),
    )
    op.create_check_constraint(
        _CK,
        "messages",
        "("
        "rating_score IS NULL OR "
        "(rating = 'up' AND rating_score BETWEEN 6 AND 10) OR "
        "(rating = 'down' AND rating_score BETWEEN 1 AND 5)"
        ")",
    )


def downgrade() -> None:
    op.drop_constraint(_CK, "messages", type_="check")
    op.drop_column("messages", "rating_score")
