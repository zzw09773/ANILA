# -*- coding: utf-8 -*-
"""附件 created_at 補上現在時間後改為必填。

未知年齡的列不在這一版刪檔。先蓋上時間，保存期限從這一刻起算。

Revision ID: r1_0061
Revises: r1_0060
Create Date: 2026-09-28
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "r1_0061"
down_revision: Union[str, None] = "r1_0060"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "UPDATE attachments SET created_at = CURRENT_TIMESTAMP "
        "WHERE created_at IS NULL"
    )
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("attachments") as batch:
            batch.alter_column(
                "created_at",
                existing_type=sa.DateTime(timezone=True),
                nullable=False,
            )
        return
    op.alter_column(
        "attachments",
        "created_at",
        existing_type=sa.DateTime(timezone=True),
        nullable=False,
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("attachments") as batch:
            batch.alter_column(
                "created_at",
                existing_type=sa.DateTime(timezone=True),
                nullable=True,
            )
        return
    op.alter_column(
        "attachments",
        "created_at",
        existing_type=sa.DateTime(timezone=True),
        nullable=True,
    )
