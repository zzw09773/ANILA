"""Drop requires_encryption from model_registry.

Encryption is an agent-level policy only. Base LLMs (model_registry rows)
do not carry this flag — the same LLM may back both classified and
non-classified agents. The column on ``agents`` is kept.

Revision ID: 0007
Revises: 0006
Create Date: 2026-04-21
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_column("model_registry", "requires_encryption")


def downgrade() -> None:
    op.add_column(
        "model_registry",
        sa.Column(
            "requires_encryption",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
