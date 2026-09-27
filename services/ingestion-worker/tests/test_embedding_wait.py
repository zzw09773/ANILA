"""沒有平台嵌入角色時，入庫先等，不把文件標成失敗。"""
from __future__ import annotations

from datetime import timedelta

import pytest

from ingestion_worker.handlers import (
    EMBEDDING_UNSET_MESSAGE,
    EMBEDDING_WAIT_SECONDS,
    defer_ingest_until_embedding_role,
    remember_collection_embedding,
)


class _Conn:
    def __init__(self):
        self.calls: list[tuple] = []

    async def execute(self, sql, *args):
        self.calls.append((sql, args))


class _Acquire:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *_args):
        return None


class _Pool:
    def __init__(self):
        self.conn = _Conn()

    def acquire(self):
        return _Acquire(self.conn)


class _Redis:
    def __init__(self):
        self.jobs: list[dict] = []

    async def enqueue_job(self, name, document_id, **kwargs):
        self.jobs.append({"name": name, "document_id": document_id, **kwargs})
        return object()


@pytest.mark.asyncio
async def test_missing_embedding_role_waits_and_reenqueues():
    pool = _Pool()
    redis = _Redis()
    result = await defer_ingest_until_embedding_role(
        {"redis": redis}, pool, 7, "job-1",
    )
    assert result["waiting"] is True
    assert result["message"] == EMBEDDING_UNSET_MESSAGE
    assert result["reenqueued"] is True
    status_sql, status_args = pool.conn.calls[0]
    assert "ingestion_documents" in status_sql
    assert status_args[1] == "pending"
    assert status_args[3] == EMBEDDING_UNSET_MESSAGE
    job_sql, job_args = pool.conn.calls[1]
    assert "ingestion_jobs" in job_sql
    assert EMBEDDING_UNSET_MESSAGE in job_args
    queued = redis.jobs[0]
    assert queued["name"] == "ingest_document"
    assert queued["document_id"] == 7
    assert queued["_defer_by"] == timedelta(seconds=EMBEDDING_WAIT_SECONDS)


@pytest.mark.asyncio
async def test_blank_collection_embedding_is_filled_once():
    pool = _Pool()
    await remember_collection_embedding(pool, 3, "console-embed")
    sql, args = pool.conn.calls[0]
    assert "embedding_model IS NULL" in sql
    assert args == (3, "console-embed")
