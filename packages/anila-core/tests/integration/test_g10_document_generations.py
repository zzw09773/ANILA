"""Gate 3: immutable staging and atomic active-generation publication."""

from __future__ import annotations

import uuid

import asyncpg
import pytest

from anila_contracts import Classification
from anila_core.ingestion.chunking_plugins import ChunkResult
from anila_core.ingestion.errors import StoreError
from anila_core.storage.adapters.pg_pool import PgPool
from anila_core.storage.adapters.pgvector_store import CollectionScopedPgVectorStore


pytestmark = pytest.mark.asyncio
_DIM = 4000
_FP = "sha256:" + ("2" * 64)


def _leaf(content: str, *, chunk_type: str = "leaf") -> ChunkResult:
    return ChunkResult(
        content=content,
        chunk_key="leaf",
        token_count=1,
        metadata={"chunk_type": chunk_type},
    )


async def test_old_active_survives_staging_failure_and_retry_is_idempotent(
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
                  (name,chunking_config,embedding_model,embedding_fingerprint,
                   embedding_dim,created_by,classification_level)
                VALUES($1,'{"strategy":"fixed"}'::jsonb,'test-model',$2,$3,$4,'機密')
                RETURNING id
                """,
                f"g10-generation-{suffix}",
                _FP,
                _DIM,
                integration_owner_id,
            )
        )
        document_id = int(
            await admin.fetchval(
                """
                INSERT INTO ingestion_documents
                  (collection_id,filename,sha256,mime_type,status,
                   classification_level)
                VALUES($1,'generation.txt',$2,'text/plain','pending','機密')
                RETURNING id
                """,
                collection_id,
                "c" * 64,
            )
        )

        async def new_job(label: str) -> tuple[int, str]:
            lease_token = uuid.uuid4().hex
            job_id = int(
                await admin.fetchval(
                    """
                    INSERT INTO ingestion_jobs
                      (arq_job_id,collection_id,document_id,job_type,status,
                       attempt_count,lease_token,lease_expires_at,heartbeat_at)
                    VALUES($1,$2,$3,'index','running',1,$4,
                           now()+interval '5 minutes',now()) RETURNING id
                    """,
                    f"g10-{label}-{suffix}",
                    collection_id,
                    document_id,
                    lease_token,
                )
            )
            return job_id, lease_token

        first_job, first_lease = await new_job("first")
        second_job, second_lease = await new_job("second")
        failed_job, failed_lease = await new_job("failed")
        store = CollectionScopedPgVectorStore(pool, collection_id=collection_id)
        vector = [0.0] * _DIM
        vector[0] = 1.0

        assert await store.stage_and_activate_generation(
            document_id,
            source_ingestion_job_id=first_job,
            source_ingestion_lease_token=first_lease,
            embedding_model="test-model",
            embedding_fingerprint=_FP,
            embedding_dim=_DIM,
            parent_chunks=[],
            leaf_chunks=[_leaf("generation one")],
            embeddings=[vector],
            classification_level=Classification.UNCLASSIFIED,
        ) == 1

        # A visible staging row must not alter retrieval of the old active row.
        staging_id = int(
            await admin.fetchval(
                """
                INSERT INTO ingestion_document_generations
                  (document_id,collection_id,generation_number,status,
                   embedding_model,embedding_fingerprint,embedding_dim,chunk_count)
                VALUES($1,$2,2,'staging','test-model',$3,$4,0) RETURNING id
                """,
                document_id,
                collection_id,
                _FP,
                _DIM,
            )
        )
        assert [row.content for row in await store.list_by_document(document_id)] == [
            "generation one"
        ]
        await admin.execute(
            "DELETE FROM ingestion_document_generations WHERE id=$1", staging_id
        )

        # A DB constraint failure after the staging generation insert rolls the
        # transaction back and leaves generation one active/searchable.
        with pytest.raises(asyncpg.CheckViolationError):
            await store.stage_and_activate_generation(
                document_id,
                source_ingestion_job_id=failed_job,
                source_ingestion_lease_token=failed_lease,
                embedding_model="test-model",
                embedding_fingerprint=_FP,
                embedding_dim=_DIM,
                parent_chunks=[],
                leaf_chunks=[_leaf("broken", chunk_type="invalid")],
                embeddings=[vector],
                classification_level=Classification.UNCLASSIFIED,
            )
        assert [row.content for row in await store.list_by_document(document_id)] == [
            "generation one"
        ]
        assert await admin.fetchval(
            "SELECT count(*) FROM ingestion_document_generations "
            "WHERE source_ingestion_job_id=$1",
            failed_job,
        ) == 0

        assert await store.stage_and_activate_generation(
            document_id,
            source_ingestion_job_id=second_job,
            source_ingestion_lease_token=second_lease,
            embedding_model="test-model",
            embedding_fingerprint=_FP,
            embedding_dim=_DIM,
            parent_chunks=[],
            leaf_chunks=[_leaf("generation two")],
            embeddings=[vector],
            classification_level=Classification.UNCLASSIFIED,
        ) == 1
        assert [row.content for row in await store.list_by_document(document_id)] == [
            "generation two"
        ]
        assert await store.stage_and_activate_generation(
            document_id,
            source_ingestion_job_id=second_job,
            source_ingestion_lease_token=second_lease,
            embedding_model="test-model",
            embedding_fingerprint=_FP,
            embedding_dim=_DIM,
            parent_chunks=[],
            leaf_chunks=[_leaf("ignored retry payload")],
            embeddings=[vector],
            classification_level=Classification.UNCLASSIFIED,
        ) == 1
        assert await admin.fetchval(
            "SELECT count(*) FROM ingestion_document_generations "
            "WHERE document_id=$1",
            document_id,
        ) == 2

        # Mutation-sensitive TOCTOU control: even after an earlier caller-side
        # lease check, expiring the token before this transaction must reject
        # publication/idempotent completion and preserve the active pointer.
        await admin.execute(
            "UPDATE ingestion_jobs SET lease_expires_at=now()-interval '1 second' "
            "WHERE id=$1",
            second_job,
        )
        with pytest.raises(StoreError) as lease_exc:
            await store.stage_and_activate_generation(
                document_id,
                source_ingestion_job_id=second_job,
                source_ingestion_lease_token=second_lease,
                embedding_model="test-model",
                embedding_fingerprint=_FP,
                embedding_dim=_DIM,
                parent_chunks=[],
                leaf_chunks=[_leaf("stale attempt must not publish")],
                embeddings=[vector],
                classification_level=Classification.UNCLASSIFIED,
            )
        assert lease_exc.value.code == "E_INGESTION_LEASE_LOST"
        assert [row.content for row in await store.list_by_document(document_id)] == [
            "generation two"
        ]

        with pytest.raises(ValueError, match="lease_token is required"):
            await store.stage_and_activate_generation(
                document_id,
                source_ingestion_job_id=second_job,
                source_ingestion_lease_token=None,
                embedding_model="test-model",
                embedding_fingerprint=_FP,
                embedding_dim=_DIM,
                parent_chunks=[],
                leaf_chunks=[],
                embeddings=[],
                classification_level=Classification.UNCLASSIFIED,
            )

        mismatch_job, mismatch_lease = await new_job("mismatch")
        with pytest.raises(StoreError) as exc_info:
            await store.stage_and_activate_generation(
                document_id,
                source_ingestion_job_id=mismatch_job,
                source_ingestion_lease_token=mismatch_lease,
                embedding_model="test-model",
                embedding_fingerprint="sha256:" + ("3" * 64),
                embedding_dim=_DIM,
                parent_chunks=[],
                leaf_chunks=[],
                embeddings=[],
                classification_level=Classification.UNCLASSIFIED,
            )
        assert exc_info.value.code == "E_EMBEDDING_CONTRACT_MISMATCH"
    finally:
        if collection_id is not None:
            await admin.execute(
                "DELETE FROM ingestion_collections WHERE id=$1", collection_id
            )
        await admin.close()
