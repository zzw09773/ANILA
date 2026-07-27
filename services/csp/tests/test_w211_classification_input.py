# -*- coding: utf-8 -*-
"""W2-11 classification correctness at the input side (SQLite-capable suite).

Covers:
- declaration below collection floor → 400
- create_collection above own clearance → 403
- sampling report fields + review audit + non-admin / no-visibility reject
- unknown classification literal → 422

The upward-latch + ClassificationEvent proof that needs real PG CHECK lives in
``test_w211_classification_input_pg.py``.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from io import BytesIO

import pytest
from anila_contracts import Classification
from fastapi import UploadFile
from fastapi import HTTPException

os.environ.setdefault("ANILA_ALLOW_DEV_SECRET", "1")

from app.api.ingestion import documents
from app.config import settings
from app.models.audit_log import AuditLog
from app.models.classification import ClassificationSamplingReview
from app.models.ingestion import IngestionCollection, IngestionDocument
from app.modules.clearance.service import issue_clearance_grant
from tests.conftest import login, make_user

_FINGERPRINT = "sha256:" + ("ab" * 32)


def _collection(db, owner, *, name="kb-w211", level="無機密") -> IngestionCollection:
    coll = IngestionCollection(
        name=name,
        description=None,
        chunking_config={"strategy": "hierarchical", "params": {}},
        embedding_model="nvidia/NV-embed-V2",
        embedding_fingerprint=_FINGERPRINT,
        embedding_dim=4000,
        status="active",
        document_count=0,
        chunk_count=0,
        bytes_stored=0,
        created_by=owner.id,
        classification_level=level,
    )
    db.add(coll)
    db.commit()
    db.refresh(coll)
    return coll


def _document(db, coll, uploader, *, title="規章 A", level="無機密") -> IngestionDocument:
    doc = IngestionDocument(
        collection_id=coll.id,
        filename=f"{title}.txt",
        title=title,
        normalized_title=title,
        sha256="a" * 64,
        mime_type="text/plain",
        bytes=4,
        storage_path="/tmp/w211-doc",
        status="indexed",
        chunk_count=1,
        uploaded_by=uploader.id,
        classification_level=level,
        classification_latched_at=datetime.now(timezone.utc),
        classification_source="collection_inherited",
    )
    db.add(doc)
    db.commit()
    db.refresh(doc)
    return doc


@pytest.mark.asyncio
async def test_upload_declaration_below_collection_floor_rejected(
    tmp_path, db
) -> None:
    user = make_user(db, username="w211-below")
    coll = _collection(db, user, level="機密")
    documents._UPLOAD_DIR = str(tmp_path)

    with pytest.raises(HTTPException) as excinfo:
        await documents.upload_document(
            coll.id,
            UploadFile(filename="note.txt", file=BytesIO(b"hello")),
            title=None,
            classification_level="無機密",
            db=db,
            current_user=user,
        )
    assert excinfo.value.status_code in (400, 422)
    assert "低於" in str(excinfo.value.detail)
    assert db.query(IngestionDocument).count() == 0


@pytest.mark.asyncio
async def test_upload_unknown_classification_rejected(tmp_path, db) -> None:
    user = make_user(db, username="w211-unknown")
    coll = _collection(db, user, level="無機密")
    documents._UPLOAD_DIR = str(tmp_path)

    with pytest.raises(HTTPException) as excinfo:
        await documents.upload_document(
            coll.id,
            UploadFile(filename="note.txt", file=BytesIO(b"hello")),
            title=None,
            classification_level="秘密",
            db=db,
            current_user=user,
        )
    assert excinfo.value.status_code == 422
    assert db.query(IngestionDocument).count() == 0


def test_create_collection_above_clearance_forbidden(client, db, monkeypatch):
    monkeypatch.setattr(settings, "EMBEDDING_MODEL_FINGERPRINT", _FINGERPRINT)
    manager = make_user(db, username="w211-mgr", role="admin")
    subject = make_user(db, username="w211-creator", role="user")
    now = datetime.now(timezone.utc)
    issue_clearance_grant(
        db,
        actor=manager,
        subject_user_id=subject.id,
        max_classification_level=Classification.UNCLASSIFIED,
        valid_from=now - timedelta(hours=1),
        expires_at=now + timedelta(days=30),
        basis_ticket="W211-CLR-1",
    )
    token = login(client, "w211-creator")

    resp = client.post(
        "/api/ingestion/collections",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "name": "too-high",
            "chunking_config": {"strategy": "hierarchical", "params": {}},
            "classification_level": "機密",
        },
    )
    assert resp.status_code == 403, resp.text
    assert "clearance" in resp.json()["detail"].lower() or "密等" in resp.json()["detail"]
    assert (
        db.query(IngestionCollection)
        .filter(IngestionCollection.name == "too-high")
        .count()
        == 0
    )


def test_create_collection_at_clearance_allowed(client, db, monkeypatch):
    monkeypatch.setattr(settings, "EMBEDDING_MODEL_FINGERPRINT", _FINGERPRINT)
    manager = make_user(db, username="w211-mgr2", role="admin")
    subject = make_user(db, username="w211-ok", role="user")
    now = datetime.now(timezone.utc)
    issue_clearance_grant(
        db,
        actor=manager,
        subject_user_id=subject.id,
        max_classification_level=Classification.CONFIDENTIAL,
        valid_from=now - timedelta(hours=1),
        expires_at=now + timedelta(days=30),
        basis_ticket="W211-CLR-2",
    )
    token = login(client, "w211-ok")

    resp = client.post(
        "/api/ingestion/collections",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "name": "at-ceiling",
            "chunking_config": {"strategy": "hierarchical", "params": {}},
            "classification_level": "機密",
        },
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["classification_level"] == "機密"


def test_sampling_report_and_review_writes_audit(client, db):
    owner = make_user(db, username="w211-owner")
    admin = make_user(db, username="w211-admin", role="admin")
    coll = _collection(db, owner, name="sample-kb", level="營業秘密")
    doc = _document(db, coll, owner, title="抽查文件", level="營業秘密")
    token = login(client, "w211-admin")

    report = client.get(
        "/api/classification/sampling-report",
        params={"n": 10},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert report.status_code == 200, report.text
    body = report.json()
    assert "documents" in body
    assert body["sample_size"] >= 1
    hit = next(d for d in body["documents"] if d["document_id"] == doc.id)
    assert hit["title"] == "抽查文件"
    assert hit["classification_level"] == "營業秘密"
    assert hit["uploader_username"] == "w211-owner"
    assert hit["collection_name"] == "sample-kb"

    review = client.post(
        "/api/classification/sampling-reviews",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "document_id": doc.id,
            "attested_level": "營業秘密",
            "outcome": "confirmed",
            "note": "抽查確認",
        },
    )
    assert review.status_code == 201, review.text
    assert (
        db.query(ClassificationSamplingReview)
        .filter(ClassificationSamplingReview.document_id == doc.id)
        .count()
        == 1
    )
    audit = (
        db.query(AuditLog)
        .filter(AuditLog.action == "classification.sampling_review")
        .order_by(AuditLog.id.desc())
        .first()
    )
    assert audit is not None
    assert str(audit.resource_id) == str(doc.id)


def test_sampling_report_rejects_non_admin(client, db):
    user = make_user(db, username="w211-pleb")
    token = login(client, "w211-pleb")
    resp = client.get(
        "/api/classification/sampling-report",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403


def test_sampling_review_rejects_non_admin(client, db):
    owner = make_user(db, username="w211-owner2")
    coll = _collection(db, owner, name="sample-kb-2")
    doc = _document(db, coll, owner, title="x")
    user = make_user(db, username="w211-pleb2")
    token = login(client, "w211-pleb2")
    resp = client.post(
        "/api/classification/sampling-reviews",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "document_id": doc.id,
            "attested_level": "無機密",
            "outcome": "confirmed",
        },
    )
    assert resp.status_code == 403


# ── 表單欄位宣告(HTTP 層)───────────────────────────────────────────────────
#
# 上面那些 zip 測試(以及真 PG 那支)都是**直接呼叫 Python 函式**,所以它們對
# `classification_level` 究竟被宣告成 Form 還是 Query 完全無感 —— 兩種寫法在函式
# 呼叫層一模一樣,但只有 Form 收得到瀏覽器送出的 multipart 欄位。獨立驗收時是靠人工
# 打真 multipart 才確認的,repo 自己抓不到,所以補這一支。
#
# 判別方式刻意選「宣告低於下限 → 400」:若欄位被宣告成 query,multipart 裡的值會被
# 忽略 → 上傳照常成功(202),這支就紅。


def _zip_payload(members: list[tuple[str, bytes]]) -> bytes:
    import zipfile

    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, body in members:
            zf.writestr(name, body)
    return buf.getvalue()


def test_zip_endpoint_reads_classification_level_from_multipart_form(
    client, db, monkeypatch, tmp_path
):
    monkeypatch.setattr(settings, "EMBEDDING_MODEL_FINGERPRINT", _FINGERPRINT)
    monkeypatch.setattr(documents, "_UPLOAD_DIR", str(tmp_path))
    owner = make_user(db, username="w211-zipform")
    coll = _collection(db, owner, name="zip-form-kb", level="營業秘密")
    token = login(client, "w211-zipform")

    resp = client.post(
        f"/api/ingestion/collections/{coll.id}/documents/zip",
        headers={"Authorization": f"Bearer {token}"},
        files={"file": ("bundle.zip", _zip_payload([("m.txt", b"body")]), "application/zip")},
        data={"classification_level": "無機密"},
    )

    assert resp.status_code == 400, (
        f"宣告低於下限應回 400;實得 {resp.status_code} —— 若是 202,"
        f"表示 multipart 的 classification_level 沒被讀到(欄位宣告成 Query?)"
    )
    assert resp.headers["content-type"].startswith("application/json")
    assert (
        db.query(IngestionDocument)
        .filter(IngestionDocument.collection_id == coll.id)
        .count()
        == 0
    ), "被拒絕的批次不該留下任何文件"
