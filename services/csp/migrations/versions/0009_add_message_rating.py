"""Add rating to messages.

Capture user feedback (thumbs up / down) on individual assistant messages.
Nullable VARCHAR(8) so existing rows keep NULL; only 'up' / 'down' are
expected values — constrained at the API layer rather than the DB so future
labels (e.g. 'flag') do not require a migration.

Revision ID: 0009
Revises: 0008
Create Date: 2026-04-22
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: Union[str, None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "messages",
        sa.Column("rating", sa.String(length=8), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("messages", "rating")
