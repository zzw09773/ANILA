# -*- coding: utf-8 -*-
"""OW-3 scope reduction — drop exec kind + result_mode (declarative only).

Owner withdrew in-process Python execution (2026-07-30): custom action
buttons only open a picker and load a preset prompt. With a single
remaining kind and a single result path (client-dispatched prompt), the
``kind`` and ``result_mode`` discriminators are meaningless single-valued
structure; this migration removes both columns.

Obsolete ``kind='exec'`` rows are deleted first. That is safe on this
restart tree: the platform DB is disposable, no production exec actions
are in service, and the withdrawn capability has no declarative
equivalent worth preserving (body was Python source, not a prompt
template). Content remains in git history / prior audit snapshots.

Idempotency
===========

Upgrade deletes obsolete ``kind='exec'`` rows only when the ``kind``
column exists (``information_schema`` guard — tables created from models
rather than the migration chain lack the column). Constraint / column
drops use ``IF EXISTS``. Downgrade re-adds columns with declarative /
to_model defaults for any surviving rows.

Revision ID: r1_0014
Revises: r1_0013
"""

from typing import Sequence, Union

from alembic import op

revision: str = "r1_0014"
down_revision: Union[str, None] = "r1_0013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Safe: restart-tree DB is disposable; exec bodies are Python source
    # with no prompt-template conversion path after the capability withdrawal.
    # Guard on column existence: DBs created from models (no kind column)
    # must not fail here and wedge the chain at r1_0013.
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = 'message_actions'
              AND column_name = 'kind'
          ) THEN
            DELETE FROM message_actions WHERE kind = 'exec';
          END IF;
        END $$
        """
    )
    op.execute(
        "ALTER TABLE message_actions "
        "DROP CONSTRAINT IF EXISTS ck_message_actions_kind"
    )
    op.execute(
        "ALTER TABLE message_actions "
        "DROP CONSTRAINT IF EXISTS ck_message_actions_result_mode"
    )
    op.execute(
        "ALTER TABLE message_actions DROP COLUMN IF EXISTS kind"
    )
    op.execute(
        "ALTER TABLE message_actions DROP COLUMN IF EXISTS result_mode"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE message_actions "
        "ADD COLUMN IF NOT EXISTS kind VARCHAR(20) NOT NULL "
        "DEFAULT 'declarative'"
    )
    op.execute(
        "ALTER TABLE message_actions "
        "ADD COLUMN IF NOT EXISTS result_mode VARCHAR(20) NOT NULL "
        "DEFAULT 'to_model'"
    )
    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conname = 'ck_message_actions_kind'
          ) THEN
            ALTER TABLE message_actions
              ADD CONSTRAINT ck_message_actions_kind
              CHECK (kind IN ('declarative', 'exec'));
          END IF;
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conname = 'ck_message_actions_result_mode'
          ) THEN
            ALTER TABLE message_actions
              ADD CONSTRAINT ck_message_actions_result_mode
              CHECK (result_mode IN ('to_model', 'direct'));
          END IF;
        END $$
        """
    )
