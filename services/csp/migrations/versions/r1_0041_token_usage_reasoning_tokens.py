# -*- coding: utf-8 -*-
"""Record upstream/estimated reasoning tokens on token_usage.

NULL means the upstream did not report reasoning tokens and none could
be estimated from reasoning / reasoning_content text.

Revision ID: r1_0041
Revises: r1_0040
Create Date: 2026-09-15
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "r1_0041"
down_revision: Union[str, None] = "r1_0040"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "token_usage",
        sa.Column("reasoning_tokens", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("token_usage", "reasoning_tokens")
