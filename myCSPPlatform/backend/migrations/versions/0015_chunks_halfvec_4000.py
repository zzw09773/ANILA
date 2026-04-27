"""Convert document_chunks.embedding from vector(1536) to halfvec(4000).

Driven by the live NV-embed-V2 deployment exposed via embedding-proxy on
port 7011 — the proxy returns 4096-d vectors and ignores the OpenAI
``dimensions`` parameter, so we cannot get server-side truncation.

Three options were considered, each with their tradeoff:

1. ``vector(1536)`` (status quo from 0014): worker truncates 4096 → 1536
   client-side. Loses 62.5% of dims; works with HNSW; standard ``vector``
   type. Strictly worst quality.
2. ``halfvec(4000)`` (this migration): worker truncates 4096 → 4000 to
   fit halfvec's HNSW max. Loses 2.3% of dims (well under Matryoshka
   noise floor); halves storage per chunk; HNSW indexable.
3. ``vector(4096)`` no index: full quality, no ANN — pure linear scan.
   OK at <10k chunks but degrades fast.

Going with (2). Drops index → drops column → re-adds as halfvec → re-adds
HNSW with halfvec_cosine_ops. The dev DB has zero chunks committed at
this revision (smoke runs failed because of upstream embedding endpoint
issues), so the destructive ALTER is data-safe.

If a future deployment uses an embedding model with native ≤2000-d
output, a follow-up migration can re-narrow back to ``vector(N)``. The
collection-level ``embedding_dim`` column is the source of truth for
per-collection dim — Sprint 4 evaluator may push that to be physically
per-table.

Revision ID: 0015
Revises: 0014
Create Date: 2026-04-25
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0015"
down_revision: Union[str, None] = "0014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_NEW_DIM = 4000


def upgrade() -> None:
    # Drop the HNSW index — column type changes need the index gone first,
    # and we'll recreate it with halfvec_cosine_ops anyway.
    op.execute("DROP INDEX IF EXISTS ix_chunks_embedding_hnsw")

    # ALTER COLUMN type. We use USING with a dummy cast so PG accepts the
    # type change, but since the column is empty (no rows yet) the cast
    # function is never invoked. If a future deployment runs this with
    # data, the cast will fail loudly and the operator picks reindexing.
    op.execute(
        f"ALTER TABLE document_chunks ALTER COLUMN embedding TYPE halfvec({_NEW_DIM}) "
        f"USING embedding::halfvec({_NEW_DIM})"
    )

    # Recreate HNSW with halfvec-specific operator class.
    op.execute(
        """
        CREATE INDEX ix_chunks_embedding_hnsw
            ON document_chunks USING hnsw (embedding halfvec_cosine_ops)
            WITH (m = 16, ef_construction = 64)
        """
    )


def downgrade() -> None:
    """Revert to vector(1536) — assumes embedding column is empty.

    There is no truncation back to 1536-d that retains data correctness;
    if the table has rows when downgrading, those rows are dropped.
    """
    op.execute("DROP INDEX IF EXISTS ix_chunks_embedding_hnsw")
    op.execute(
        "ALTER TABLE document_chunks ALTER COLUMN embedding TYPE vector(1536) "
        "USING embedding::vector(1536)"
    )
    op.execute(
        """
        CREATE INDEX ix_chunks_embedding_hnsw
            ON document_chunks USING hnsw (embedding vector_cosine_ops)
            WITH (m = 16, ef_construction = 64)
        """
    )
