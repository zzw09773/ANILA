# -*- coding: utf-8 -*-
"""附件沿用對話的四級分類欄位。

長文回答存成該對話的附件時，等級跟對話走。欄位與其他資源相同。

Revision ID: r1_0054
Revises: r1_0053
Create Date: 2026-09-27
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "r1_0054"
down_revision: Union[str, None] = "r1_0053"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_UNCLASSIFIED = "無機密"


def upgrade() -> None:
    op.add_column(
        "attachments",
        sa.Column(
            "classification_level",
            sa.String(length=20),
            nullable=False,
            server_default=_UNCLASSIFIED,
        ),
    )
    op.add_column(
        "attachments",
        sa.Column("classification_latched_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "attachments",
        sa.Column("classification_source", sa.String(length=50), nullable=True),
    )
    op.add_column(
        "attachments",
        sa.Column(
            "classification_event_id",
            sa.Integer(),
            sa.ForeignKey("classification_events.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("attachments", "classification_event_id")
    op.drop_column("attachments", "classification_source")
    op.drop_column("attachments", "classification_latched_at")
    op.drop_column("attachments", "classification_level")
