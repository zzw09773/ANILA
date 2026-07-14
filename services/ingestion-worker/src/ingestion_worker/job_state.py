"""Lease-fenced durable state machine for ingestion jobs."""

from __future__ import annotations

import asyncio
import json
import uuid

from anila_core.storage.adapters.pg_pool import PgPool


class LeaseLostError(RuntimeError):
    pass


async def claim_job(
    pool: PgPool,
    *,
    job_id: int,
    document_id: int,
    attempt_number: int,
    lease_seconds: int,
) -> str | None:
    """CAS one queued attempt to running; stale/terminal dispatches are no-ops."""
    token = uuid.uuid4().hex
    sql = """
        UPDATE ingestion_jobs
           SET status = 'running', attempt_count = $3,
               lease_token = $4, heartbeat_at = now(),
               lease_expires_at = now() + ($5 * interval '1 second'),
               started_at = COALESCE(started_at, now()),
               completed_at = NULL, error_code = NULL, error_message = NULL,
               failure_kind = NULL, retryable = NULL
         WHERE id = $1 AND document_id = $2
           AND status = 'queued'
           AND attempt_count + 1 = $3
           AND $3 <= max_attempts
         RETURNING lease_token
    """
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            sql, job_id, document_id, attempt_number, token, lease_seconds
        )
    return token if row is not None else None


async def heartbeat(
    pool: PgPool, *, job_id: int, lease_token: str, lease_seconds: int
) -> bool:
    async with pool.acquire() as conn:
        result = await conn.execute(
            """
            UPDATE ingestion_jobs
               SET heartbeat_at = now(),
                   lease_expires_at = now() + ($3 * interval '1 second')
             WHERE id = $1 AND status = 'running' AND lease_token = $2
            """,
            job_id,
            lease_token,
            lease_seconds,
        )
    return result == "UPDATE 1"


async def heartbeat_loop(
    pool: PgPool,
    *,
    job_id: int,
    lease_token: str,
    lease_seconds: int,
    interval_seconds: int,
    owner_task: asyncio.Task,
) -> None:
    try:
        while True:
            await asyncio.sleep(interval_seconds)
            if not await heartbeat(
                pool,
                job_id=job_id,
                lease_token=lease_token,
                lease_seconds=lease_seconds,
            ):
                owner_task.cancel("ingestion lease lost")
                return
    except asyncio.CancelledError:
        raise
    except Exception:
        owner_task.cancel("ingestion heartbeat failed")


async def ensure_lease(pool: PgPool, *, job_id: int, lease_token: str) -> None:
    async with pool.acquire() as conn:
        valid = await conn.fetchval(
            "SELECT EXISTS(SELECT 1 FROM ingestion_jobs WHERE id=$1 "
            "AND status='running' AND lease_token=$2 "
            "AND lease_expires_at >= now())",
            job_id,
            lease_token,
        )
    if not valid:
        raise LeaseLostError("ingestion job lease is no longer owned")


async def progress(
    pool: PgPool,
    *,
    job_id: int,
    lease_token: str,
    progress_pct: int | None = None,
    progress_message: str | None = None,
) -> bool:
    async with pool.acquire() as conn:
        result = await conn.execute(
            """
            UPDATE ingestion_jobs
               SET progress_pct = COALESCE($3, progress_pct),
                   progress_message = COALESCE($4, progress_message)
             WHERE id = $1 AND status = 'running' AND lease_token = $2
            """,
            job_id,
            lease_token,
            progress_pct,
            progress_message,
        )
    return result == "UPDATE 1"


async def succeed(
    pool: PgPool, *, job_id: int, lease_token: str, message: str
) -> bool:
    async with pool.acquire() as conn:
        result = await conn.execute(
            """
            UPDATE ingestion_jobs
               SET status = 'succeeded', progress_pct = 100,
                   progress_message = $3, completed_at = now(),
                   lease_token = NULL, lease_expires_at = NULL,
                   heartbeat_at = now(), next_attempt_at = NULL
             WHERE id = $1 AND status = 'running' AND lease_token = $2
            """,
            job_id,
            lease_token,
            message,
        )
    return result == "UPDATE 1"


async def fail_or_retry(
    pool: PgPool,
    *,
    job_id: int,
    document_id: int,
    lease_token: str,
    error_code: str,
    error_message: str,
    retryable: bool,
    backoff_seconds: int,
) -> str | None:
    """Fence a failure and atomically create the next-attempt intent."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT attempt_count, max_attempts FROM ingestion_jobs "
                "WHERE id=$1 AND document_id=$2 AND status='running' "
                "AND lease_token=$3 FOR UPDATE",
                job_id,
                document_id,
                lease_token,
            )
            if row is None:
                return None
            attempt = int(row["attempt_count"])
            max_attempts = int(row["max_attempts"])
            if retryable and attempt < max_attempts:
                next_attempt = attempt + 1
                arq_id = f"ingest-job-{job_id}-attempt-{next_attempt}"
                await conn.execute(
                    """
                    UPDATE ingestion_jobs SET status='retry_wait',
                        arq_job_id=$2, error_code=$3, error_message=$4,
                        failure_kind='retryable', retryable=true,
                        next_attempt_at=now()+($5 * interval '1 second'),
                        lease_token=NULL, lease_expires_at=NULL
                    WHERE id=$1
                    """,
                    job_id,
                    arq_id,
                    error_code,
                    error_message,
                    backoff_seconds,
                )
                await conn.execute(
                    """
                    INSERT INTO ingestion_outbox
                      (ingestion_job_id, attempt_number, arq_job_id, task_name,
                       payload, status, attempt_count, available_at, created_at)
                    VALUES ($1,$2,$3,'ingest_document',$4::jsonb,'pending',0,
                            now()+($5 * interval '1 second'),now())
                    ON CONFLICT (ingestion_job_id, attempt_number) DO NOTHING
                    """,
                    job_id,
                    next_attempt,
                    arq_id,
                    json.dumps(
                        {
                            "document_id": document_id,
                            "ingestion_job_id": job_id,
                            "attempt_number": next_attempt,
                        }
                    ),
                    backoff_seconds,
                )
                await conn.execute(
                    "UPDATE ingestion_documents SET processing_stage='pending', "
                    "status=CASE WHEN active_generation_id IS NULL "
                    "THEN 'pending' ELSE 'indexed' END, error_message=$2 "
                    "WHERE id=$1",
                    document_id,
                    error_message,
                )
                return "retry_wait"

            terminal = "dead_letter" if retryable else "failed"
            await conn.execute(
                """
                UPDATE ingestion_jobs SET status=$2, progress_pct=100,
                    error_code=$3, error_message=$4,
                    failure_kind=$5, retryable=$6, completed_at=now(),
                    dead_lettered_at=CASE WHEN $2='dead_letter' THEN now() END,
                    lease_token=NULL, lease_expires_at=NULL,
                    next_attempt_at=NULL
                WHERE id=$1
                """,
                job_id,
                terminal,
                error_code,
                error_message,
                "retry_exhausted" if retryable else "permanent",
                retryable,
            )
            await conn.execute(
                "UPDATE ingestion_documents SET processing_stage='failed', "
                "status=CASE WHEN active_generation_id IS NULL "
                "THEN 'failed' ELSE 'indexed' END, error_message=$2 "
                "WHERE id=$1",
                document_id,
                error_message,
            )
            return terminal


async def reap_expired(
    pool: PgPool,
    *,
    limit: int,
    backoff_seconds: int,
    lease_grace_seconds: int = 0,
) -> int:
    """Recover expired owners after the configured lease grace."""
    reaped = 0
    async with pool.acquire() as conn:
        async with conn.transaction():
            rows = await conn.fetch(
                """
                SELECT id, document_id, attempt_count, max_attempts
                  FROM ingestion_jobs
                 WHERE status='running'
                   AND lease_expires_at
                       < now() - ($2 * interval '1 second')
                 ORDER BY lease_expires_at
                 FOR UPDATE SKIP LOCKED LIMIT $1
                """,
                limit,
                lease_grace_seconds,
            )
            for row in rows:
                job_id = int(row["id"])
                document_id = int(row["document_id"])
                attempt = int(row["attempt_count"])
                if attempt >= int(row["max_attempts"]):
                    await conn.execute(
                        """UPDATE ingestion_jobs SET status='dead_letter',
                        failure_kind='lease_expired', retryable=true,
                        error_code='E_LEASE_EXPIRED', progress_pct=100,
                        error_message='worker lease expired and retries exhausted',
                        completed_at=now(), dead_lettered_at=now(),
                        lease_token=NULL, lease_expires_at=NULL WHERE id=$1""",
                        job_id,
                    )
                    await conn.execute(
                        "UPDATE ingestion_documents SET processing_stage='failed', "
                        "status=CASE WHEN active_generation_id IS NULL "
                        "THEN 'failed' ELSE 'indexed' END, "
                        "error_message='worker retries exhausted' WHERE id=$1",
                        document_id,
                    )
                else:
                    next_attempt = attempt + 1
                    arq_id = f"ingest-job-{job_id}-attempt-{next_attempt}"
                    await conn.execute(
                        """UPDATE ingestion_jobs SET status='retry_wait',
                        arq_job_id=$2, failure_kind='lease_expired', retryable=true,
                        error_code='E_LEASE_EXPIRED',
                        error_message='worker lease expired; retry scheduled',
                        next_attempt_at=now()+($3 * interval '1 second'),
                        lease_token=NULL, lease_expires_at=NULL WHERE id=$1""",
                        job_id,
                        arq_id,
                        backoff_seconds,
                    )
                    await conn.execute(
                        """INSERT INTO ingestion_outbox
                        (ingestion_job_id,attempt_number,arq_job_id,task_name,payload,
                         status,attempt_count,available_at,created_at)
                        VALUES($1,$2,$3,'ingest_document',$4::jsonb,'pending',0,
                               now()+($5 * interval '1 second'),now())
                        ON CONFLICT (ingestion_job_id,attempt_number) DO NOTHING""",
                        job_id,
                        next_attempt,
                        arq_id,
                        json.dumps(
                            {
                                "document_id": document_id,
                                "ingestion_job_id": job_id,
                                "attempt_number": next_attempt,
                            }
                        ),
                        backoff_seconds,
                    )
                    await conn.execute(
                        "UPDATE ingestion_documents SET "
                        "processing_stage='pending', "
                        "status=CASE WHEN active_generation_id IS NULL "
                        "THEN 'pending' ELSE 'indexed' END, "
                        "error_message='worker lease expired; retry scheduled' "
                        "WHERE id=$1",
                        document_id,
                    )
                reaped += 1
    return reaped


async def reaper_loop(
    pool: PgPool,
    *,
    interval_seconds: int,
    limit: int,
    backoff_seconds: int,
    lease_grace_seconds: int = 0,
) -> None:
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            await reap_expired(
                pool,
                limit=limit,
                backoff_seconds=backoff_seconds,
                lease_grace_seconds=lease_grace_seconds,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            # Readiness will surface DB loss; keep the supervisor alive so it
            # resumes recovery when the transient outage clears.
            continue
