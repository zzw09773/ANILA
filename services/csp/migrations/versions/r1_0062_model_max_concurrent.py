# -*- coding: utf-8 -*-
"""模型同時處理上限。空值表示不限。

登記模型的人設定這個整數。CSP 用它當跨 process 的公平佇列上限。

Revision ID: r1_0062
Revises: r1_0061
Create Date: 2026-09-28
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "r1_0062"
down_revision: Union[str, None] = "r1_0061"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "model_registry",
        sa.Column("max_concurrent", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("model_registry", "max_concurrent")
