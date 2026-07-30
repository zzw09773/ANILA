# -*- coding: utf-8 -*-
"""Agent session ownership for CSP resume authorisation (P2.4 H3).

Revision ID: r1_0025
Revises: r1_0024
Create Date: 2026-07-31
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "r1_0025"
down_revision: Union[str, None] = "r1_0024"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "agent_session_owners",
        sa.Column("session_id", sa.String(length=128), nullable=False),
        sa.Column("owner_user_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["owner_user_id"], ["users.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("session_id"),
    )
    op.create_index(
        "ix_agent_session_owners_owner_user_id",
        "agent_session_owners",
        ["owner_user_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_agent_session_owners_owner_user_id",
        table_name="agent_session_owners",
    )
    op.drop_table("agent_session_owners")
