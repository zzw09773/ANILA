"""Add ``token_usage.request_type`` to distinguish chat / embedding / judge.

Sprint 4 / Chunk V: every model invocation that goes through the
ANILA platform — including the ingestion pipeline's embedding calls
and the future Chunking Evaluator's LLM-as-judge calls — should land
in ``token_usage`` for auditing + billing rollup. Existing rows from
the chat path get ``'chat'`` as the default; the column is nullable
to keep back-compat for any external loader that pre-dates this
column.

Revision ID: 0020
Revises: 0019
Create Date: 2026-04-27
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0020"
down_revision: Union[str, None] = "0019"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Backfill ``request_timestamp`` if a previous deploy never got it.
    # Migration 0001 baseline shipped ``token_usage`` with only ``created_at``;
    # ``request_timestamp`` was added later via the SQLAlchemy model + the
    # ``_ensure_schema_backfills`` startup hook. Fresh DBs (e.g. customer
    # delivery clones) reach 0020 without ever having seen that backfill,
    # so the index DDL below trips ``UndefinedColumn``. ``IF NOT EXISTS``
    # is idempotent so live DBs that already have the column skip the ADD.
    op.execute(
        "ALTER TABLE token_usage ADD COLUMN IF NOT EXISTS request_timestamp "
        "TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP"
    )

    op.add_column(
        "token_usage",
        sa.Column(
            "request_type",
            sa.String(length=20),
            nullable=False,
            server_default=sa.text("'chat'"),
        ),
    )
    op.create_index(
        "idx_usage_request_type_time",
        "token_usage",
        ["request_type", "request_timestamp"],
    )


def downgrade() -> None:
    op.drop_index("idx_usage_request_type_time", table_name="token_usage")
    op.drop_column("token_usage", "request_type")
