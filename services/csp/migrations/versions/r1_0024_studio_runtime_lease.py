"""Bind Studio runtime sinks to a fenced durable attempt.

Revision ID: r1_0024
Revises: r1_0023
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "r1_0024"
down_revision: Union[str, None] = "r1_0023"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("artifact_jobs", sa.Column("durable_attempt", sa.Integer(), nullable=True))
    op.add_column(
        "artifact_jobs",
        sa.Column("durable_lease_digest", sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("artifact_jobs", "durable_lease_digest")
    op.drop_column("artifact_jobs", "durable_attempt")
