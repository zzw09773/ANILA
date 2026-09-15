# -*- coding: utf-8 -*-
"""Persist the latest compact summary on conversations.

Only one snapshot is kept: summary text, the boundary message it covers,
and when it was written. Deleting that message SET NULLs the FK; the
application layer also clears compact_summary.

Revision ID: r1_0042
Revises: r1_0041
Create Date: 2026-09-15
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "r1_0042"
down_revision: Union[str, None] = "r1_0041"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "conversations",
        sa.Column("compact_summary", sa.Text(), nullable=True),
    )
    op.add_column(
        "conversations",
        sa.Column("compact_boundary_message_id", sa.Integer(), nullable=True),
    )
    op.add_column(
        "conversations",
        sa.Column("compact_updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_conversations_compact_boundary_message_id",
        "conversations",
        "messages",
        ["compact_boundary_message_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_conversations_compact_boundary_message_id",
        "conversations",
        type_="foreignkey",
    )
    op.drop_column("conversations", "compact_updated_at")
    op.drop_column("conversations", "compact_boundary_message_id")
    op.drop_column("conversations", "compact_summary")
