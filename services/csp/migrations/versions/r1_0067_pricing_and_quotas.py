# -*- coding: utf-8 -*-
"""模型單價、用量額度、被擋下的紀錄。

單價只追加。額度預設不限制。貨幣放在既有的 platform_settings，不另開表。

Revision ID: r1_0067
Revises: r1_0066
Create Date: 2026-10-01
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "r1_0067"
down_revision: Union[str, None] = "r1_0066"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "model_prices",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("model_id", sa.Integer(), nullable=False),
        sa.Column("input_micros", sa.BigInteger(), nullable=True),
        sa.Column("output_micros", sa.BigInteger(), nullable=True),
        sa.Column("reasoning_micros", sa.BigInteger(), nullable=True),
        sa.Column("effective_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by_user_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(
            ["model_id"],
            ["model_registry.id"],
            name="fk_model_prices_model_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.id"],
            name="fk_model_prices_created_by",
            ondelete="SET NULL",
        ),
    )
    op.create_index(
        "ix_model_prices_model_effective",
        "model_prices",
        ["model_id", "effective_at"],
    )

    op.create_table(
        "usage_quotas",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("scope_type", sa.String(length=16), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("department_id", sa.Integer(), nullable=True),
        sa.Column("api_key_id", sa.Integer(), nullable=True),
        sa.Column("period", sa.String(length=16), nullable=False),
        sa.Column("metric", sa.String(length=16), nullable=False),
        sa.Column("limit_value", sa.BigInteger(), nullable=True),
        sa.Column(
            "warn_percent",
            sa.Integer(),
            nullable=False,
            server_default="80",
        ),
        sa.Column(
            "on_limit",
            sa.String(length=16),
            nullable=False,
            server_default="warn",
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by_user_id", sa.Integer(), nullable=True),
        sa.Column("updated_by_user_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_usage_quotas_user_id", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["department_id"],
            ["departments.id"],
            name="fk_usage_quotas_department_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["api_key_id"],
            ["api_keys.id"],
            name="fk_usage_quotas_api_key_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.id"],
            name="fk_usage_quotas_created_by",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_user_id"],
            ["users.id"],
            name="fk_usage_quotas_updated_by",
            ondelete="SET NULL",
        ),
    )
    op.create_index("ix_usage_quotas_user", "usage_quotas", ["user_id"])
    op.create_index("ix_usage_quotas_department", "usage_quotas", ["department_id"])
    op.create_index("ix_usage_quotas_api_key", "usage_quotas", ["api_key_id"])

    op.create_table(
        "quota_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("quota_id", sa.Integer(), nullable=True),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("api_key_id", sa.Integer(), nullable=True),
        sa.Column("department_id", sa.Integer(), nullable=True),
        sa.Column("action", sa.String(length=16), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["quota_id"],
            ["usage_quotas.id"],
            name="fk_quota_events_quota_id",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_quota_events_user_id", ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["api_key_id"],
            ["api_keys.id"],
            name="fk_quota_events_api_key_id",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["department_id"],
            ["departments.id"],
            name="fk_quota_events_department_id",
            ondelete="SET NULL",
        ),
    )
    op.create_index("ix_quota_events_occurred", "quota_events", ["occurred_at"])
    op.create_index("ix_quota_events_user", "quota_events", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_quota_events_user", table_name="quota_events")
    op.drop_index("ix_quota_events_occurred", table_name="quota_events")
    op.drop_table("quota_events")
    op.drop_index("ix_usage_quotas_api_key", table_name="usage_quotas")
    op.drop_index("ix_usage_quotas_department", table_name="usage_quotas")
    op.drop_index("ix_usage_quotas_user", table_name="usage_quotas")
    op.drop_table("usage_quotas")
    op.drop_index("ix_model_prices_model_effective", table_name="model_prices")
    op.drop_table("model_prices")
