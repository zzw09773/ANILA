"""A document that produced no chunks must not report itself as indexed.

``ingest_document``'s zero-chunk branch used to write
``status='indexed', chunk_count=0, error_message=None`` and return. The
row then claimed an ``indexed_at`` timestamp for a document with nothing
in the vector index: the workspace sidebar counted it under 「已索引」 and
chat counted it as a source, while it could never contribute a single
passage to an answer. Silent success, of exactly the kind this pipeline's
other terminal paths refuse to produce.

These tests pin the corrected behaviour at the level the handler actually
decides it — the parameters it sends to ``ingestion_documents`` and
``ingestion_jobs`` — so reverting the branch to ``'indexed'`` turns them
red on the status argument itself.

Everything below the handler is faked: the fake pool records the SQL and
bind parameters rather than executing them, which is why this needs
neither Postgres nor an embedding endpoint. That also means the SQL text
itself is not verified here; ``_update_document_status`` and
``_record_job_failure`` are shared with the success paths that production
exercises constantly.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from ingestion_worker import handlers


class _FakeConn:
    """Records every statement instead of running it."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple]] = []

    async def execute(self, sql: str, *args):
        self.calls.append((sql, args))
        return "UPDATE 1"

    async def fetchrow(self, sql: str, *args):
        self.calls.append((sql, args))
        return None


class _FakePool:
    def __init__(self) -> None:
        self.conn = _FakeConn()

    @asynccontextmanager
    async def acquire(self):
        yield self.conn


class _EmptyChunker:
    """A chunker that legitimately finds nothing to chunk — a blank file,
    or a scanned PDF whose pages carry no extractable text."""

    requires_embedder = False

    def chunk(self, text, parse_meta, params):
        return []


def _statements(pool: _FakePool, table: str) -> list[tuple[str, tuple]]:
    return [(sql, args) for sql, args in pool.conn.calls if table in sql]


@pytest.fixture
def wired(monkeypatch, tmp_path):
    """A ctx + pool with everything below ``ingest_document`` faked out."""
    blob = tmp_path / "scan.pdf"
    blob.write_bytes(b"%PDF-1.4 fake")

    async def fake_load_meta(pool, document_id):
        return {
            "collection_id": 7,
            "storage_path": str(blob),
            "filename": "scan.pdf",
            "mime_type": "application/pdf",
            "chunking_config": {"strategy": "hierarchical"},
            "uploaded_by": 3,
            "owner_user_id": 3,
        }

    def fake_extract(filename, blob_bytes, mime_type):
        return ("", {}, [])

    async def fake_resolve(pool, *, settings_fallback_name, settings_fallback_native=None):
        return SimpleNamespace(name="nvidia/nv-embed-v2", native_dim=4096)

    import ingestion_worker.platform_embedding as pe

    monkeypatch.setattr(handlers, "_load_document_meta", fake_load_meta)
    monkeypatch.setattr(handlers, "extract_text", fake_extract)
    monkeypatch.setattr(handlers, "get_chunker", lambda strategy: _EmptyChunker())
    monkeypatch.setattr(pe, "resolve_from_pool", fake_resolve)

    pool = _FakePool()
    ctx = {
        "pool": pool,
        "embedder": SimpleNamespace(
            model_name="nvidia/nv-embed-v2", native_dim=4096,
        ),
        "job_id": "arq-job-zero-chunk",
    }
    return ctx, pool


async def test_zero_chunk_document_is_not_reported_as_indexed(wired):
    """The defect itself: 0 chunks used to mean status='indexed'.

    Mutant: put ``"indexed"`` back in the zero-chunk branch — the status
    parameter changes and this fails.
    """
    ctx, pool = wired

    result = await handlers.ingest_document(ctx, 41)

    doc_updates = _statements(pool, "ingestion_documents")
    assert doc_updates, "the handler never touched the document row"
    # ``_update_document_status`` binds (document_id, status, chunk_count,
    # error_message) in that order.
    statuses = [args[1] for _sql, args in doc_updates]
    assert "indexed" not in statuses, (
        f"a document with zero chunks reported itself as indexed: {statuses}"
    )
    assert statuses[-1] == "failed"
    assert result["chunk_count"] == 0


async def test_zero_chunk_document_carries_a_reason_the_user_can_act_on(wired):
    """``error_message`` is the field that exists for this; the branch
    used to pass None, i.e. "nothing to report".

    Mutant: pass ``error_message=None`` — the row goes back to being an
    unexplained failure and this fails.
    """
    ctx, pool = wired

    await handlers.ingest_document(ctx, 41)

    final_sql, final_args = _statements(pool, "ingestion_documents")[-1]
    assert final_args[1] == "failed"
    assert final_args[2] == 0, "chunk_count must still be recorded as 0"
    reason = final_args[3]
    assert reason, "status='failed' with no error_message tells the user nothing"
    assert "檢索" in reason
    # Actionable, not just descriptive — the user has to know what to do.
    assert "重新上傳" in reason
    # No internal plumbing in a message the uploader reads.
    assert "chunker" not in reason
    assert "document_chunks" not in reason


async def test_zero_chunk_document_also_closes_the_job_row(wired):
    """The branch returned before the success path's ``_update_job``, so
    the job row was left at status='running', progress 30 ('chunking').

    That is not cosmetic. The SSE progress stream
    (``services/csp/app/api/ingestion/jobs.py``) only returns when the
    job reaches a terminal status — ``{"succeeded", "failed",
    "cancelled"}`` — and otherwise heartbeats until a 30-minute hard cap.
    So a user who uploaded a document that produced no chunks watched a
    spinner for half an hour and was then told nothing. Marking the
    document 'failed' without settling the job would fix one table and
    leave the other lying.

    Mutant: drop the ``_record_job_failure`` call — no ingestion_jobs
    statement is issued and this fails.
    """
    ctx, pool = wired

    await handlers.ingest_document(ctx, 41)

    job_updates = _statements(pool, "ingestion_jobs")
    failure_rows = [
        (sql, args) for sql, args in job_updates if "status = 'failed'" in sql
    ]
    assert failure_rows, "the job row was left mid-progress"
    sql, (arq_job_id, error_code, error_message) = failure_rows[-1]
    assert arq_job_id == "arq-job-zero-chunk"
    assert error_code == "E_CHUNK_EMPTY"
    assert error_message
    # The properties that actually stop the spinner: a status the SSE
    # loop treats as terminal, and a completed_at so the row reads as
    # finished rather than abandoned.
    assert "completed_at = now()" in sql
    assert "progress_pct = 100" in sql


async def test_zero_chunk_document_leaves_the_operator_a_log_line(wired, caplog):
    """Before this change the path logged nothing at all.

    The original brief for this package said the zero-chunk path "logs at
    WARNING"; it did not — the only trace was the string ``"no chunks
    produced"`` inside the returned dict, which lands in the arq job
    result and nowhere an operator looks. So the operator got nothing.

    Mutant: delete the ``logger.warning`` call — this fails.
    """
    import logging

    ctx, pool = wired

    with caplog.at_level(logging.WARNING, logger="ingestion_worker.handlers"):
        await handlers.ingest_document(ctx, 41)

    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "E_CHUNK_EMPTY" in logged
    assert "41" in logged


async def test_zero_chunk_document_does_not_retry_the_parse(wired):
    """Returning rather than raising is deliberate: the parse will produce
    zero chunks every time, so handing it to Arq's retry policy would burn
    the whole retry budget on a document that cannot change."""
    ctx, pool = wired

    result = await handlers.ingest_document(ctx, 41)

    assert result["error_code"] == "E_CHUNK_EMPTY"


async def test_zero_chunk_document_is_not_counted_into_the_collection(wired):
    """Unchanged, and asserted so it stays that way: the collection's
    document_count must not include a document nothing can retrieve."""
    ctx, pool = wired

    await handlers.ingest_document(ctx, 41)

    assert not _statements(pool, "ingestion_collections")
