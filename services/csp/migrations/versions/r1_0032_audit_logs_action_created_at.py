"""Add composite index on audit_logs(action, created_at) for inference listing.

Revision ID: r1_0032
Revises: r1_0031
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op


revision: str = "r1_0032"
down_revision: Union[str, None] = "r1_0031"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index(
        "ix_audit_logs_action_created_at",
        "audit_logs",
        ["action", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_audit_logs_action_created_at", table_name="audit_logs")
