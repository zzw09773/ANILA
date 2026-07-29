# -*- coding: utf-8 -*-
"""OW-1 — message history tree (parent_id + active_leaf_message_id).

SYSTEM-MAP:46 / docs/plans/ow1-message-tree-blueprint.md: editing re-asks
as a sibling branch; regenerate creates an assistant sibling; both variants
are queryable, switchable, and independently deletable.

Schema
======

* ``messages.parent_id`` — nullable self-FK, ``ON DELETE CASCADE``
* ``ck_messages_parent_not_self`` — reject parent_id = id
* ``ix_messages_conversation_parent`` — ``(conversation_id, parent_id)``
* ``conversations.active_leaf_message_id`` — nullable FK → messages(id)
  ``ON DELETE SET NULL``
* ``ix_conversations_active_leaf`` — partial index (PG)

Backfill (PG only): linearise existing rows via
``LAG(id) OVER (PARTITION BY conversation_id ORDER BY created_at, id)``
and set active leaf to the newest message per conversation. Needed for the
``view=active`` invariant on any pre-existing DB, not for data preservation.
No NOT NULL / no defaults — roots and new conversations legitimately NULL.

Idempotency
===========

Steps 1–7 are fully idempotent: raw ``IF NOT EXISTS`` / ``pg_constraint``
guards (same idiom as ``r1_0009_department_tree``) so a DB that already
gained columns via ``create_all`` does not wedge on ``DuplicateColumn``.
Downgrade drops are similarly tolerant (``IF EXISTS``).

Steps 8–9 are first-run only. Step 8 gates the LAG parent backfill on an
untouched tree (``NOT EXISTS`` any non-NULL ``parent_id``); re-running it
on a DB with real branches would chain sibling roots into a bogus linear
chain. Step 9 only fills NULL ``active_leaf_message_id`` pointers.

SQLite note: partial index + window backfill are PG-path only; tests get
columns via SQLAlchemy ``create_all`` / model definitions.

Revision ID: r1_0012
Revises: r1_0011
"""

from typing import Sequence, Union

from alembic import op

revision: str = "r1_0012"
down_revision: Union[str, None] = "r1_0011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1) messages.parent_id column (no inline FK): IF NOT EXISTS survives create_all.
    op.execute(
        """
        ALTER TABLE messages
          ADD COLUMN IF NOT EXISTS parent_id INTEGER
        """
    )
    # 2) Named FK — guard via pg_constraint.
    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
             WHERE conname = 'fk_messages_parent_id'
               AND conrelid = 'messages'::regclass
          ) THEN
            ALTER TABLE messages
              ADD CONSTRAINT fk_messages_parent_id
              FOREIGN KEY (parent_id) REFERENCES messages(id)
              ON DELETE CASCADE;
          END IF;
        END $$;
        """
    )
    # 3) CHECK parent_id <> id.
    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
             WHERE conname = 'ck_messages_parent_not_self'
               AND conrelid = 'messages'::regclass
          ) THEN
            ALTER TABLE messages
              ADD CONSTRAINT ck_messages_parent_not_self
              CHECK (parent_id IS NULL OR parent_id <> id);
          END IF;
        END $$;
        """
    )
    # 4) Tree lookup index.
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_messages_conversation_parent
          ON messages (conversation_id, parent_id)
        """
    )
    # 5) conversations.active_leaf_message_id column.
    op.execute(
        """
        ALTER TABLE conversations
          ADD COLUMN IF NOT EXISTS active_leaf_message_id INTEGER
        """
    )
    # 6) FK SET NULL (circular with messages.conversation_id — named + guarded).
    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
             WHERE conname = 'fk_conversations_active_leaf_message_id'
               AND conrelid = 'conversations'::regclass
          ) THEN
            ALTER TABLE conversations
              ADD CONSTRAINT fk_conversations_active_leaf_message_id
              FOREIGN KEY (active_leaf_message_id) REFERENCES messages(id)
              ON DELETE SET NULL;
          END IF;
        END $$;
        """
    )
    # 7) Partial index — PG does not auto-index FKs.
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_conversations_active_leaf
          ON conversations (active_leaf_message_id)
          WHERE active_leaf_message_id IS NOT NULL
        """
    )
    # 8) Backfill parent_id: linear chain by (created_at, id).
    # First-run only — skip when any parent_id is already set (real branches).
    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM messages WHERE parent_id IS NOT NULL
          ) THEN
            WITH ordered AS (
              SELECT id,
                     LAG(id) OVER (
                       PARTITION BY conversation_id
                       ORDER BY created_at, id
                     ) AS prev_id
                FROM messages
            )
            UPDATE messages AS m
               SET parent_id = ordered.prev_id
              FROM ordered
             WHERE m.id = ordered.id
               AND m.parent_id IS NULL
               AND ordered.prev_id IS NOT NULL;
          END IF;
        END $$;
        """
    )
    # 9) Backfill active leaf = newest message per conversation.
    op.execute(
        """
        UPDATE conversations AS c
           SET active_leaf_message_id = sub.leaf_id
          FROM (
            SELECT conversation_id,
                   (ARRAY_AGG(id ORDER BY created_at DESC, id DESC))[1] AS leaf_id
              FROM messages
             GROUP BY conversation_id
          ) AS sub
         WHERE c.id = sub.conversation_id
           AND c.active_leaf_message_id IS NULL
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_conversations_active_leaf")
    op.execute(
        "ALTER TABLE conversations "
        "DROP CONSTRAINT IF EXISTS fk_conversations_active_leaf_message_id"
    )
    op.execute(
        "ALTER TABLE conversations DROP COLUMN IF EXISTS active_leaf_message_id"
    )
    op.execute("DROP INDEX IF EXISTS ix_messages_conversation_parent")
    op.execute(
        "ALTER TABLE messages DROP CONSTRAINT IF EXISTS ck_messages_parent_not_self"
    )
    op.execute(
        "ALTER TABLE messages DROP CONSTRAINT IF EXISTS fk_messages_parent_id"
    )
    op.execute("ALTER TABLE messages DROP COLUMN IF EXISTS parent_id")
