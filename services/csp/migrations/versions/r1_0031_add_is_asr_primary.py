# -*- coding: utf-8 -*-
"""Add is_asr_primary to model_registry.

Admins pick which speech-recognition decoder asr-gateway should consume
through the CSP Models page. At most one row may carry is_asr_primary=true
at a time — enforced by a partial unique index. Mirrors is_router_primary /
is_image_primary / is_platform_embedding.

⚠ ``down_revision = r1_0030``: r1_0029 / r1_0030 are owned by other packages
and may land in a different worktree first. At merge time, confirm the chain
r1_0028 → r1_0029 → r1_0030 → r1_0031 before upgrading production.

Revision ID: r1_0031
Revises: r1_0030
Create Date: 2026-07-31
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "r1_0031"
down_revision: Union[str, None] = "r1_0030"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "model_registry",
        sa.Column(
            "is_asr_primary",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    # Partial unique index: only enforce uniqueness on rows where flag=true,
    # so unlimited rows can sit at false while at most one can be true.
    op.execute(
        "CREATE UNIQUE INDEX uq_model_registry_asr_primary "
        "ON model_registry (is_asr_primary) "
        "WHERE is_asr_primary = true"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_model_registry_asr_primary")
    op.drop_column("model_registry", "is_asr_primary")
