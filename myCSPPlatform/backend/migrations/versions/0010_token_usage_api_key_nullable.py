"""Make token_usage.api_key_id nullable.

Wave 1 of the JWT-on-data-plane rollout. Traffic authenticated via a JWT
(the SPA's login session) has no named API key, so the foreign-key column
must accept NULL. The downgrade path refuses to run if any such rows
already exist — shrinking a column's nullability over live data would
either lose rows or fail loudly, and "fail loudly" is the safer default.

Revision ID: 0010
Revises: 0009
Create Date: 2026-04-23
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: Union[str, None] = "0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("token_usage") as batch:
        batch.alter_column(
            "api_key_id",
            existing_type=sa.Integer(),
            nullable=True,
        )


def downgrade() -> None:
    # Refuse to downgrade if JWT-attributed rows exist — forcing them into
    # NOT NULL would require deleting or fabricating data. Operator must
    # clean up first.
    bind = op.get_bind()
    null_count = bind.execute(
        sa.text("SELECT COUNT(*) FROM token_usage WHERE api_key_id IS NULL")
    ).scalar_one()
    if null_count:
        raise RuntimeError(
            f"Refusing to downgrade: {null_count} token_usage rows have "
            "api_key_id IS NULL (SPA/JWT traffic). Delete or backfill them "
            "before running this downgrade."
        )
    with op.batch_alter_table("token_usage") as batch:
        batch.alter_column(
            "api_key_id",
            existing_type=sa.Integer(),
            nullable=False,
        )
