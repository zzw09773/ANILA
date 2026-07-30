# -*- coding: utf-8 -*-
"""P4.6b — endpoint_author_grants（模型端點位址設定授權）.

Owner ruling (2026-07-30): registering / changing a model endpoint
address is reserved for the platform owner and for developers the
owner designates one by one. Administrators keep every other model
operation (view / activate / deactivate / assign / delete / bulk
import from a registered row) but may no longer introduce an address
of their choosing — that was the remaining confirmation-oracle root
against ``endpoint_group_key``.

This migration only creates the binding table. ``users.role`` and the
authentication service are untouched.

Idempotency
===========

Upgrade uses raw ``CREATE TABLE IF NOT EXISTS`` /
``CREATE INDEX IF NOT EXISTS``, tolerating DBs where ``create_all``
already built the table. Downgrade likewise ``IF EXISTS``.

SQLite note: the partial unique index predicate is emitted on the
Postgres path; the test environment is aligned by SQLAlchemy
``create_all`` / model ``sqlite_where``.

Revision ID: r1_0015
Revises: r1_0014
"""

from typing import Sequence, Union

from alembic import op

revision: str = "r1_0015"
down_revision: Union[str, None] = "r1_0014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS endpoint_author_grants (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL,
            granted_by INTEGER,
            granted_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
            revoked_at TIMESTAMP WITHOUT TIME ZONE,
            CONSTRAINT fk_endpoint_author_grants_user
              FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
            CONSTRAINT fk_endpoint_author_grants_granted_by
              FOREIGN KEY (granted_by) REFERENCES users(id) ON DELETE SET NULL
        )
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS ix_endpoint_author_grants_active_user
          ON endpoint_author_grants (user_id)
          WHERE revoked_at IS NULL
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP INDEX IF EXISTS ix_endpoint_author_grants_active_user"
    )
    op.execute("DROP TABLE IF EXISTS endpoint_author_grants")
