# -*- coding: utf-8 -*-
"""User-selectable thinking tiers and per-model probed levels.

conversations.thinking_tier stores the conversation's picker value
(default|off|standard|deep; NULL means default).
model_registry.thinking_levels_supported records what the endpoint
accepted at register/probe time (NULL = not probed).
model_registry.thinking_user_selectable lets an admin lock the picker.

Revision ID: r1_0040
Revises: r1_0039
Create Date: 2026-09-15
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "r1_0040"
down_revision: Union[str, None] = "r1_0039"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_JSON = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.add_column(
        "conversations",
        sa.Column("thinking_tier", sa.String(length=16), nullable=True),
    )
    op.add_column(
        "model_registry",
        sa.Column("thinking_levels_supported", _JSON, nullable=True),
    )
    op.add_column(
        "model_registry",
        sa.Column(
            "thinking_user_selectable",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
    )


def downgrade() -> None:
    op.drop_column("model_registry", "thinking_user_selectable")
    op.drop_column("model_registry", "thinking_levels_supported")
    op.drop_column("conversations", "thinking_tier")
