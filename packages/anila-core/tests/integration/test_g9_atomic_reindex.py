"""Gate 2 G9: re-index replaces one complete generation atomically."""

from __future__ import annotations

import uuid

import asyncpg
import pytest

from anila_contracts import Classification
from anila_core.ingestion.chunking_plugins import ChunkResult
from anila_core.storage.adapters.pg_pool import PgPool
from anila_core.storage.adapters.pgvector_store import CollectionScopedPgVectorStore


pytestmark = pytest.mark.asyncio
_DIM = 4000


def _parent(content: str) -> ChunkResult:
    return ChunkResult(
        content=content,
        chunk_key="parent",
        token_count=1,
        metadata={"chunk_type": "heading"},
    )


def _leaf(content: str) -> ChunkResult:
    return ChunkResult(
        content=content,
        chunk_key="leaf",
        token_count=1,
        metadata={"chunk_type": "leaf", "parent_chunk_key": "parent"},
    )


async def test_reindex_is_idempotent_and_failed_generation_rolls_back(
    pool: PgPool,
    integration_admin_dsn: str,
    integration_owner_id: int,
) -> None:
    admin = await asyncpg.connect(dsn=integration_admin_dsn)
    collection_id: int | None = None
    try:
        suffix = uuid.uuid4().hex
        collection_id = int(
            await admin.fetchval(
                """
                INSERT INTO ingestion_collections
                    (name, chunking_config, embedding_model, embedding_dim,
                     created_by, classification_level)
                VALUES ($1, '{"strategy":"fixed"}'::jsonb, 'test-model',
                        $2, $3, '機密')
                RETURNING id
                """,
                f"g9-reindex-{suffix}",
                _DIM,
                integration_owner_id,
            )
        )
        document_id = int(
            await admin.fetchval(
                """
                INSERT INTO ingestion_documents
                    (collection_id, filename, sha256, mime_type, status,
                     classification_level)
                VALUES ($1, 'g9.txt', $2, 'text/plain', 'indexed', '機密')
                RETURNING id
                """,
                collection_id,
                "b" * 64,
            )
        )

        store = CollectionScopedPgVectorStore(pool, collection_id=collection_id)
        vector = [0.0] * _DIM
        vector[0] = 1.0
        assert await store.replace_document_chunks(
            document_id=document_id,
            parent_chunks=[_parent("generation one parent")],
            leaf_chunks=[_leaf("generation one leaf")],
            embeddings=[vector],
            classification_level=Classification.UNCLASSIFIED,
        ) == 2

        # The same logical keys can be re-indexed repeatedly without unique
        # collisions and without accumulating stale rows.
        await store.replace_document_chunks(
            document_id=document_id,
            parent_chunks=[_parent("generation two parent")],
            leaf_chunks=[_leaf("generation two leaf")],
            embeddings=[vector],
            classification_level=Classification.UNCLASSIFIED,
        )
        rows = await store.list_by_document(document_id, limit=10)
        assert len(rows) == 2
        assert {row.content for row in rows} == {
            "generation two parent",
            "generation two leaf",
        }
        assert {row.classification_level for row in rows} == {
            Classification.CONFIDENTIAL
        }

        # A database error after DELETE and parent insertion must roll the
        # entire transaction back, preserving generation two byte-for-byte.
        with pytest.raises((asyncpg.PostgresError, ValueError)):
            await store.replace_document_chunks(
                document_id=document_id,
                parent_chunks=[_parent("broken generation parent")],
                leaf_chunks=[_leaf("broken generation leaf")],
                embeddings=[[0.1]],  # halfvec(4000) dimension violation
                classification_level=Classification.UNCLASSIFIED,
            )
        after_failure = await store.list_by_document(document_id, limit=10)
        assert {row.content for row in after_failure} == {
            "generation two parent",
            "generation two leaf",
        }

        # A parser result with zero chunks is also a replacement and removes
        # the previous searchable generation.
        assert await store.replace_document_chunks(
            document_id=document_id,
            parent_chunks=[],
            leaf_chunks=[],
            embeddings=[],
            classification_level=Classification.UNCLASSIFIED,
        ) == 0
        assert await store.list_by_document(document_id, limit=10) == []

        await store.index_chunks(
            document_id=document_id,
            chunks=[
                ChunkResult(
                    content="allowed confidential chunk",
                    chunk_key="allowed-confidential",
                    token_count=2,
                )
            ],
            embeddings=[vector],
            classification_level=Classification.UNCLASSIFIED,
        )
        await store.index_chunks(
            document_id=document_id,
            chunks=[
                ChunkResult(
                    content="must remain top secret",
                    chunk_key="blocked-top-secret",
                    token_count=2,
                )
            ],
            embeddings=[vector],
            classification_level=Classification.TOP_SECRET,
        )
        parent_ids = await store.add_parent_chunks(
            document_id=document_id,
            chunks=[
                ChunkResult(
                    content="top secret parent context",
                    chunk_key="high-parent",
                    token_count=2,
                    metadata={"chunk_type": "heading"},
                )
            ],
            classification_level=Classification.TOP_SECRET,
        )
        await store.index_chunks(
            document_id=document_id,
            chunks=[
                ChunkResult(
                    content="allowed leaf without high parent disclosure",
                    chunk_key="allowed-leaf",
                    token_count=2,
                    metadata={
                        "chunk_type": "leaf",
                        "parent_chunk_key": "high-parent",
                    },
                )
            ],
            embeddings=[vector],
            classification_level=Classification.UNCLASSIFIED,
            parent_id_map=parent_ids,
        )
        governed_hits = await store.similarity_search_scoped_documents(
            query_embedding=vector,
            document_ids=[document_id],
            top_k=10,
            min_score=0.0,
            classification_ceiling=Classification.CONFIDENTIAL,
        )
        assert {hit.chunk.chunk_key for hit in governed_hits} == {
            "allowed-confidential",
            "allowed-leaf",
        }
        leaf_hit = next(
            hit for hit in governed_hits if hit.chunk.chunk_key == "allowed-leaf"
        )
        assert leaf_hit.parent_content is None
    finally:
        if collection_id is not None:
            await admin.execute(
                "DELETE FROM ingestion_collections WHERE id = $1", collection_id
            )
        await admin.close()
