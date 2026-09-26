# -*- coding: utf-8 -*-
"""agent 底層模型下線的不可用原因。

與 health_status、approval_status 分開。NULL 表示這項不擋派工。
停用或刪除底層模型時寫入原因，Router 因此略過該 agent。

Revision ID: r1_0049
Revises: r1_0048
Create Date: 2026-09-26
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "r1_0049"
down_revision: Union[str, None] = "r1_0048"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# 與 app.services.agent_availability.BASE_MODEL_OFFLINE 同一個原因碼。
# 遷移不 import app，避免 alembic 啟動時拉進整個服務。
_BASE_MODEL_OFFLINE = "base_model_offline"

_BACKFILL_UNAVAILABLE = sa.text(
    """
    UPDATE agents
    SET unavailable_reason = :reason
    WHERE approval_status = 'approved'
      AND unavailable_reason IS NULL
      AND (
            base_model_id IS NULL
            OR NOT EXISTS (
                SELECT 1
                FROM model_registry AS m
                WHERE m.id = agents.base_model_id
                  AND m.is_active IS TRUE
            )
      )
    """
)


def backfill_unavailable_reason(connection) -> None:
    """已核准、但底層模型停用或遺失的 agent 回填不可用原因。

    條件含 unavailable_reason IS NULL，重複執行不會改到已寫過的列，
    也不會動到尚未核准的 agent。
    """
    connection.execute(_BACKFILL_UNAVAILABLE, {"reason": _BASE_MODEL_OFFLINE})


def upgrade() -> None:
    op.add_column(
        "agents",
        sa.Column("unavailable_reason", sa.String(length=40), nullable=True),
    )
    op.create_index(
        "ix_agents_unavailable_reason",
        "agents",
        ["unavailable_reason"],
    )
    backfill_unavailable_reason(op.get_bind())


def downgrade() -> None:
    op.drop_index("ix_agents_unavailable_reason", table_name="agents")
    op.drop_column("agents", "unavailable_reason")
