# -*- coding: utf-8 -*-
"""Add is_slides_primary to model_registry — the「主簡報模型」knob.

Admins pick which LLM anila-studio uses to write slide decks (and to run
vision QA on them) through the CSP Models page. At most one row may carry
is_slides_primary=true at a time — enforced by a partial unique index.
Mirrors is_router_primary / is_image_primary / is_asr_primary.

Revision ID: r1_0037
Revises: r1_0036
Create Date: 2026-09-02
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "r1_0037"
down_revision: Union[str, None] = "r1_0036"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "model_registry",
        sa.Column(
            "is_slides_primary",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_model_registry_slides_primary "
        "ON model_registry (is_slides_primary) "
        "WHERE is_slides_primary = true"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_model_registry_slides_primary")
    op.drop_column("model_registry", "is_slides_primary")
