# -*- coding: utf-8 -*-
"""交接訪談：固定題目的回答，確認後才有清單。

Revision ID: r1_0075
Revises: r1_0074
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "r1_0075"
down_revision: Union[str, None] = "r1_0074"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

json_type = sa.JSON().with_variant(JSONB, "postgresql")


def upgrade() -> None:
    op.create_table(
        "handover_interviews",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column(
            "project_label",
            sa.String(length=80),
            nullable=False,
            server_default="",
        ),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("answers", json_type, nullable=False),
        sa.Column("checklist", json_type, nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_handover_interviews_user_id",
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_handover_interviews_user_id",
        "handover_interviews",
        ["user_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_handover_interviews_user_id", table_name="handover_interviews")
    op.drop_table("handover_interviews")
