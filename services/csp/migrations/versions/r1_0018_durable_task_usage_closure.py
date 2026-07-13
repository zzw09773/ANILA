# -*- coding: utf-8 -*-
"""Allow Agent usage without an informational base model.

Revision ID: r1_0018
Revises: r1_0017
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "r1_0018"
down_revision: Union[str, None] = "r1_0017"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        "token_usage", "model_id", existing_type=sa.Integer(), nullable=True
    )


def downgrade() -> None:
    # Fails closed if operators have not backfilled nullable Agent rows.
    op.alter_column(
        "token_usage", "model_id", existing_type=sa.Integer(), nullable=False
    )
