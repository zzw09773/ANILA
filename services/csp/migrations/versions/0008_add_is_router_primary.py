"""Add is_router_primary to model_registry.

Admins pick which LLM is used by ANILA Router (main routing brain) through
the CSP Models page. At most one row may carry is_router_primary=true at a
time — enforced by a partial unique index.

Revision ID: 0008
Revises: 0007
Create Date: 2026-04-22
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "model_registry",
        sa.Column(
            "is_router_primary",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    # Partial unique index: only enforce uniqueness on rows where flag=true,
    # so unlimited rows can sit at false while at most one can be true.
    op.execute(
        "CREATE UNIQUE INDEX uq_model_registry_router_primary "
        "ON model_registry (is_router_primary) "
        "WHERE is_router_primary = true"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_model_registry_router_primary")
    op.drop_column("model_registry", "is_router_primary")
