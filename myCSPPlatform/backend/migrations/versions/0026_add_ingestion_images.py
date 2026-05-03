"""Add ``ingestion_images`` table — persist PDF-extracted images for Studio.

## What this enables (Phase 5)

Before this migration, images extracted from PDFs during ingest were
**discarded** after the VLM caption stage:

  ParserRegistry → ImageRef(image_bytes, ...) → caption text → chunks
  (image_bytes garbage-collected here)

Studio could only ever surface the *text* description of a chart, not
the chart itself. For dense visual content (thesis defense decks,
research papers, technical reports) this is a hard ceiling — no amount
of prompt tuning fixes "the chart is missing".

After this migration, the worker persists each captioned image to disk
(``share/uploads/anila-images/<doc_id>/<image_id>.<ext>``) AND inserts a
row here with the caption embedding so Studio can do vector search over
images parallel to chunk search:

  Studio ingest topic
    → embed query (existing path)
        → similarity search on document_chunks (already there)
        → similarity search on ingestion_images (NEW)
              → top-N relevant images for the deck
                → LLM picks one per slide via ``Slide.image_ref``
                  → renderer reads bytes from disk, embeds in .pptx

## Schema decisions

  * ``collection_id`` denormalised onto the row (not via JOIN through
    ingestion_documents) so per-collection vector search stays a single
    indexed lookup. Same trick document_chunks uses.
  * ``embedding halfvec(4000)`` matches document_chunks so we can reuse
    the embedder + HNSW operator class.
  * ``image_id`` is the parser-assigned UUID-ish key that the
    ``[[IMAGE:<id>]]`` placeholder references; UNIQUE (document_id,
    image_id) guards against duplicate inserts on re-ingest.
  * ``storage_path`` is **relative** to the share-uploads root so the
    same row works regardless of how the host mount-point shifts.
  * ON DELETE CASCADE on both FKs: deleting a document or collection
    takes its images with it. This is one of the few places we want
    cascade — orphaned image bytes on disk are pure waste.

## Why not store images inline (BYTEA)

A 6.8 MB PDF can produce 80+ images totalling 50-200 MB. Postgres can
handle this but it bloats every backup, every replication stream, and
every ``pg_dump``. Filesystem storage scales linearly with no DB-side
cost; the ``storage_path`` column is the join key.

Revision ID: 0026
Revises: 0025
Create Date: 2026-04-29
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0026"
# Chains directly onto 0024. (0025_add_action_functions was removed when
# the ANILA Functions v1 prototype was wound down; this migration was
# previously chained onto it and is now rebased to 0024 to keep the
# history linear.)
down_revision: Union[str, None] = "0024"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if "ingestion_images" not in inspector.get_table_names():
        # Use raw DDL for the halfvec column — alembic's autogenerate
        # doesn't know the type. Same approach migration 0015 uses for
        # document_chunks.embedding.
        op.execute(
            """
            CREATE TABLE ingestion_images (
                id              BIGSERIAL    PRIMARY KEY,
                collection_id   INTEGER      NOT NULL
                    REFERENCES ingestion_collections(id) ON DELETE CASCADE,
                document_id     INTEGER      NOT NULL
                    REFERENCES ingestion_documents(id) ON DELETE CASCADE,
                image_id        TEXT         NOT NULL,
                page            INTEGER,
                storage_path    TEXT         NOT NULL,
                mime            TEXT         NOT NULL DEFAULT 'image/png',
                alt_text        TEXT,
                caption         TEXT,
                width           INTEGER,
                height          INTEGER,
                bytes_size      BIGINT,
                embedding       halfvec(4000),
                created_at      TIMESTAMP WITHOUT TIME ZONE
                    NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at      TIMESTAMP WITHOUT TIME ZONE
                    NOT NULL DEFAULT CURRENT_TIMESTAMP,
                CONSTRAINT uq_images_doc_image_id UNIQUE (document_id, image_id)
            );
            """
        )
        # Per-collection scan path. Most queries filter on collection_id
        # before doing vector search; without this index a 100-doc
        # knowledge base would seq-scan all images during retrieval.
        op.execute(
            "CREATE INDEX ix_images_collection ON ingestion_images (collection_id);"
        )
        # HNSW index for cosine similarity. ef_construction=64 / m=16
        # mirrors the document_chunks setup so build/query characteristics
        # are consistent across both vector tables.
        op.execute(
            """
            CREATE INDEX ix_images_embedding_hnsw
              ON ingestion_images
              USING hnsw (embedding halfvec_cosine_ops)
              WITH (m = 16, ef_construction = 64);
            """
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "ingestion_images" in inspector.get_table_names():
        # Drop indexes first; sqlalchemy.inspect lists them but DROP
        # TABLE will cascade them anyway. Explicit drop keeps the
        # downgrade traceable in logs.
        for idx in ("ix_images_embedding_hnsw", "ix_images_collection"):
            op.execute(f"DROP INDEX IF EXISTS {idx};")
        op.execute("DROP TABLE ingestion_images;")
