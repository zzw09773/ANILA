"""True-PostgreSQL concurrency tests for the durable similarity debounce queue."""

from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace

import asyncpg
import pytest
import pytest_asyncio

from anila_core.storage.adapters.pg_pool import PgPool
from ingestion_worker.similarity_relations import (
    SimilarityLeaseLostError,
    claim_similarity_recompute,
    finish_similarity_recompute,
    recompute_similarity_edges,
    request_similarity_recompute,
)


DSN = os.getenv("ANILA_JOB_STATE_PG_URL")
pytestmark = pytest.mark.skipif(
    not DSN, reason="ANILA_JOB_STATE_PG_URL must point to a disposable PostgreSQL DB"
)


@pytest_asyncio.fixture
async def pool():
    conn = await asyncpg.connect(DSN)
    await conn.execute(
        """
        CREATE EXTENSION IF NOT EXISTS vector;
        DROP TABLE IF EXISTS document_relations, document_chunks,
          ingestion_documents, similarity_recompute_requests,
          ingestion_collections CASCADE;
        CREATE TABLE ingestion_collections(id integer PRIMARY KEY);
        CREATE TABLE similarity_recompute_requests(
          collection_id integer PRIMARY KEY
            REFERENCES ingestion_collections(id) ON DELETE CASCADE,
          status text NOT NULL CHECK (status IN ('pending','running')),
          request_seq bigint NOT NULL CHECK (request_seq >= 1),
          claimed_seq bigint,
          requested_at timestamptz NOT NULL DEFAULT now(),
          not_before timestamptz NOT NULL DEFAULT now(),
          lease_token varchar(64),
          lease_expires_at timestamptz,
          last_error text,
          updated_at timestamptz NOT NULL DEFAULT now(),
          CHECK (
            (status='pending' AND claimed_seq IS NULL AND lease_token IS NULL
              AND lease_expires_at IS NULL)
            OR
            (status='running' AND claimed_seq IS NOT NULL AND lease_token IS NOT NULL
              AND lease_expires_at IS NOT NULL)
          )
        );
        CREATE TABLE ingestion_documents(
          id integer PRIMARY KEY, collection_id integer NOT NULL,
          normalized_title text
        );
        CREATE TABLE document_chunks(
          id serial PRIMARY KEY, collection_id integer NOT NULL,
          document_id integer NOT NULL, is_active_generation boolean NOT NULL,
          chunk_type text NOT NULL, embedding halfvec(3)
        );
        CREATE TABLE document_relations(
          id serial PRIMARY KEY, collection_id integer NOT NULL,
          src_document_id integer NOT NULL, dst_document_id integer,
          target_ref text NOT NULL, relation_type text NOT NULL,
          confidence double precision, source text NOT NULL,
          extractor_run_id text,
          UNIQUE(collection_id,src_document_id,target_ref,relation_type,source)
        );
        INSERT INTO ingestion_collections(id) VALUES (1),(2);
        """
    )
    await conn.close()
    value = PgPool(DSN, min_size=1, max_size=8)
    await value.open()
    try:
        yield value
    finally:
        await value.close()


@pytest.mark.asyncio
async def test_concurrent_requests_dedupe_to_one_monotonic_row(pool):
    sequences = await asyncio.gather(
        *(
            request_similarity_recompute(
                pool, collection_id=1, debounce_seconds=0
            )
            for _ in range(12)
        )
    )

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT status,request_seq,count(*) OVER () AS rows "
            "FROM similarity_recompute_requests WHERE collection_id=1"
        )
    assert row["status"] == "pending"
    assert row["request_seq"] == 12
    assert row["rows"] == 1
    assert sorted(sequences) == list(range(1, 13))


@pytest.mark.asyncio
async def test_concurrent_claim_has_one_owner_and_new_request_becomes_followup(pool):
    await request_similarity_recompute(pool, collection_id=1, debounce_seconds=0)
    first, second = await asyncio.gather(
        claim_similarity_recompute(pool, lease_seconds=60),
        claim_similarity_recompute(pool, lease_seconds=60),
    )
    claims = [claim for claim in (first, second) if claim is not None]
    assert len(claims) == 1
    claim = claims[0]

    # A request arriving while the collection is running advances the sequence
    # without stealing the lease; acknowledgement must preserve one follow-up.
    assert await request_similarity_recompute(
        pool, collection_id=1, debounce_seconds=0
    ) == 2
    assert await finish_similarity_recompute(pool, claim=claim)
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT status,request_seq,claimed_seq,lease_token,lease_expires_at "
            "FROM similarity_recompute_requests WHERE collection_id=1"
        )
    assert tuple(row) == ("pending", 2, None, None, None)


@pytest.mark.asyncio
async def test_expired_lease_is_recovered_once(pool):
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO similarity_recompute_requests
              (collection_id,status,request_seq,claimed_seq,not_before,
               lease_token,lease_expires_at)
            VALUES (2,'running',4,4,now()-interval '1 minute',
                    'expired',now()-interval '1 second')
            """
        )
    first, second = await asyncio.gather(
        claim_similarity_recompute(pool, lease_seconds=60),
        claim_similarity_recompute(pool, lease_seconds=60),
    )
    claims = [claim for claim in (first, second) if claim is not None]
    assert len(claims) == 1
    assert claims[0].collection_id == 2
    assert claims[0].request_seq == 4
    assert claims[0].lease_token != "expired"


@pytest.mark.asyncio
async def test_expired_owner_is_fenced_before_relation_mutation(pool):
    await request_similarity_recompute(pool, collection_id=1, debounce_seconds=0)
    stale = await claim_similarity_recompute(pool, lease_seconds=60)
    assert stale is not None
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE similarity_recompute_requests "
            "SET lease_expires_at=now()-interval '1 second' WHERE collection_id=1"
        )
    successor = await claim_similarity_recompute(pool, lease_seconds=60)
    assert successor is not None
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO document_relations"
            "(collection_id,src_document_id,dst_document_id,target_ref,"
            "relation_type,confidence,source) "
            "VALUES(1,10,11,'sentinel','relates',0.9,'similarity')"
        )

    settings = SimpleNamespace(
        enable_similarity_edges=True,
        similarity_max_docs=500,
        similarity_min=0.75,
        similarity_top_k=3,
    )
    with pytest.raises(SimilarityLeaseLostError):
        await recompute_similarity_edges(
            pool,
            claim=stale,
            collection_id=1,
            run_id="stale-owner",
            settings=settings,
        )
    async with pool.acquire() as conn:
        assert await conn.fetchval(
            "SELECT count(*) FROM document_relations WHERE target_ref='sentinel'"
        ) == 1
