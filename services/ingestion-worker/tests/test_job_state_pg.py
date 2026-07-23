"""True-PostgreSQL lease fencing and reaper concurrency tests."""

from __future__ import annotations

import asyncio
import os

import asyncpg
import pytest
import pytest_asyncio

from anila_core.storage.adapters.pg_pool import PgPool
from ingestion_worker import job_state


DSN = os.getenv("ANILA_JOB_STATE_PG_URL")
ADMIN_DSN = os.getenv("ANILA_JOB_STATE_PG_ADMIN_URL", DSN)
pytestmark = pytest.mark.skipif(
    not DSN, reason="ANILA_JOB_STATE_PG_URL must point to a disposable PostgreSQL DB"
)


@pytest_asyncio.fixture
async def pool():
    admin = await asyncpg.connect(ADMIN_DSN)
    await admin.execute("CREATE EXTENSION IF NOT EXISTS vector")
    await admin.close()
    conn = await asyncpg.connect(DSN)
    await conn.execute("DROP TABLE IF EXISTS ingestion_outbox, ingestion_jobs, ingestion_documents CASCADE")
    await conn.execute(
        """
        CREATE TABLE ingestion_documents(
          id integer PRIMARY KEY,status varchar(20),processing_stage text DEFAULT 'pending',
          active_generation_id bigint,error_message text);
        CREATE TABLE ingestion_jobs(
          id integer PRIMARY KEY, document_id integer NOT NULL, arq_job_id text,
          status varchar(20) NOT NULL, attempt_count integer NOT NULL DEFAULT 0,
          max_attempts integer NOT NULL DEFAULT 3, lease_token text,
          lease_expires_at timestamptz, heartbeat_at timestamptz,
          next_attempt_at timestamptz, failure_kind text, retryable boolean,
          error_code text,error_message text,progress_pct smallint DEFAULT 0,
          progress_message text,started_at timestamptz,completed_at timestamptz,
          dead_lettered_at timestamptz);
        CREATE TABLE ingestion_outbox(
          id serial PRIMARY KEY,ingestion_job_id integer NOT NULL,
          attempt_number integer NOT NULL,arq_job_id text NOT NULL UNIQUE,
          task_name text NOT NULL,payload jsonb NOT NULL,status text NOT NULL,
          attempt_count integer NOT NULL,available_at timestamptz NOT NULL,
          created_at timestamptz NOT NULL,
          UNIQUE(ingestion_job_id,attempt_number));
        """
    )
    await conn.close()
    value = PgPool(DSN, min_size=1, max_size=5)
    await value.open()
    try:
        yield value
    finally:
        await value.close()


async def _seed(pool, job_id: int, *, status="queued", expired=False):
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO ingestion_documents(id,status,error_message) "
            "VALUES($1,'pending',NULL)",
            job_id,
        )
        await conn.execute(
            """INSERT INTO ingestion_jobs
            (id,document_id,arq_job_id,status,attempt_count,max_attempts,
             lease_token,lease_expires_at,heartbeat_at)
            VALUES($1,$1,$2,$3::text,$4,3,$5,
             CASE WHEN $6 THEN now()-interval '1 second' ELSE now()+interval '1 hour' END,
             CASE WHEN $3::text='running' THEN now() END)""",
            job_id,
            f"ingest-job-{job_id}-attempt-1",
            status,
            1 if status == "running" else 0,
            "lease" if status == "running" else None,
            expired,
        )


@pytest.mark.asyncio
async def test_claim_is_single_owner_and_stale_attempt_cannot_reopen_terminal(pool):
    await _seed(pool, 1)
    first, second = await asyncio.gather(
        job_state.claim_job(pool, job_id=1, document_id=1, attempt_number=1, lease_seconds=60),
        job_state.claim_job(pool, job_id=1, document_id=1, attempt_number=1, lease_seconds=60),
    )
    assert sum(value is not None for value in (first, second)) == 1
    token = first or second
    assert await job_state.succeed(pool, job_id=1, lease_token=token, message="done")
    assert await job_state.claim_job(
        pool, job_id=1, document_id=1, attempt_number=1, lease_seconds=60
    ) is None
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT status FROM ingestion_documents WHERE id=1") == "pending"


@pytest.mark.asyncio
async def test_reaper_skips_valid_lease_and_recovers_expired_once(pool):
    await _seed(pool, 1, status="running", expired=False)
    await _seed(pool, 2, status="running", expired=True)
    counts = await asyncio.gather(
        job_state.reap_expired(pool, limit=10, backoff_seconds=1),
        job_state.reap_expired(pool, limit=10, backoff_seconds=1),
    )
    assert sum(counts) == 1
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT id,status FROM ingestion_jobs ORDER BY id")
        assert [(r["id"], r["status"]) for r in rows] == [(1, "running"), (2, "retry_wait")]
        assert await conn.fetchval("SELECT count(*) FROM ingestion_outbox") == 1
        assert await conn.fetchval(
            "SELECT count(*) FROM ingestion_jobs WHERE status='running' "
            "AND lease_expires_at < now()"
        ) == 0


@pytest.mark.asyncio
async def test_reaper_honours_grace_before_recovering_stale_owner(pool):
    await _seed(pool, 5, status="running", expired=True)
    assert await job_state.reap_expired(
        pool, limit=10, backoff_seconds=1, lease_grace_seconds=60
    ) == 0
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT status FROM ingestion_jobs WHERE id=5") == "running"
        await conn.execute(
            "UPDATE ingestion_jobs SET lease_expires_at=now()-interval '61 seconds' "
            "WHERE id=5"
        )
    assert await job_state.reap_expired(
        pool, limit=10, backoff_seconds=1, lease_grace_seconds=60
    ) == 1
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT status FROM ingestion_jobs WHERE id=5") == "retry_wait"


@pytest.mark.asyncio
async def test_retryable_failure_schedules_durable_attempt_and_permanent_stops(pool):
    await _seed(pool, 3)
    token = await job_state.claim_job(
        pool, job_id=3, document_id=3, attempt_number=1, lease_seconds=60
    )
    assert await job_state.fail_or_retry(
        pool,
        job_id=3,
        document_id=3,
        lease_token=token,
        error_code="E_TRANSIENT",
        error_message="temporary",
        retryable=True,
        backoff_seconds=1,
    ) == "retry_wait"
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM ingestion_outbox") == 1
        await conn.execute(
            "UPDATE ingestion_jobs SET status='queued',next_attempt_at=NULL WHERE id=3"
        )
    token2 = await job_state.claim_job(
        pool, job_id=3, document_id=3, attempt_number=2, lease_seconds=60
    )
    assert await job_state.fail_or_retry(
        pool,
        job_id=3,
        document_id=3,
        lease_token=token2,
        error_code="E_INTERNAL",
        error_message="permanent",
        retryable=False,
        backoff_seconds=1,
    ) == "failed"
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT status FROM ingestion_jobs WHERE id=3") == "failed"
        assert await conn.fetchval("SELECT count(*) FROM ingestion_outbox") == 1


@pytest.mark.asyncio
async def test_retry_exhaustion_is_one_dead_letter_and_has_no_stuck_lease(pool):
    await _seed(pool, 6)
    async with pool.acquire() as conn:
        await conn.execute("UPDATE ingestion_jobs SET max_attempts=2 WHERE id=6")
    token = await job_state.claim_job(
        pool, job_id=6, document_id=6, attempt_number=1, lease_seconds=60
    )
    assert await job_state.fail_or_retry(
        pool,
        job_id=6,
        document_id=6,
        lease_token=token,
        error_code="E_TRANSIENT",
        error_message="temporary",
        retryable=True,
        backoff_seconds=1,
    ) == "retry_wait"
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE ingestion_jobs SET status='queued',next_attempt_at=NULL WHERE id=6"
        )
    token2 = await job_state.claim_job(
        pool, job_id=6, document_id=6, attempt_number=2, lease_seconds=60
    )
    assert await job_state.fail_or_retry(
        pool,
        job_id=6,
        document_id=6,
        lease_token=token2,
        error_code="E_TRANSIENT",
        error_message="still temporary",
        retryable=True,
        backoff_seconds=1,
    ) == "dead_letter"
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT status,completed_at,dead_lettered_at,lease_token "
            "FROM ingestion_jobs WHERE id=6"
        )
        assert row["status"] == "dead_letter"
        assert row["completed_at"] is not None
        assert row["dead_lettered_at"] is not None
        assert row["lease_token"] is None
        assert await conn.fetchval(
            "SELECT count(*) FROM ingestion_jobs WHERE id=6 "
            "AND status NOT IN ('succeeded','failed','cancelled','dead_letter') "
            "AND lease_expires_at < now()"
        ) == 0


@pytest.mark.asyncio
async def test_fail_or_retry_terminal_status_cast_avoids_ambiguous_parameter(pool):
    """Regression: varchar SET + CASE compare on $2 must not AmbiguousParameterError."""
    await _seed(pool, 8)
    async with pool.acquire() as conn:
        await conn.execute("UPDATE ingestion_jobs SET max_attempts=1 WHERE id=8")
    token = await job_state.claim_job(
        pool, job_id=8, document_id=8, attempt_number=1, lease_seconds=60
    )
    assert (
        await job_state.fail_or_retry(
            pool,
            job_id=8,
            document_id=8,
            lease_token=token,
            error_code="E_TRANSIENT",
            error_message="original ingest failure must remain visible",
            retryable=True,
            backoff_seconds=1,
        )
        == "dead_letter"
    )
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT status, dead_lettered_at, error_message FROM ingestion_jobs WHERE id=8"
        )
        assert row["status"] == "dead_letter"
        assert row["dead_lettered_at"] is not None
        assert row["error_message"] == "original ingest failure must remain visible"
        assert await conn.fetchval(
            "SELECT status FROM ingestion_documents WHERE id=8"
        ) == "failed"


@pytest.mark.asyncio
async def test_terminal_write_db_outage_is_fail_loud_then_reaper_recovers(pool):
    await _seed(pool, 7, status="running", expired=False)

    class DownPool:
        class Acquire:
            async def __aenter__(self):
                raise ConnectionError("database temporarily unavailable")

            async def __aexit__(self, *_args):
                return None

        def acquire(self):
            return self.Acquire()

    with pytest.raises(ConnectionError, match="temporarily unavailable"):
        await job_state.fail_or_retry(
            DownPool(),
            job_id=7,
            document_id=7,
            lease_token="lease",
            error_code="E_TRANSIENT",
            error_message="temporary",
            retryable=True,
            backoff_seconds=1,
        )
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT status FROM ingestion_jobs WHERE id=7") == "running"
        await conn.execute(
            "UPDATE ingestion_jobs SET lease_expires_at=now()-interval '16 seconds' "
            "WHERE id=7"
        )
    assert await job_state.reap_expired(
        pool,
        limit=10,
        backoff_seconds=1,
        lease_grace_seconds=15,
    ) == 1
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT status FROM ingestion_jobs WHERE id=7") == "retry_wait"
        assert await conn.fetchval(
            "SELECT count(*) FROM ingestion_outbox WHERE ingestion_job_id=7"
        ) == 1
