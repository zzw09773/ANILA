"""Ingestion document delete / reprocess 與 job SSE stream（先前零測試）。

delete：owner 204、非授權 403、缺件 404。
reprocess：failed 文件 enqueue、非授權 403。
stream：缺 job 404、非授權 403、終態至少一幀。

不走 TestClient：沙箱 lifespan 會卡住。直接呼叫 handler + db fixture。
"""
from __future__ import annotations

import asyncio
import hashlib
import json

import pytest
from fastapi import HTTPException
from sqlalchemy.orm import sessionmaker

from app.api.ingestion import documents as documents_api
from app.api.ingestion import jobs as jobs_api
from app.models.ingestion import IngestionCollection, IngestionDocument, IngestionJob
from tests.conftest import make_user


@pytest.fixture(autouse=True)
def _quiet_audit(monkeypatch):
    monkeypatch.setattr(documents_api, "log_audit_event", lambda *a, **k: 1)


def _coll(db, owner_id: int, name: str = "kb") -> IngestionCollection:
    coll = IngestionCollection(
        name=name,
        chunking_config={"strategy": "fixed", "params": {"size": 256}},
        embedding_model="test-embed",
        embedding_dim=8,
        created_by=owner_id,
        origin="csp",
    )
    db.add(coll)
    db.commit()
    db.refresh(coll)
    return coll


def _doc(db, collection_id: int, *, filename: str, status: str = "indexed") -> IngestionDocument:
    digest = hashlib.sha256(f"{collection_id}:{filename}:{status}".encode()).hexdigest()
    doc = IngestionDocument(
        collection_id=collection_id,
        filename=filename,
        sha256=digest,
        status=status,
        error_message="boom" if status == "failed" else None,
    )
    db.add(doc)
    db.commit()
    db.refresh(doc)
    return doc


def _job(db, *, collection_id: int, document_id: int | None, user_id: int, status: str = "queued") -> IngestionJob:
    job = IngestionJob(
        arq_job_id=f"arq-{collection_id}-{document_id}-{status}-{user_id}",
        collection_id=collection_id,
        document_id=document_id,
        job_type="ingest",
        status=status,
        progress_pct=100 if status == "succeeded" else 0,
        progress_message="done" if status == "succeeded" else None,
        enqueued_by=user_id,
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def _patch_enqueue(monkeypatch):
    called: dict = {}

    async def _fake_enqueue(document_id: int) -> str:
        called["document_id"] = document_id
        return f"fake-job-{document_id}"

    monkeypatch.setattr(documents_api, "enqueue_ingest_document", _fake_enqueue)
    return called


def _run(coro):
    return asyncio.run(coro)


# ── delete ───────────────────────────────────────────────────────────────────
def test_delete_document_owner_204(db):
    owner = make_user(db, username="ing_del_owner", role="developer")
    coll = _coll(db, owner.id, name="del-kb")
    doc = _doc(db, coll.id, filename="gone.txt")
    doc_id = doc.id

    resp = documents_api.delete_document(document_id=doc_id, db=db, current_user=owner)
    assert resp.status_code == 204
    db.expire_all()
    assert db.get(IngestionDocument, doc_id) is None


def test_delete_document_non_owner_403(db):
    owner = make_user(db, username="ing_del_real", role="developer")
    other = make_user(db, username="ing_del_other", role="developer")
    coll = _coll(db, owner.id, name="del-secret")
    doc = _doc(db, coll.id, filename="secret.txt")

    with pytest.raises(HTTPException) as ei:
        documents_api.delete_document(document_id=doc.id, db=db, current_user=other)
    assert ei.value.status_code == 403
    db.expire_all()
    assert db.get(IngestionDocument, doc.id) is not None


def test_delete_document_missing_404(db):
    user = make_user(db, username="ing_del_miss", role="developer")
    with pytest.raises(HTTPException) as ei:
        documents_api.delete_document(document_id=999999, db=db, current_user=user)
    assert ei.value.status_code == 404
    assert ei.value.detail == "Document not found"


# ── reprocess ────────────────────────────────────────────────────────────────
def test_reprocess_enqueues_failed_document(db, monkeypatch):
    called = _patch_enqueue(monkeypatch)
    owner = make_user(db, username="ing_re_owner", role="developer")
    coll = _coll(db, owner.id, name="re-kb")
    doc = _doc(db, coll.id, filename="retry.txt", status="failed")

    body = _run(documents_api.reprocess_document(
        document_id=doc.id, db=db, current_user=owner
    ))
    assert body.id == doc.id
    assert body.status == "pending"
    assert called["document_id"] == doc.id

    db.expire_all()
    jobs = (
        db.query(IngestionJob)
        .filter(IngestionJob.document_id == doc.id)
        .all()
    )
    assert len(jobs) == 1
    assert jobs[0].arq_job_id == f"fake-job-{doc.id}"
    assert jobs[0].status == "queued"
    assert db.get(IngestionDocument, doc.id).status == "pending"


def test_reprocess_non_owner_403(db, monkeypatch):
    _patch_enqueue(monkeypatch)
    owner = make_user(db, username="ing_re_real", role="developer")
    other = make_user(db, username="ing_re_other", role="developer")
    coll = _coll(db, owner.id, name="re-secret")
    doc = _doc(db, coll.id, filename="locked.txt", status="failed")

    with pytest.raises(HTTPException) as ei:
        _run(documents_api.reprocess_document(
            document_id=doc.id, db=db, current_user=other
        ))
    assert ei.value.status_code == 403
    db.expire_all()
    assert db.get(IngestionDocument, doc.id).status == "failed"


def test_reprocess_missing_404(db, monkeypatch):
    _patch_enqueue(monkeypatch)
    user = make_user(db, username="ing_re_miss", role="developer")
    with pytest.raises(HTTPException) as ei:
        _run(documents_api.reprocess_document(
            document_id=999999, db=db, current_user=user
        ))
    assert ei.value.status_code == 404
    assert ei.value.detail == "Document not found"


def test_reprocess_not_failed_409(db, monkeypatch):
    _patch_enqueue(monkeypatch)
    owner = make_user(db, username="ing_re_ok", role="developer")
    coll = _coll(db, owner.id, name="re-ok")
    doc = _doc(db, coll.id, filename="indexed.txt", status="indexed")
    with pytest.raises(HTTPException) as ei:
        _run(documents_api.reprocess_document(
            document_id=doc.id, db=db, current_user=owner
        ))
    assert ei.value.status_code == 409


# ── stream ───────────────────────────────────────────────────────────────────
def test_stream_missing_job_404(db):
    user = make_user(db, username="ing_st_miss", role="developer")
    with pytest.raises(HTTPException) as ei:
        _run(jobs_api.stream_job(job_id=999999, db=db, current_user=user))
    assert ei.value.status_code == 404


def test_stream_non_owner_403(db):
    owner = make_user(db, username="ing_st_real", role="developer")
    other = make_user(db, username="ing_st_other", role="developer")
    coll = _coll(db, owner.id, name="st-secret")
    doc = _doc(db, coll.id, filename="stream.txt")
    job = _job(
        db, collection_id=coll.id, document_id=doc.id, user_id=owner.id, status="queued"
    )
    with pytest.raises(HTTPException) as ei:
        _run(jobs_api.stream_job(job_id=job.id, db=db, current_user=other))
    assert ei.value.status_code == 403


def test_stream_emits_one_frame_for_terminal_job(db, db_engine, monkeypatch):
    """終態 job 應立刻吐一幀 snapshot 後結束。"""
    factory = sessionmaker(bind=db_engine, expire_on_commit=False)
    monkeypatch.setattr(jobs_api, "SessionLocal", factory)
    monkeypatch.setattr(jobs_api, "_POLL_SECONDS", 0.01)

    owner = make_user(db, username="ing_st_owner", role="developer")
    coll = _coll(db, owner.id, name="st-kb")
    doc = _doc(db, coll.id, filename="done.txt", status="indexed")
    job = _job(
        db,
        collection_id=coll.id,
        document_id=doc.id,
        user_id=owner.id,
        status="succeeded",
    )

    async def _collect() -> bytes:
        response = await jobs_api.stream_job(
            job_id=job.id, db=db, current_user=owner
        )
        assert response.media_type == "text/event-stream"
        chunks: list[bytes] = []
        async for chunk in response.body_iterator:
            chunks.append(chunk)
        return b"".join(chunks)

    async def _main() -> bytes:
        return await asyncio.wait_for(_collect(), timeout=5)

    body = _run(_main())
    assert body.startswith(b"data: "), body
    first = body.split(b"\n", 1)[0]
    payload = json.loads(first[len(b"data: "):].decode())
    assert payload["id"] == job.id
    assert payload["status"] == "succeeded"
    assert payload["progress_pct"] == 100
