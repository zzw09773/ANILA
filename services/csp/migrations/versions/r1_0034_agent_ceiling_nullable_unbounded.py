# -*- coding: utf-8 -*-
"""Allow agents.classification_ceiling NULL again (= UI 「未設定」).

Gate 2 (r1_0011) made agent/model/service ceilings NOT NULL with a least-
privilege default. The developer Agents UI still exposes unset as JSON null
so description-only saves remain possible. Restore NULL for *agents only*
— model_registry and registered_services stay NOT NULL. NULL means
「未設定（不可派工）」, never an unbounded privilege; runtime admission
continues to fail-closed when a ceiling is missing at invoke time.

Revision ID: r1_0034
Revises: r1_0033
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "r1_0034"
down_revision: Union[str, None] = "r1_0033"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        "agents",
        "classification_ceiling",
        existing_type=sa.String(length=20),
        nullable=True,
        existing_server_default=sa.text("'無機密'"),
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE agents SET classification_ceiling = '無機密' "
            "WHERE classification_ceiling IS NULL"
        )
    )
    op.alter_column(
        "agents",
        "classification_ceiling",
        existing_type=sa.String(length=20),
        nullable=False,
        existing_server_default=sa.text("'無機密'"),
    )
