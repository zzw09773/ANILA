"""Widen ``document_chunks`` unique constraint to include document_id.

The original 0014 constraint was ``UNIQUE (collection_id, chunk_key)``,
which assumed every chunker would emit globally unique keys within a
collection. In practice chunkers naturally produce per-document keys
(``seg-0000`` / ``leaf-0004-Section-Title`` / ``page-0001``), so two
documents in the same collection trivially collide.

Two ways to fix:

1. Make every chunker include ``document_id`` in ``chunk_key`` →
   forces every chunker plug-in to know the storage layer's identity
   model. Bad layering.
2. Widen the unique key to ``(collection_id, document_id, chunk_key)``
   → chunkers stay scope-free; chunks are unique-per-(doc, key), which
   is what they already are. Pick this.

Side effect: re-ingesting the same document re-creates the same set of
``chunk_key`` values, which would re-trigger the constraint. The
ingestion-worker handles that by deleting prior chunks first when
re-indexing the same document_id (Chunk-L scope, separate concern).
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0016"
down_revision: Union[str, None] = "0015"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE document_chunks "
        "DROP CONSTRAINT IF EXISTS uq_chunks_collection_chunk_key"
    )
    op.execute(
        "ALTER TABLE document_chunks "
        "ADD CONSTRAINT uq_chunks_doc_chunk_key "
        "UNIQUE (collection_id, document_id, chunk_key)"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE document_chunks "
        "DROP CONSTRAINT IF EXISTS uq_chunks_doc_chunk_key"
    )
    op.execute(
        "ALTER TABLE document_chunks "
        "ADD CONSTRAINT uq_chunks_collection_chunk_key "
        "UNIQUE (collection_id, chunk_key)"
    )
