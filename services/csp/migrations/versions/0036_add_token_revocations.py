"""Add the ``token_revocations`` table.

The new table records every JWT-revocation event (logout, password
change, admin-revoke) so downstream services that verify CSP-signed
JWTs can cold-start a deny-list via
``GET /api/auth/revocations?since=...`` before subscribing to the
live ``anila:auth:token-revoke`` Redis channel.

Schema
======

  ``id``                  BIGINT  PK, autoincrement
  ``user_id``             INTEGER FK users.id ON DELETE CASCADE, indexed
  ``revoked_at_version``  INTEGER post-bump value of ``users.token_version``
  ``revoked_at``          TIMESTAMP WITH TIME ZONE, default now(), indexed

Indexes
=======

* ``ix_token_revocations_user_id`` — single-column on user_id; used
  when answering "all revocations for one user".
* ``ix_token_revocations_revoked_at`` — single-column on the timestamp;
  used by the cold-start sync endpoint's ``since`` filter.
* ``ix_token_revocations_user_id_revoked_at`` — composite, optimised
  for the latest-revocation-per-user lookup once we add per-user
  filtering to the endpoint.

Retention is handled at the application layer (the endpoint clamps
its window to 30 days). A background cleanup job is intentionally
deferred — see the TODO in ``app/api/auth.py``.

Revision ID: 0036
Revises: 0035
Create Date: 2026-05-23
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0036"
down_revision: Union[str, None] = "0035"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())

    if "token_revocations" in existing_tables:
        # Idempotent: re-running on an already-upgraded DB is a no-op.
        return

    op.create_table(
        "token_revocations",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("revoked_at_version", sa.Integer(), nullable=False),
        sa.Column(
            "revoked_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )

    op.create_index(
        "ix_token_revocations_user_id",
        "token_revocations",
        ["user_id"],
    )
    op.create_index(
        "ix_token_revocations_revoked_at",
        "token_revocations",
        ["revoked_at"],
    )
    op.create_index(
        "ix_token_revocations_user_id_revoked_at",
        "token_revocations",
        ["user_id", "revoked_at"],
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_token_revocations_user_id_revoked_at;")
    op.execute("DROP INDEX IF EXISTS ix_token_revocations_revoked_at;")
    op.execute("DROP INDEX IF EXISTS ix_token_revocations_user_id;")
    op.drop_table("token_revocations")
