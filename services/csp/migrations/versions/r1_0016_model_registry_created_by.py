# -*- coding: utf-8 -*-
"""P4.6b follow-up — model_registry.created_by_user_id (authorship).

Designated developers must list and fetch the registry rows they
registered so they can correct an address. Visibility is based on
authorship of the row plus an active endpoint-author grant — not on a
row in ``user_model_permissions`` (inference access). That table is
rewritten wholesale when an administrator adjusts allowed models, which
would silently strip the developer's view of every gateway they
registered.

Idempotency
===========

Upgrade uses ``ADD COLUMN IF NOT EXISTS`` and a ``pg_constraint``-guarded
named FK, matching r1_0011 / r1_0012. Downgrade drops the constraint and
column with ``IF EXISTS``.

Revision ID: r1_0016
Revises: r1_0015
"""

from typing import Sequence, Union

from alembic import op

revision: str = "r1_0016"
down_revision: Union[str, None] = "r1_0015"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE model_registry
          ADD COLUMN IF NOT EXISTS created_by_user_id INTEGER
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
             WHERE conname = 'fk_model_registry_created_by_user'
               AND conrelid = 'model_registry'::regclass
          ) THEN
            ALTER TABLE model_registry
              ADD CONSTRAINT fk_model_registry_created_by_user
              FOREIGN KEY (created_by_user_id) REFERENCES users(id)
              ON DELETE SET NULL;
          END IF;
        END $$;
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_model_registry_created_by_user
          ON model_registry (created_by_user_id)
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP INDEX IF EXISTS ix_model_registry_created_by_user"
    )
    op.execute(
        """
        ALTER TABLE model_registry
          DROP CONSTRAINT IF EXISTS fk_model_registry_created_by_user
        """
    )
    op.execute(
        """
        ALTER TABLE model_registry
          DROP COLUMN IF EXISTS created_by_user_id
        """
    )
