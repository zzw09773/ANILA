# -*- coding: utf-8 -*-
"""額度依對象的部分唯一索引，以及擋下紀錄的單位索引。

Revision ID: r1_0068
Revises: r1_0067
Create Date: 2026-10-01
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "r1_0068"
down_revision: Union[str, None] = "r1_0067"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index(
        "uq_usage_quotas_user_period_metric",
        "usage_quotas",
        ["user_id", "period", "metric"],
        unique=True,
        sqlite_where=sa.text("scope_type = 'user'"),
        postgresql_where=sa.text("scope_type = 'user'"),
    )
    op.create_index(
        "uq_usage_quotas_unit_period_metric",
        "usage_quotas",
        ["department_id", "period", "metric"],
        unique=True,
        sqlite_where=sa.text("scope_type = 'unit'"),
        postgresql_where=sa.text("scope_type = 'unit'"),
    )
    op.create_index(
        "uq_usage_quotas_api_key_period_metric",
        "usage_quotas",
        ["api_key_id", "period", "metric"],
        unique=True,
        sqlite_where=sa.text("scope_type = 'api_key'"),
        postgresql_where=sa.text("scope_type = 'api_key'"),
    )
    op.create_index(
        "ix_quota_events_department",
        "quota_events",
        ["department_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_quota_events_department", table_name="quota_events")
    op.drop_index("uq_usage_quotas_api_key_period_metric", table_name="usage_quotas")
    op.drop_index("uq_usage_quotas_unit_period_metric", table_name="usage_quotas")
    op.drop_index("uq_usage_quotas_user_period_metric", table_name="usage_quotas")
