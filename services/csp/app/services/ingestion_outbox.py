"""Transactional ingestion intent creation and durable Arq relay.

The request path writes no Redis state.  It persists the document,
``ingestion_jobs`` row, and outbox row in one database transaction.  This
relay claims rows in a short ``FOR UPDATE SKIP LOCKED`` transaction, releases
the database lock before touching Redis, and uses a deterministic Arq job id
to make crash replay idempotent.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from arq.connections import ArqRedis
from arq.jobs import Job, JobStatus
from anila_security import create_queue_proof
from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.config import settings
from app.models.ingestion import IngestionDocument, IngestionJob, IngestionOutbox
from app.services.ingestion_queue import _get_pool

logger = logging.getLogger(__name__)

_LEASE_SECONDS = 30
_POLL_SECONDS = 1.0
_MAX_BACKOFF_SECONDS = 300


@dataclass(frozen=True)
class OutboxClaim:
    outbox_id: int
    ingestion_job_id: int
    attempt_number: int
    arq_job_id: str
    task_name: str
    payload: dict[str, Any]
    lease_token: str
    replay_probe: bool


def create_ingestion_dispatch(
    db: Session,
    *,
    document: IngestionDocument,
    enqueued_by: int | None,
) -> IngestionJob:
    """Stage an ingestion job and outbox row in the caller's transaction.

    The caller must add or update ``document`` first and commit exactly once
    after this helper returns.  No external side effect occurs here.
    """
    db.flush()
    if document.id is None:
        raise RuntimeError("document must have a database identity before dispatch")

    job = IngestionJob(
        collection_id=document.collection_id,
        document_id=document.id,
        job_type="ingest",
        status="dispatch_pending",
        progress_pct=0,
        enqueued_by=enqueued_by,
        max_attempts=settings.INGESTION_JOB_MAX_ATTEMPTS,
    )
    db.add(job)
    db.flush()
    if job.id is None:
        raise RuntimeError("ingestion job identity was not allocated")

    # Derived solely from the durable DB job identity.  A relay retry for this
    # intent therefore always addresses the same Arq job.
    job.arq_job_id = f"ingest-job-{job.id}-attempt-1"
    outbox = IngestionOutbox(
        ingestion_job_id=job.id,
        attempt_number=1,
        arq_job_id=job.arq_job_id,
        task_name="ingest_document",
        payload={
            "document_id": document.id,
            "ingestion_job_id": job.id,
            "attempt_number": 1,
        },
        status="pending",
    )
    db.add(outbox)
    db.flush()
    return job


def _claim_next(
    session_factory: Callable[[], Session] = SessionLocal,
    *,
    now: datetime | None = None,
) -> OutboxClaim | None:
    """Lease one due row, draining obsolete history before Redis work."""
    current = now or datetime.now(timezone.utc)
    stale_published_before = current - timedelta(
        seconds=settings.INGESTION_OUTBOX_STALE_SECONDS
    )
    while True:
        db = session_factory()
        try:
            row = (
                db.query(IngestionOutbox)
                .filter(
                    or_(
                        and_(
                            IngestionOutbox.status == "pending",
                            IngestionOutbox.available_at <= current,
                        ),
                        and_(
                            IngestionOutbox.status == "dispatching",
                            IngestionOutbox.lease_expires_at.is_not(None),
                            IngestionOutbox.lease_expires_at <= current,
                        ),
                        and_(
                            IngestionOutbox.status == "published",
                            IngestionOutbox.published_at.is_not(None),
                            IngestionOutbox.published_at <= stale_published_before,
                        ),
                    )
                )
                .order_by(IngestionOutbox.id)
                .with_for_update(skip_locked=True)
                .first()
            )
            if row is None:
                db.rollback()
                return None

            job = (
                db.query(IngestionJob)
                .filter(IngestionJob.id == row.ingestion_job_id)
                .with_for_update()
                .one_or_none()
            )
            expected_previous = row.attempt_number - 1
            replay_probe = row.status == "published"
            can_queue_initial = (
                row.attempt_number == 1
                and job is not None
                and job.status == "dispatch_pending"
                and job.attempt_count == 0
            )
            retry_due_at = job.next_attempt_at if job is not None else None
            if retry_due_at is not None and retry_due_at.tzinfo is None:
                retry_due_at = retry_due_at.replace(tzinfo=timezone.utc)
            can_queue_retry = (
                row.attempt_number > 1
                and job is not None
                and job.status == "retry_wait"
                and job.attempt_count == expected_previous
                and retry_due_at is not None
                and retry_due_at <= current
            )
            if replay_probe and not (
                job is not None
                and job.status == "queued"
                and job.attempt_count == expected_previous
            ):
                # Only a DB-queued logical attempt is eligible for Redis-loss
                # probing. Once the job advances, this publish receipt has no
                # remaining recovery role. Delete it and immediately continue
                # so old terminal history cannot consume one poll interval per
                # row and starve newer pending work.
                db.delete(row)
                db.commit()
                continue
            already_executable_or_executed = (
                job is not None
                and (
                    (job.status == "queued" and job.attempt_count == expected_previous)
                    or (job.status == "running" and job.attempt_count == row.attempt_number)
                    or (
                        job.status in {"succeeded", "failed", "cancelled", "dead_letter"}
                        and job.attempt_count >= row.attempt_number
                    )
                )
            )
            if job is None or row.attempt_number > job.max_attempts or not (
                can_queue_initial or can_queue_retry or already_executable_or_executed
            ):
                row.status = "pending"
                row.available_at = current + timedelta(seconds=5)
                row.lease_token = None
                row.lease_expires_at = None
                row.last_error = "job state is inconsistent with outbox attempt"
                db.commit()
                return None
            if can_queue_initial or can_queue_retry:
                job.status = "queued"
                job.next_attempt_at = None

            lease_token = uuid.uuid4().hex
            row.status = "dispatching"
            row.published_at = None
            row.attempt_count += 1
            row.lease_token = lease_token
            row.lease_expires_at = current + timedelta(seconds=_LEASE_SECONDS)
            row.last_error = None
            claim = OutboxClaim(
                outbox_id=row.id,
                ingestion_job_id=row.ingestion_job_id,
                attempt_number=row.attempt_number,
                arq_job_id=row.arq_job_id,
                task_name=row.task_name,
                payload=dict(row.payload),
                lease_token=lease_token,
                replay_probe=replay_probe,
            )
            db.commit()
            return claim
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()


async def _dispatch_claim(claim: OutboxClaim, pool: ArqRedis) -> None:
    """Enqueue once, or prove that the deterministic duplicate exists."""
    document_id = claim.payload.get("document_id")
    ingestion_job_id = claim.payload.get("ingestion_job_id")
    attempt_number = claim.payload.get("attempt_number")
    if not isinstance(document_id, int):
        raise RuntimeError("ingestion outbox payload has no integer document_id")
    if ingestion_job_id != claim.ingestion_job_id:
        raise RuntimeError("ingestion outbox payload job identity mismatch")
    if attempt_number != claim.attempt_number:
        raise RuntimeError("ingestion outbox payload attempt mismatch")

    if claim.replay_probe:
        existing_status = await Job(claim.arq_job_id, redis=pool).status()
        if existing_status != JobStatus.not_found:
            return

    enqueued = await pool.enqueue_job(
        claim.task_name,
        document_id,
        ingestion_job_id,
        attempt_number,
        create_queue_proof(
            settings.INGESTION_QUEUE_HMAC_KEY,
            task_name=claim.task_name,
            payload={
                "document_id": document_id,
                "ingestion_job_id": ingestion_job_id,
                "attempt_number": attempt_number,
            },
        ),
        _job_id=claim.arq_job_id,
    )
    if enqueued is not None:
        return

    # ``None`` means the unique Arq job id already exists.  Do not infer
    # success from that alone: read it back before acknowledging the outbox.
    existing_status = await Job(claim.arq_job_id, redis=pool).status()
    if existing_status == JobStatus.not_found:
        raise RuntimeError("Arq duplicate response could not be read back")


def _mark_published(
    claim: OutboxClaim,
    session_factory: Callable[[], Session] = SessionLocal,
    *,
    now: datetime | None = None,
) -> None:
    current = now or datetime.now(timezone.utc)
    db = session_factory()
    try:
        updated = (
            db.query(IngestionOutbox)
            .filter(
                IngestionOutbox.id == claim.outbox_id,
                IngestionOutbox.status == "dispatching",
                IngestionOutbox.lease_token == claim.lease_token,
            )
            .update(
                {
                    "status": "published",
                    "published_at": current,
                    "lease_token": None,
                    "lease_expires_at": None,
                    "last_error": None,
                },
                synchronize_session=False,
            )
        )
        if updated != 1:
            raise RuntimeError("ingestion outbox lease was lost before publish")
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _release_failed(
    claim: OutboxClaim,
    exc: Exception,
    session_factory: Callable[[], Session] = SessionLocal,
    *,
    now: datetime | None = None,
) -> None:
    current = now or datetime.now(timezone.utc)
    db = session_factory()
    try:
        row = (
            db.query(IngestionOutbox)
            .filter(
                IngestionOutbox.id == claim.outbox_id,
                IngestionOutbox.status == "dispatching",
                IngestionOutbox.lease_token == claim.lease_token,
            )
            .with_for_update()
            .first()
        )
        if row is None:
            db.rollback()
            return
        delay = min(2 ** min(row.attempt_count, 8), _MAX_BACKOFF_SECONDS)
        row.status = "pending"
        row.available_at = current + timedelta(seconds=delay)
        row.lease_token = None
        row.lease_expires_at = None
        row.last_error = f"{type(exc).__name__}: {exc}"[:1000]
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("Failed to release ingestion outbox lease %s", claim.outbox_id)
    finally:
        db.close()


async def relay_once(
    *,
    session_factory: Callable[[], Session] = SessionLocal,
    pool: ArqRedis | None = None,
) -> bool:
    """Attempt one due intent; return whether a row was claimed."""
    claim = await asyncio.to_thread(_claim_next, session_factory)
    if claim is None:
        return False
    try:
        redis = pool or await _get_pool()
        await _dispatch_claim(claim, redis)
        await asyncio.to_thread(_mark_published, claim, session_factory)
    except asyncio.CancelledError:
        # Preserve the lease.  On process restart it expires and deterministic
        # replay proves the already-enqueued Arq job before acknowledgement.
        raise
    except Exception as exc:
        logger.warning(
            "Ingestion outbox dispatch failed id=%s attempt will retry: %s",
            claim.outbox_id,
            type(exc).__name__,
        )
        await asyncio.to_thread(_release_failed, claim, exc, session_factory)
    return True


async def _relay_loop() -> None:
    while True:
        try:
            processed = await relay_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            # A transient database failure while claiming an intent must not
            # permanently kill the only application-owned relay task.
            logger.exception("Ingestion outbox relay iteration failed")
            processed = False
        if not processed:
            await asyncio.sleep(_POLL_SECONDS)


async def start_ingestion_outbox_relay() -> asyncio.Task[None]:
    """Start the application-owned durable ingestion relay."""
    return asyncio.create_task(_relay_loop(), name="ingestion-outbox-relay")
