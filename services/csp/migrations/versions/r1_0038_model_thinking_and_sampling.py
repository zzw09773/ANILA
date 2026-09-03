# -*- coding: utf-8 -*-
"""Add per-model thinking effort and sampling overrides.

Admins set these on the CSP Models page. NULL means "use the upstream /
platform default" — the CSP proxy only injects a key when the column is
set and the caller did not already send that key.

thinking_effort: NULL/default = leave vendor knobs alone; off | low |
medium | high | xhigh | max map to chat_template_kwargs.enable_thinking
(Qwen/vLLM) and, for o-style models, reasoning_effort.

Revision ID: r1_0038
Revises: r1_0037
Create Date: 2026-09-03
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "r1_0038"
down_revision: Union[str, None] = "r1_0037"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "model_registry",
        sa.Column("thinking_effort", sa.String(length=20), nullable=True),
    )
    op.add_column(
        "model_registry",
        sa.Column("temperature", sa.Float(), nullable=True),
    )
    op.add_column(
        "model_registry",
        sa.Column("top_p", sa.Float(), nullable=True),
    )
    op.add_column(
        "model_registry",
        sa.Column("presence_penalty", sa.Float(), nullable=True),
    )
    op.add_column(
        "model_registry",
        sa.Column("max_tokens", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("model_registry", "max_tokens")
    op.drop_column("model_registry", "presence_penalty")
    op.drop_column("model_registry", "top_p")
    op.drop_column("model_registry", "temperature")
    op.drop_column("model_registry", "thinking_effort")
