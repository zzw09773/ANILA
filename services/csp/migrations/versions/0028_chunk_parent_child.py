"""Sprint 9 X / parent-child RAG — schema for hierarchical chunk tree.

Adds three columns to ``document_chunks`` so the HierarchicalChunker
can persist its parent-child tree (heading nodes + paragraph leaves)
instead of flattening everything into a list of leaves with a
``heading_path`` string in metadata.

What this enables
=================

The previous schema treated every chunk as a leaf. A ~1024-token
section was one row, embedded as one vector. Queries hitting a
sub-topic inside a section had recall problems because the single
section vector couldn't represent multiple semantic facets at once.

After this migration the chunker can emit:

  * ``chunk_type='document'`` — root row per document (no embedding)
  * ``chunk_type='heading'``  — one per heading; no embedding; carries
                                 the heading title as content for
                                 retrieval-time JOIN expansion
  * ``chunk_type='leaf'``     — paragraph-level chunk; HAS embedding;
                                 ``parent_chunk_id`` points at the
                                 nearest enclosing heading row

Retrieval path: vector search restricted to ``chunk_type='leaf'``
returns top-k leaves, then a single ``id = ANY(...)`` lookup pulls
the matching parent rows so the API response can include both
``content`` (precise leaf) and ``parent_content`` (section-level
context). LLM context-assembly logic is deferred to a follow-up
AgenticRAG sprint per design doc decision #3.

Schema decisions
================

  * ``parent_chunk_id`` is a self-FK to ``document_chunks(id)`` with
    ``ON DELETE SET NULL`` rather than CASCADE: deleting a single
    chunk shouldn't cascade-delete its leaves. Document-level deletes
    still cascade via ``document_chunks.document_id``.
  * ``chunk_type`` is VARCHAR + CHECK rather than ENUM. ENUM extension
    requires migration churn; CHECK lets us add new types in a
    forward-compatible way.
  * ``chunk_level`` is INTEGER. 0 = document root, 1+ = depth from
    root. Lets retrieval / UI render the tree depth without parsing
    metadata.heading_path each time.
  * Partial index on ``parent_chunk_id`` WHERE NOT NULL keeps the
    index tight — most queries that touch this column want to find
    children of a given parent, and the NULL parents (root rows or
    legacy leaves) don't need to be in that index.
  * Defaults: ``chunk_type='leaf'``, ``chunk_level=0``,
    ``parent_chunk_id=NULL``. Any existing rows (per design doc
    decision #1, the production DB has none today) would behave
    exactly like classic leaves under the new retrieval path —
    ``parent_content`` returns ``None``, ``content`` returns as-is.

Revision ID: 0028
Revises: 0027
Create Date: 2026-05-02
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0028"
down_revision: Union[str, None] = "0027"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# ---------------------------------------------------------------------------
# UPGRADE
# ---------------------------------------------------------------------------


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if "document_chunks" not in inspector.get_table_names():
        # Defensive — migrations 0014 / 0015 should have created the
        # table years ago. If we're somehow running on an empty schema
        # this migration is a no-op for chunk-related work; the chunk
        # table itself comes from the earlier migration.
        return

    existing_cols = {col["name"] for col in inspector.get_columns("document_chunks")}

    # Use raw DDL because alembic's batch mode breaks self-referencing
    # FKs (SQLite emulation creates a temp table that conflicts with
    # the FK target name). Postgres handles the columns just fine.
    if "parent_chunk_id" not in existing_cols:
        op.execute(
            """
            ALTER TABLE document_chunks
              ADD COLUMN parent_chunk_id BIGINT
                REFERENCES document_chunks(id) ON DELETE SET NULL
            """
        )

    if "chunk_type" not in existing_cols:
        op.execute(
            """
            ALTER TABLE document_chunks
              ADD COLUMN chunk_type VARCHAR(20) NOT NULL DEFAULT 'leaf'
            """
        )

    if "chunk_level" not in existing_cols:
        op.execute(
            """
            ALTER TABLE document_chunks
              ADD COLUMN chunk_level INTEGER NOT NULL DEFAULT 0
            """
        )

    # Partial index — only rows that have a parent show up here. Most
    # queries that need this column want to find children of a given
    # parent (cardinality ~10–200 per parent, well-suited to a B-tree).
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_chunks_parent
          ON document_chunks (parent_chunk_id)
          WHERE parent_chunk_id IS NOT NULL
        """
    )

    # Type taxonomy enforcement. CHECK rather than ENUM so we can add
    # 'image' / 'table' later without migration churn.
    existing_constraints = {
        c["name"] for c in inspector.get_check_constraints("document_chunks")
    }
    if "chunks_type_valid" not in existing_constraints:
        op.execute(
            """
            ALTER TABLE document_chunks
              ADD CONSTRAINT chunks_type_valid
              CHECK (chunk_type IN ('document', 'heading', 'leaf'))
            """
        )


# ---------------------------------------------------------------------------
# DOWNGRADE
# ---------------------------------------------------------------------------


def downgrade() -> None:
    op.execute("ALTER TABLE document_chunks DROP CONSTRAINT IF EXISTS chunks_type_valid")
    op.execute("DROP INDEX IF EXISTS idx_chunks_parent")
    # Drop the columns. Order matters: parent_chunk_id last because
    # the partial index above references it (already dropped, but
    # belt-and-braces ordering helps if downgrade is interrupted).
    op.execute("ALTER TABLE document_chunks DROP COLUMN IF EXISTS chunk_level")
    op.execute("ALTER TABLE document_chunks DROP COLUMN IF EXISTS chunk_type")
    op.execute("ALTER TABLE document_chunks DROP COLUMN IF EXISTS parent_chunk_id")
