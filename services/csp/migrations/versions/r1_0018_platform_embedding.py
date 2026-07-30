# -*- coding: utf-8 -*-
"""P4.8 — platform embedding designation + vector source provenance.

Invariant 1
===========
``model_registry.is_platform_embedding`` mirrors ``is_router_primary``:
a boolean with a partial unique index so at most one row is designated.
``embedding_native_dim`` stores the dimension measured by calling the
model at designation time (never a configured guess).

Invariant 2
===========
``conversation_memory_chunks``, ``document_chunks``, and
``ingestion_images`` each gain ``embedding_source_model`` +
``embedding_native_dim``. Retrieval filters to the current designated
model; other rows stay stored for pending recompute.

Existing document/image rows are backfilled from their collection's
``embedding_model`` so a freshly designated matching model does not
silently empty RAG. Memory chunks stay NULL (feature was broken by the
case-mismatch defect; they re-embed on the next turn).

Revision ID: r1_0018
Revises: r1_0017
Create Date: 2026-07-30
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "r1_0018"
down_revision: Union[str, None] = "r1_0017"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_VECTOR_TABLES = (
    "conversation_memory_chunks",
    "document_chunks",
    "ingestion_images",
)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "model_registry" in tables:
        cols = {c["name"] for c in inspector.get_columns("model_registry")}
        if "is_platform_embedding" not in cols:
            op.add_column(
                "model_registry",
                sa.Column(
                    "is_platform_embedding",
                    sa.Boolean(),
                    nullable=False,
                    server_default=sa.text("false"),
                ),
            )
        if "embedding_native_dim" not in cols:
            op.add_column(
                "model_registry",
                sa.Column("embedding_native_dim", sa.Integer(), nullable=True),
            )
        # Partial unique: at most one designated platform embedding.
        op.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS "
            "uq_model_registry_platform_embedding "
            "ON model_registry (is_platform_embedding) "
            "WHERE is_platform_embedding = true"
        )

    for table in _VECTOR_TABLES:
        if table not in tables:
            continue
        cols = {c["name"] for c in inspector.get_columns(table)}
        if "embedding_source_model" not in cols:
            op.add_column(
                table,
                sa.Column(
                    "embedding_source_model",
                    sa.String(length=200),
                    nullable=True,
                ),
            )
        if "embedding_native_dim" not in cols:
            op.add_column(
                table,
                sa.Column("embedding_native_dim", sa.Integer(), nullable=True),
            )

    # Backfill document/image provenance from the owning collection so
    # designating the same model does not empty retrieval.
    if "document_chunks" in tables and "ingestion_collections" in tables:
        op.execute(
            """
            UPDATE document_chunks AS dc
               SET embedding_source_model = ic.embedding_model,
                   embedding_native_dim = ic.embedding_dim
              FROM ingestion_collections AS ic
             WHERE dc.collection_id = ic.id
               AND dc.embedding_source_model IS NULL
            """
        )
    if "ingestion_images" in tables and "ingestion_collections" in tables:
        op.execute(
            """
            UPDATE ingestion_images AS ii
               SET embedding_source_model = ic.embedding_model,
                   embedding_native_dim = ic.embedding_dim
              FROM ingestion_collections AS ic
             WHERE ii.collection_id = ic.id
               AND ii.embedding_source_model IS NULL
            """
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    for table in _VECTOR_TABLES:
        if table not in tables:
            continue
        cols = {c["name"] for c in inspector.get_columns(table)}
        if "embedding_native_dim" in cols:
            op.drop_column(table, "embedding_native_dim")
        if "embedding_source_model" in cols:
            op.drop_column(table, "embedding_source_model")

    if "model_registry" in tables:
        op.execute(
            "DROP INDEX IF EXISTS uq_model_registry_platform_embedding"
        )
        cols = {c["name"] for c in inspector.get_columns("model_registry")}
        if "embedding_native_dim" in cols:
            op.drop_column("model_registry", "embedding_native_dim")
        if "is_platform_embedding" in cols:
            op.drop_column("model_registry", "is_platform_embedding")
