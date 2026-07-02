"""Add ``users.ui_settings`` — server-synced per-user UI preferences.

ANILA UI kept folders / stars / tweaks in browser localStorage. On shared
PKI-card workstations that leaks one user's folder names to the next user
and loses preferences across terminals. This JSONB column lets the chat UI
persist them server-side, keyed to the authenticated user.

Revision ID: 0043
Revises: 0042
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0043"
down_revision: Union[str, None] = "0042"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "ui_settings",
            sa.JSON().with_variant(sa.dialects.postgresql.JSONB, "postgresql"),
            nullable=False,
            server_default="{}",
        ),
    )


def downgrade() -> None:
    op.drop_column("users", "ui_settings")
