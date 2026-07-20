"""Focused contract tests for direct ingestion queue compatibility helpers."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from anila_security import verify_queue_proof

from app.services import ingestion_queue


QUEUE_KEY = "queue-contract-test-key-012345678901234567890"


class RecordingPool:
    def __init__(self) -> None:
        self.call: tuple[tuple[object, ...], dict[str, object]] | None = None

    async def enqueue_job(self, *args: object, **kwargs: object):
        self.call = (args, kwargs)
        return SimpleNamespace(job_id=kwargs["_job_id"])


@pytest.mark.asyncio
async def test_enqueue_ingest_requires_explicit_job_and_attempt() -> None:
    with pytest.raises(TypeError):
        await ingestion_queue.enqueue_ingest_document(17)
    with pytest.raises(TypeError):
        await ingestion_queue.enqueue_ingest_document(17, attempt_number=1)
    with pytest.raises(TypeError):
        await ingestion_queue.enqueue_ingest_document(17, ingestion_job_id=23)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "ingestion_job_id",
    [None, "", 0, False, -1],
)
async def test_enqueue_ingest_rejects_missing_or_invalid_job_identity(
    monkeypatch, ingestion_job_id
) -> None:
    async def pool_must_not_be_opened():
        raise AssertionError("invalid fenced metadata must fail before Redis")

    monkeypatch.setattr(ingestion_queue, "_get_pool", pool_must_not_be_opened)

    with pytest.raises(ValueError, match="ingestion_job_id must be a positive integer"):
        await ingestion_queue.enqueue_ingest_document(
            17,
            ingestion_job_id=ingestion_job_id,
            attempt_number=1,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("attempt_number", [None, "", 0, False, -1])
async def test_enqueue_ingest_rejects_invalid_attempt_number(
    monkeypatch, attempt_number
) -> None:
    async def pool_must_not_be_opened():
        raise AssertionError("invalid fenced metadata must fail before Redis")

    monkeypatch.setattr(ingestion_queue, "_get_pool", pool_must_not_be_opened)

    with pytest.raises(ValueError, match="attempt_number must be a positive integer"):
        await ingestion_queue.enqueue_ingest_document(
            17,
            ingestion_job_id=23,
            attempt_number=attempt_number,
        )


@pytest.mark.asyncio
async def test_enqueue_ingest_publishes_fenced_payload_and_hmac(monkeypatch) -> None:
    pool = RecordingPool()

    async def get_pool():
        return pool

    monkeypatch.setattr(ingestion_queue, "_get_pool", get_pool)
    monkeypatch.setattr(
        ingestion_queue.settings,
        "INGESTION_QUEUE_HMAC_KEY",
        QUEUE_KEY,
    )

    result = await ingestion_queue.enqueue_ingest_document(
        17,
        ingestion_job_id=23,
        attempt_number=2,
    )

    assert pool.call is not None
    args, kwargs = pool.call
    assert args[:4] == ("ingest_document", 17, 23, 2)
    assert kwargs == {"_job_id": "ingest-job-23-attempt-2"}
    proof = args[4]
    assert isinstance(proof, str)
    verify_queue_proof(
        QUEUE_KEY,
        task_name="ingest_document",
        payload={
            "document_id": 17,
            "ingestion_job_id": 23,
            "attempt_number": 2,
        },
        proof=proof,
    )
    assert result == "ingest-job-23-attempt-2"


@pytest.mark.asyncio
async def test_enqueue_with_metadata_requires_and_forwards_fence(monkeypatch) -> None:
    calls: list[tuple[int, int | None, int]] = []

    async def fake_enqueue(document_id, *, ingestion_job_id, attempt_number):
        calls.append((document_id, ingestion_job_id, attempt_number))
        return "ingest-job-31-attempt-3"

    monkeypatch.setattr(ingestion_queue, "enqueue_ingest_document", fake_enqueue)

    result = await ingestion_queue.enqueue_with_metadata(
        19,
        ingestion_job_id=31,
        attempt_number=3,
    )

    assert calls == [(19, 31, 3)]
    assert result == {"arq_job_id": "ingest-job-31-attempt-3"}
