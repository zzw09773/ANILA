# -*- coding: utf-8 -*-
"""帳號生命週期：閒置停用原因、知識庫所屬單位、每日試算狀態。

Revision ID: r1_0063
Revises: r1_0062
Create Date: 2026-09-29
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "r1_0063"
down_revision: Union[str, None] = "r1_0062"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("disabled_reason", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "ingestion_collections",
        sa.Column("department_id", sa.Integer(), nullable=True),
    )
    op.create_index(
        "ix_ingestion_collections_department_id",
        "ingestion_collections",
        ["department_id"],
    )
    op.create_foreign_key(
        "fk_ingestion_collections_department_id",
        "ingestion_collections",
        "departments",
        ["department_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_table(
        "account_inactivity_state",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "preview_completed",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column(
            "phase",
            sa.String(length=16),
            nullable=False,
            server_default="pending",
        ),
        sa.Column(
            "notice_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "notice_days",
            sa.Integer(),
            nullable=False,
            server_default="180",
        ),
        sa.Column("last_run_on", sa.Date(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    state = sa.table(
        "account_inactivity_state",
        sa.column("id", sa.Integer),
        sa.column("preview_completed", sa.Boolean),
        sa.column("phase", sa.String),
        sa.column("notice_count", sa.Integer),
        sa.column("notice_days", sa.Integer),
    )
    op.bulk_insert(
        state,
        [
            {
                "id": 1,
                "preview_completed": False,
                "phase": "pending",
                "notice_count": 0,
                "notice_days": 180,
            }
        ],
    )


def downgrade() -> None:
    op.drop_table("account_inactivity_state")
    op.drop_constraint(
        "fk_ingestion_collections_department_id",
        "ingestion_collections",
        type_="foreignkey",
    )
    op.drop_index(
        "ix_ingestion_collections_department_id",
        table_name="ingestion_collections",
    )
    op.drop_column("ingestion_collections", "department_id")
    op.drop_column("users", "disabled_reason")
