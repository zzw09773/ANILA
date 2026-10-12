# -*- coding: utf-8 -*-
"""技能套用紀錄與釘選。

套用列保留 180 天，由既有的每日清理工作刪除。釘選沒有期限。

Revision ID: r1_0073
Revises: r1_0072
Create Date: 2026-10-12
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "r1_0073"
down_revision: Union[str, None] = "r1_0072"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "user_skill_usage",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("lineage_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_user_skill_usage_user_id",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "mode IN ('manual', 'auto')",
            name="ck_user_skill_usage_mode",
        ),
    )
    op.create_index(
        "ix_user_skill_usage_user_used",
        "user_skill_usage",
        ["user_id", "used_at"],
    )
    op.create_index(
        "ix_user_skill_usage_lineage",
        "user_skill_usage",
        ["lineage_id"],
    )
    op.create_table(
        "user_skill_favorites",
        sa.Column("user_id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("lineage_id", sa.Integer(), primary_key=True, nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_user_skill_favorites_user_id",
            ondelete="CASCADE",
        ),
    )


def downgrade() -> None:
    op.drop_table("user_skill_favorites")
    op.drop_index("ix_user_skill_usage_lineage", table_name="user_skill_usage")
    op.drop_index("ix_user_skill_usage_user_used", table_name="user_skill_usage")
    op.drop_table("user_skill_usage")
