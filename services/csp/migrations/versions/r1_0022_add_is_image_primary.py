# -*- coding: utf-8 -*-
"""Add is_image_primary to model_registry.

Slice 8b (doc 2026-07-06-flux-image-primary-design.md §1) — admins pick
which FLUX / OpenAI Images model flux2-dev-agent / anila-studio should
auto-consume through the CSP Models page. At most one row may carry
is_image_primary=true at a time — enforced by a partial unique index.
Mirrors is_router_primary / is_platform_embedding.

Revision ID: r1_0022
Revises: r1_0021
Create Date: 2026-07-30
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "r1_0022"
down_revision: Union[str, None] = "r1_0021"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "model_registry",
        sa.Column(
            "is_image_primary",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    # Partial unique index: only enforce uniqueness on rows where flag=true,
    # so unlimited rows can sit at false while at most one can be true.
    op.execute(
        "CREATE UNIQUE INDEX uq_model_registry_image_primary "
        "ON model_registry (is_image_primary) "
        "WHERE is_image_primary = true"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_model_registry_image_primary")
    op.drop_column("model_registry", "is_image_primary")
