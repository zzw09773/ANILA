"""Add ``conversations.origin`` so multiple frontends can co-exist.

Sprint 7 X follow-up: ANILA UI and ANILA LM both POST to
``/api/conversations`` and read the same row set back. The result is
that LM-grounded retrieval Q&A bleeds into the ANILA UI conversation
list (and vice-versa) — different UX, different mental model, same
backing table.

Adding a free-form ``origin`` text column lets each frontend tag its
conversations and filter on the way back. Today's known origins:

  - ``NULL``       — pre-migration / unspecified (treated as ANILA UI
                     legacy so existing data behaves identically).
  - ``'anila-ui'`` — explicitly tagged from the main chat SPA.
  - ``'anilalm'``  — tagged from the knowledge-base SPA.

Future origins (n8n, slack-bot, cli) just write a different string;
no schema change needed. We intentionally use TEXT not ENUM so adding
a new origin doesn't require an ALTER TYPE.

Revision ID: 0023
Revises: 0022
Create Date: 2026-04-28
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0023"
down_revision: Union[str, None] = "0022"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = {c["name"] for c in inspector.get_columns("conversations")}
    if "origin" not in existing:
        with op.batch_alter_table("conversations") as batch:
            batch.add_column(
                sa.Column(
                    "origin",
                    sa.String(length=32),
                    nullable=True,
                )
            )

    # Composite index on (user_id, origin) so per-user list queries that
    # filter by origin stay covered without scanning per-user tail data.
    # Existing ``ix_conversations_user_id`` continues to serve all-origins
    # lookups; this just adds a faster path for the common UI list.
    existing_idx = {ix["name"] for ix in inspector.get_indexes("conversations")}
    if "ix_conversations_user_origin" not in existing_idx:
        op.create_index(
            "ix_conversations_user_origin",
            "conversations",
            ["user_id", "origin"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_idx = {ix["name"] for ix in inspector.get_indexes("conversations")}
    if "ix_conversations_user_origin" in existing_idx:
        op.drop_index("ix_conversations_user_origin", table_name="conversations")
    existing = {c["name"] for c in inspector.get_columns("conversations")}
    if "origin" in existing:
        with op.batch_alter_table("conversations") as batch:
            batch.drop_column("origin")
