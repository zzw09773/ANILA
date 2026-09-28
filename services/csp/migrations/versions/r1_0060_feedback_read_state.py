# -*- coding: utf-8 -*-
"""使用者回饋的已讀水位，依管理員各記一筆。

新評分才寫 messages.rated_at。既有評分留空，上線時不會被當成新回饋。

Revision ID: r1_0060
Revises: r1_0059
Create Date: 2026-09-28
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "r1_0060"
down_revision: Union[str, None] = "r1_0059"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "messages",
        sa.Column("rated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_messages_rated_at", "messages", ["rated_at"])
    op.create_table(
        "feedback_read_states",
        sa.Column("user_id", sa.Integer(), primary_key=True),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_feedback_read_states_user_id",
            ondelete="CASCADE",
        ),
    )


def downgrade() -> None:
    op.drop_table("feedback_read_states")
    op.drop_index("ix_messages_rated_at", table_name="messages")
    op.drop_column("messages", "rated_at")
