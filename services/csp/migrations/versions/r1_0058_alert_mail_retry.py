# -*- coding: utf-8 -*-
"""警報寄信失敗後有限次重試。

同一指紋在解決前仍只成功寄一封。寄失敗留下空的 sent_at、嘗試次數與
下次時間，背景迴圈再試。三次之後停，直到警報解決後重開。

Revision ID: r1_0058
Revises: r1_0057
Create Date: 2026-09-28
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "r1_0058"
down_revision: Union[str, None] = "r1_0057"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column("alert_mail_deliveries", "sent_at", nullable=True)
    op.add_column(
        "alert_mail_deliveries",
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "alert_mail_deliveries",
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "alert_mail_deliveries",
        sa.Column("category", sa.String(length=50), nullable=True),
    )
    op.add_column(
        "alert_mail_deliveries",
        sa.Column("severity", sa.String(length=20), nullable=True),
    )
    op.add_column(
        "alert_mail_deliveries",
        sa.Column("title", sa.String(length=200), nullable=True),
    )
    op.add_column(
        "alert_mail_deliveries",
        sa.Column("message", sa.Text(), nullable=True),
    )
    op.add_column(
        "alert_mail_deliveries",
        sa.Column("source_type", sa.String(length=50), nullable=True),
    )
    op.add_column(
        "alert_mail_deliveries",
        sa.Column("source_id", sa.String(length=100), nullable=True),
    )


def downgrade() -> None:
    op.execute("DELETE FROM alert_mail_deliveries WHERE sent_at IS NULL")
    op.alter_column("alert_mail_deliveries", "sent_at", nullable=False)
    op.drop_column("alert_mail_deliveries", "source_id")
    op.drop_column("alert_mail_deliveries", "source_type")
    op.drop_column("alert_mail_deliveries", "message")
    op.drop_column("alert_mail_deliveries", "title")
    op.drop_column("alert_mail_deliveries", "severity")
    op.drop_column("alert_mail_deliveries", "category")
    op.drop_column("alert_mail_deliveries", "next_retry_at")
    op.drop_column("alert_mail_deliveries", "attempt_count")
