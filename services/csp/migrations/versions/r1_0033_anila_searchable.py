# -*- coding: utf-8 -*-
"""ANILA 可直接檢索的知識庫標記。

SYSTEM-MAP §3 的主線圖寫著 Router 判斷「不需要 agent」時用院內知識庫直答，
這條路一直沒實作。本 migration 加上那個標記，並把「已標記的庫不可能是機密」
交給資料庫的 CHECK 約束閉合 —— 不是交給應用程式。

⚠ 那條 CHECK 的價值在於：升密的程式如果忘了先取消標記,UPDATE 會**當場失敗**,
而不是靜默留下一個被全院檢索的機密庫。耦合看得見、會叫。

⚠ 無機密是**必要條件不是觸發條件**：這條約束只禁止「機密＋已標記」這個組合,
不會讓任何庫因為密等低就自動變成已標記。

⚠ ``down_revision = r1_0032``：r1_0032 是 2026-08-07 的大小寫正規化。

Revision ID: r1_0033
Revises: r1_0032
Create Date: 2026-08-07
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "r1_0033"
down_revision: Union[str, None] = "r1_0032"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "ingestion_collections"
_COLUMN = "anila_searchable"
_CHECK = "ck_ingestion_collections_anila_searchable_unclassified"


def upgrade() -> None:
    op.add_column(
        _TABLE,
        sa.Column(
            _COLUMN,
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.create_check_constraint(
        _CHECK,
        _TABLE,
        f"NOT {_COLUMN} OR classification_level = '無機密'",
    )


def downgrade() -> None:
    op.drop_constraint(_CHECK, _TABLE, type_="check")
    op.drop_column(_TABLE, _COLUMN)
