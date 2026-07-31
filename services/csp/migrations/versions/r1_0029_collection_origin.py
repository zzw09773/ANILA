# -*- coding: utf-8 -*-
"""ingestion_collections.origin —— 知識庫屬於哪個產品面。

Revision ID: r1_0029
Revises: r1_0028
Create Date: 2026-07-31

SYSTEM-MAP §1 / §5：CSP 專案知識庫與 ANILALM 個人知識庫各自獨立，
但同一張 ``ingestion_collections`` 表、同一支 list API，過去只濾
``created_by``，所以治理中心建的庫會出現在 ANILALM「你的知識庫」。

做法比照 ``conversations.origin``（migration 0023）：多一欄 provenance
標籤，list 用 query 參數過濾。不另開 ``/api/personal`` 雙掛載——那是
attic ``a76ccb5`` 的第二套機制；本輪跟對話先例走。

既有列保持 ``NULL``：擁有者裁定「看不見既有語料」比「在錯的 App
多看一眼」更糟，所以 list 在指定 origin 時仍會帶上 ``origin IS NULL``
的舊列。新建立必須由呼叫端寫入 ``csp`` 或 ``anilalm``。

``origin`` 是產品貨架分區，不是授權邊界；ownership / 密等 latch /
檢索 RLS 都不讀它。
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "r1_0029"
down_revision: Union[str, None] = "r1_0028"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "ingestion_collections"
_COLUMN = "origin"
_CHECK = "ck_ingestion_collections_origin"


def upgrade() -> None:
    op.add_column(
        _TABLE,
        sa.Column(_COLUMN, sa.String(length=32), nullable=True),
    )
    op.create_check_constraint(
        _CHECK,
        _TABLE,
        f"{_COLUMN} IS NULL OR {_COLUMN} IN ('csp', 'anilalm')",
    )
    op.create_index(
        "ix_ingestion_collections_origin",
        _TABLE,
        [_COLUMN],
    )


def downgrade() -> None:
    op.drop_index("ix_ingestion_collections_origin", table_name=_TABLE)
    op.drop_constraint(_CHECK, _TABLE, type_="check")
    op.drop_column(_TABLE, _COLUMN)
