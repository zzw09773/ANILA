"""origin=generated 只接受 Router 的服務憑證。使用者自己帶這個旗標沒有用。"""
from __future__ import annotations

import io
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.models.attachment import Attachment
from app.models.conversation import Conversation
from app.models.service_client import ServiceClient
from app.services.attachment_retention import purge_expired_uploads
from app.services.attachment_service import (
    capacity_for_conversation,
    extract_attachment_text,
    reset_forged_generated_origin_warning,
)
from app.services.service_token_envelope import (
    compute_lookup_hash,
    encode_service_token_envelope,
    generate_service_token,
)
from tests.conftest import login, make_user


@pytest.fixture
def storage_root(tmp_path, monkeypatch):
    import app.services.attachment_service as attachment_service

    root = tmp_path / "attachments"
    root.mkdir()
    monkeypatch.setattr(attachment_service, "ATTACHMENT_STORAGE_ROOT", root)
    return root


def _client_row(db, *, name: str, client_type: str, token: str) -> None:
    db.add(
        ServiceClient(
            client_name=name,
            client_type=client_type,
            service_token_envelope=encode_service_token_envelope(token),
            service_token_lookup_hash=compute_lookup_hash(token),
            service_token_issued_at=datetime.now(timezone.utc),
            is_legacy=False,
            is_active=True,
        )
    )
    db.commit()


def _upload(client, user_token, conv_id, name, *, origin=None, service_token=None):
    headers = {"Authorization": f"Bearer {user_token}"}
    if service_token:
        headers["X-CSP-Service-Token"] = service_token
    data = {"conversation_id": str(conv_id)}
    if origin is not None:
        data["origin"] = origin
    resp = client.post(
        "/api/attachments",
        headers=headers,
        files={"file": (name, io.BytesIO(b"hello"), "text/markdown")},
        data=data,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["reference_id"]


def test_user_cannot_mark_generated_but_router_can(
    client: TestClient, db, storage_root, monkeypatch, caplog,
):
    """使用者帶 origin=generated 仍是一般上傳；Router 憑證才存成 generated。"""
    reset_forged_generated_origin_warning()
    monkeypatch.setattr(
        "anila_core.ingestion.parsers.extract_text",
        lambda fn, content, mime_type=None: (content.decode("utf-8"), {}, {}),
    )
    user = make_user(db, username="origin-gate")
    conv = Conversation(user_id=user.id, title="長文", classification_level="機密")
    db.add(conv)
    db.commit()
    db.refresh(conv)
    user_token = login(client, username="origin-gate")
    router_token = generate_service_token()
    studio_token = generate_service_token()
    _client_row(db, name="router-primary", client_type="router", token=router_token)
    _client_row(db, name="anila-studio", client_type="studio", token=studio_token)

    forged = _upload(
        client, user_token, conv.id, "偽造.md", origin="generated",
    )
    studio = _upload(
        client, user_token, conv.id, "工作室.md",
        origin="generated", service_token=studio_token,
    )
    real = _upload(
        client, user_token, conv.id, "真長文.md",
        origin="generated", service_token=router_token,
    )

    def row(ref: str) -> Attachment:
        return db.query(Attachment).filter(Attachment.reference_id == ref).one()

    assert row(forged).origin == "upload"
    assert row(studio).origin == "upload"
    assert row(real).origin == "generated"

    for ref in (forged, studio, real):
        extract_attachment_text(row(ref).id, db=db)
    cap = capacity_for_conversation(db, conv.id)
    assert row(forged).id in cap["admitted_ids"]
    assert row(studio).id in cap["admitted_ids"]
    assert row(real).id not in cap["admitted_ids"]
    assert row(real).id not in cap["excluded_ids"]

    now = datetime(2026, 9, 28, tzinfo=timezone.utc)
    for ref in (forged, studio, real):
        item = row(ref)
        item.created_at = now - timedelta(days=31)
    db.commit()
    assert purge_expired_uploads(db, now=now) == 2
    db.refresh(row(forged))
    db.refresh(row(studio))
    db.refresh(row(real))
    assert row(forged).extract_status == "expired"
    assert row(studio).extract_status == "expired"
    assert row(real).origin == "generated"
    assert row(real).extract_status == "ok"
    assert row(real).extracted_text

    warnings = [
        record for record in caplog.records
        if "generated" in record.getMessage() and record.levelname == "WARNING"
    ]
    assert len(warnings) == 1
    assert "csk-" not in warnings[0].getMessage()
    assert "偽造.md" not in warnings[0].getMessage()
