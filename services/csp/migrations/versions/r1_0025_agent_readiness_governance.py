"""Persist Agent readiness evidence identities.

Revision ID: r1_0025
Revises: r1_0024

The evidence columns are intentionally nullable.  Existing rows have no
trustworthy manifest/health/trace evidence and must remain non-dispatchable
until an operator re-registers, probes and trace-tests them.  ``is_active`` is
non-null with a true backfill because lifecycle state is independent from the
approval workflow and existing rows were active by default.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "r1_0025"
down_revision: Union[str, None] = "r1_0024"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "agents",
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
    )
    op.add_column("agents", sa.Column("health_checked_at", sa.DateTime(), nullable=True))
    op.add_column("agents", sa.Column("manifest_sha256", sa.String(length=64), nullable=True))
    op.add_column(
        "agents", sa.Column("manifest_revision", sa.String(length=128), nullable=True)
    )
    op.add_column(
        "agents",
        sa.Column("trace_test_governance_fingerprint", sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("agents", "trace_test_governance_fingerprint")
    op.drop_column("agents", "manifest_revision")
    op.drop_column("agents", "manifest_sha256")
    op.drop_column("agents", "health_checked_at")
    op.drop_column("agents", "is_active")
