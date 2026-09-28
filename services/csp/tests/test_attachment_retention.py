"""使用者上傳的附件滿 30 天刪檔與抽出文字，列留成墓碑。

對話本身、平台產出的長文、知識庫文件都不在這次清除裡。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.proxy import _inject_attachments
from app.models.attachment import Attachment
from app.models.audit_log import AuditLog
from app.models.conversation import Conversation
from app.models.ingestion import IngestionCollection, IngestionDocument
from app.services.attachment_retention import (
    ATTACHMENT_EXPIRED_MESSAGE,
    purge_expired_uploads,
    retention_action,
)
from tests.conftest import login, make_user


@pytest.fixture
def storage_root(tmp_path, monkeypatch):
    import app.services.attachment_service as attachment_service

    root = tmp_path / "attachments"
    root.mkdir()
    monkeypatch.setattr(attachment_service, "ATTACHMENT_STORAGE_ROOT", root)
    return root


def _write(root: Path, name: str, text: str) -> str:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return name


def _conv(db, user, *, level: str = "無機密") -> Conversation:
    conv = Conversation(
        user_id=user.id,
        title="保留",
        classification_level=level,
    )
    db.add(conv)
    db.commit()
    db.refresh(conv)
    return conv


def _attachment(
    db,
    root: Path,
    user,
    conv: Conversation,
    *,
    filename: str,
    body: str,
    age: timedelta,
    origin: str = "upload",
    now: datetime,
) -> Attachment:
    rel = _write(root, f"{user.id}/{filename}", body)
    att = Attachment(
        conversation_id=conv.id,
        uploaded_by=user.id,
        filename=filename,
        content_type="text/plain",
        size_bytes=len(body.encode()),
        storage_path=rel,
        created_at=now - age,
        extracted_text=body,
        token_count=12,
        page_count=2,
        extract_status="ok",
        origin=origin,
        classification_level=conv.classification_level,
    )
    db.add(att)
    db.commit()
    db.refresh(att)
    return att


def test_purge_after_30_days_leaves_a_tombstone_and_spares_the_rest(
    client: TestClient, db, storage_root,
):
    """滿 30 天的上傳刪檔與抽出文字；未滿、產出文件、知識庫與對話都留著。"""
    now = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)
    user = make_user(db, username="retain-user")
    plain = _conv(db, user)
    secret = _conv(db, user, level="機密")
    expired = _attachment(
        db, storage_root, user, plain,
        filename="過期筆記.txt", body="EXPIRED_UPLOAD_BODY",
        age=timedelta(days=30), now=now,
    )
    classified = _attachment(
        db, storage_root, user, secret,
        filename="過期機密.txt", body="EXPIRED_CLASSIFIED_BODY",
        age=timedelta(days=31), now=now,
    )
    fresh = _attachment(
        db, storage_root, user, plain,
        filename="未滿.txt", body="FRESH_UPLOAD_BODY",
        age=timedelta(days=30) - timedelta(seconds=1), now=now,
    )
    generated = _attachment(
        db, storage_root, user, plain,
        filename="長文.md", body="GENERATED_ANSWER_BODY",
        age=timedelta(days=40), origin="generated", now=now,
    )
    kb_file = storage_root / "kb" / "規章.pdf"
    kb_file.parent.mkdir()
    kb_file.write_bytes(b"KB_BYTES")
    coll = IngestionCollection(
        name="規章",
        chunking_config={"strategy": "fixed"},
        embedding_model="embed",
        embedding_dim=8,
        created_by=user.id,
        origin="anilalm",
    )
    db.add(coll)
    db.commit()
    db.refresh(coll)
    doc = IngestionDocument(
        collection_id=coll.id,
        filename="規章.pdf",
        sha256="a" * 64,
        status="indexed",
        storage_path=str(kb_file),
        uploaded_at=now - timedelta(days=400),
    )
    db.add(doc)
    db.commit()
    db.refresh(doc)

    purged = purge_expired_uploads(db, now=now)
    assert purged == 2

    db.refresh(expired)
    db.refresh(classified)
    db.refresh(fresh)
    db.refresh(generated)
    db.refresh(doc)
    assert db.get(Conversation, plain.id) is not None
    assert db.get(Conversation, secret.id) is not None

    for tombstone, filename in (
        (expired, "過期筆記.txt"),
        (classified, "過期機密.txt"),
    ):
        assert tombstone.filename == filename
        assert tombstone.extracted_text is None
        assert tombstone.token_count is None
        assert tombstone.page_count is None
        assert tombstone.extract_status == "expired"
        assert tombstone.extract_error == ATTACHMENT_EXPIRED_MESSAGE
        assert not (storage_root / tombstone.storage_path).is_file()

    assert fresh.extracted_text == "FRESH_UPLOAD_BODY"
    assert (storage_root / fresh.storage_path).is_file()
    assert generated.extracted_text == "GENERATED_ANSWER_BODY"
    assert generated.origin == "generated"
    assert (storage_root / generated.storage_path).is_file()
    assert doc.status == "indexed"
    assert doc.filename == "規章.pdf"
    assert kb_file.read_bytes() == b"KB_BYTES"

    token = login(client, username="retain-user")
    listed = client.get(
        f"/api/conversations/{plain.id}/attachments",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert listed.status_code == 200, listed.text
    row = next(
        item for item in listed.json()["attachments"]
        if item["filename"] == "過期筆記.txt"
    )
    assert row["extract_status"] == "expired"
    assert row["extract_error"] == ATTACHMENT_EXPIRED_MESSAGE

    downloaded = client.get(
        f"/api/attachments/{expired.reference_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert downloaded.status_code == 410
    assert downloaded.json()["detail"] == ATTACHMENT_EXPIRED_MESSAGE

    body = {"messages": [{"role": "user", "content": "下一題"}]}
    _inject_attachments(db, plain.id, body, None)
    rendered = json.dumps(body.get("messages") or [], ensure_ascii=False)
    assert "EXPIRED_UPLOAD_BODY" not in rendered
    assert "GENERATED_ANSWER_BODY" not in rendered
    assert "FRESH_UPLOAD_BODY" in rendered

    again = purge_expired_uploads(db, now=now)
    assert again == 0
    db.refresh(expired)
    assert expired.extract_status == "expired"
    assert expired.filename == "過期筆記.txt"

    audits = (
        db.query(AuditLog)
        .filter(AuditLog.action == "attachment_retention_purge")
        .order_by(AuditLog.id.asc())
        .all()
    )
    assert [json.loads(row.metadata_json)["purged_count"] for row in audits] == [2, 0]
    for row in audits:
        blob = f"{row.detail or ''}\n{row.metadata_json or ''}"
        assert "過期筆記.txt" not in blob
        assert "過期機密.txt" not in blob
        assert "EXPIRED_UPLOAD_BODY" not in blob


def test_tombstone_is_committed_before_the_file_is_removed(db, storage_root, monkeypatch):
    from pathlib import Path

    from sqlalchemy.orm import Session

    now = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)
    user = make_user(db, username="retain-order")
    conv = _conv(db, user)
    att = _attachment(
        db, storage_root, user, conv,
        filename="先記墓碑.txt", body="ORDER_BODY",
        age=timedelta(days=31), now=now,
    )
    seen: dict[str, str | None] = {}
    real_unlink = Path.unlink

    def spy(self, *args, **kwargs):
        other = Session(bind=db.get_bind())
        try:
            row = other.get(Attachment, att.id)
            seen["status"] = None if row is None else row.extract_status
        finally:
            other.close()
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", spy)
    assert purge_expired_uploads(db, now=now) == 1
    assert seen["status"] == "expired"
    assert not (storage_root / att.storage_path).is_file()


def test_unknown_age_is_stamped_and_not_purged():
    now = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)
    cutoff = now - timedelta(days=30)
    assert retention_action(None, now=now, cutoff=cutoff) == "stamp"
    assert retention_action(now, now=now, cutoff=cutoff) == "keep"
    assert retention_action(now - timedelta(days=31), now=now, cutoff=cutoff) == "purge"


def test_retention_job_starts_and_stops_with_the_app():
    import inspect

    from app.main import lifespan

    src = inspect.getsource(lifespan)
    assert "start_attachment_retention" in src
    assert "retention_task.cancel()" in src
