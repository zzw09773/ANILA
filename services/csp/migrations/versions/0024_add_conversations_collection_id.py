"""Add ``conversations.collection_id`` for ANILALM knowledge-base scoping.

## The bug this fixes

ANILA LM (anilalm origin) lets the user open multiple knowledge bases
(``ingestion_collections``). The sidebar lists conversations for the
current knowledge base. But conversations had no ``collection_id``,
so the sidebar query could only filter by ``user_id`` + ``origin`` —
that returns ALL the user's anilalm conversations regardless of which
collection they were created under. Symptom: open Collection B, see
Collection A's conversations.

## The fix

Add ``collection_id INTEGER NULL`` referencing
``ingestion_collections(id)`` with ``ON DELETE SET NULL``. The
NULL-tolerant FK is deliberate:

  * Existing rows (origin=NULL legacy + origin=anila-ui) are NOT
    knowledge-base-scoped — the main chat SPA has no collection
    concept. They stay NULL forever.
  * Future ``origin='anilalm'`` rows MUST set collection_id; the API
    layer enforces this at create time. The DB column stays nullable
    so we don't need a backfill (there are no anilalm rows in the
    snapshot we ran this against).
  * If the user deletes a collection, conversations that were scoped to
    it have ``collection_id`` set NULL (rather than CASCADE-deleting
    the conversations themselves) — losing the knowledge-base context
    is bad, but losing the user's chat history is worse.

A composite index on ``(user_id, collection_id, origin)`` covers the
hot ANILALM sidebar query
``WHERE user_id=? AND collection_id=? AND origin='anilalm'`` without
scanning tail data.

## Why not change ``origin`` itself

Origin is multi-frontend tagging (anilalm, anila-ui, n8n, slack-bot,
…). Collection scoping is a separate concern that only applies to
some origins. Combining them would require encoding collection_id in
a string, which breaks FK semantics. Keep them separate.

Revision ID: 0024
Revises: 0023
Create Date: 2026-04-29
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0024"
down_revision: Union[str, None] = "0023"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = {c["name"] for c in inspector.get_columns("conversations")}
    if "collection_id" not in existing:
        with op.batch_alter_table("conversations") as batch:
            batch.add_column(
                sa.Column(
                    "collection_id",
                    sa.Integer(),
                    nullable=True,
                )
            )
            # FK with ON DELETE SET NULL — see top-of-file rationale.
            batch.create_foreign_key(
                "conversations_collection_id_fkey",
                "ingestion_collections",
                ["collection_id"],
                ["id"],
                ondelete="SET NULL",
            )

    existing_idx = {ix["name"] for ix in inspector.get_indexes("conversations")}
    if "ix_conversations_user_collection_origin" not in existing_idx:
        op.create_index(
            "ix_conversations_user_collection_origin",
            "conversations",
            ["user_id", "collection_id", "origin"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    existing_idx = {ix["name"] for ix in inspector.get_indexes("conversations")}
    if "ix_conversations_user_collection_origin" in existing_idx:
        op.drop_index(
            "ix_conversations_user_collection_origin", table_name="conversations",
        )

    existing = {c["name"] for c in inspector.get_columns("conversations")}
    if "collection_id" in existing:
        with op.batch_alter_table("conversations") as batch:
            # Drop the FK first so the column drop doesn't fight the constraint.
            try:
                batch.drop_constraint(
                    "conversations_collection_id_fkey", type_="foreignkey",
                )
            except Exception:
                # Some Postgres versions / sqlalchemy versions auto-name the FK
                # differently if the explicit name failed at create. Best-effort.
                pass
            batch.drop_column("collection_id")
