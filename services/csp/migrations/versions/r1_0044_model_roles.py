# -*- coding: utf-8 -*-
"""Add model_roles for vision / summary / knowledge_chat.

既有的主路由、平台嵌入、主簡報仍留在 model_registry 的旗標欄
（is_router_primary、is_platform_embedding、is_slides_primary）。
那些欄的現值不動。這張表只放新角色，而且不預先填模型名稱：
沒有列就是尚未設定，由治理中心指定。

Revision ID: r1_0044
Revises: r1_0043
Create Date: 2026-09-25
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "r1_0044"
down_revision: Union[str, None] = "r1_0043"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "model_roles",
        sa.Column("role", sa.String(length=64), primary_key=True),
        sa.Column("model_id", sa.Integer(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["model_id"],
            ["model_registry.id"],
            name="fk_model_roles_model_id",
            ondelete="SET NULL",
        ),
    )
    op.create_index("ix_model_roles_model_id", "model_roles", ["model_id"])


def downgrade() -> None:
    op.drop_index("ix_model_roles_model_id", table_name="model_roles")
    op.drop_table("model_roles")
