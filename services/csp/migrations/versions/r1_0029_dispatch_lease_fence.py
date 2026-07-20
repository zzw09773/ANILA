"""Add restart-safe initial-dispatch lease/fencing to session event runs.

Revision ID: r1_0029
Revises: r1_0028
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "r1_0029"
down_revision: Union[str, None] = "r1_0028"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "session_event_runs",
        sa.Column("dispatch_idempotency_key_sha256", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "session_event_runs",
        sa.Column("dispatch_lease_token_sha256", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "session_event_runs",
        sa.Column(
            "dispatch_lease_generation",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "session_event_runs",
        sa.Column("dispatch_lease_expires_at", sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("session_event_runs", "dispatch_lease_expires_at")
    op.drop_column("session_event_runs", "dispatch_lease_generation")
    op.drop_column("session_event_runs", "dispatch_lease_token_sha256")
    op.drop_column("session_event_runs", "dispatch_idempotency_key_sha256")
