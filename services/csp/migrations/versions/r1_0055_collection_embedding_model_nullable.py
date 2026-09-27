# -*- coding: utf-8 -*-
"""知識庫的嵌入模型可以先空著。

沒有平台嵌入角色時，建庫不再寫入模型名稱。欄位改成可空；
入庫會等到治理中心設好角色再寫上真正的名稱。

Revision ID: r1_0055
Revises: r1_0054
Create Date: 2026-09-27
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "r1_0055"
down_revision: Union[str, None] = "r1_0054"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        "ingestion_collections",
        "embedding_model",
        existing_type=sa.String(length=200),
        nullable=True,
    )


def downgrade() -> None:
    op.execute(
        "UPDATE ingestion_collections "
        "SET embedding_model = '' "
        "WHERE embedding_model IS NULL"
    )
    op.alter_column(
        "ingestion_collections",
        "embedding_model",
        existing_type=sa.String(length=200),
        nullable=False,
    )
