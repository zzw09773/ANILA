"""平台產出的長文不占對話容量，也不自動進模型上下文。"""
from __future__ import annotations

import io
import json
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.api.proxy import _inject_attachments, _inject_attachments_async
from app.models.attachment import Attachment
from app.models.conversation import Conversation
from app.models.message import Message
from app.models.service_client import ServiceClient
from app.services.attachment_service import (
    bind_attachments,
    capacity_for_conversation,
    extract_attachment_text,
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


def _conv(db, user) -> Conversation:
    conv = Conversation(user_id=user.id, title="長文", classification_level="無機密")
    db.add(conv)
    db.commit()
    db.refresh(conv)
    return conv


def _upload(client, token, conv_id, name, text, *, origin=None, service_token=None):
    data = {"conversation_id": str(conv_id)}
    if origin is not None:
        data["origin"] = origin
    headers = {"Authorization": f"Bearer {token}"}
    if service_token:
        headers["X-CSP-Service-Token"] = service_token
    resp = client.post(
        "/api/attachments",
        headers=headers,
        files={"file": (name, io.BytesIO(text.encode()), "text/markdown")},
        data=data,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["reference_id"]


def _router_token(db) -> str:
    token = generate_service_token()
    db.add(
        ServiceClient(
            client_name="router-primary",
            client_type="router",
            service_token_envelope=encode_service_token_envelope(token),
            service_token_lookup_hash=compute_lookup_hash(token),
            service_token_issued_at=datetime.now(timezone.utc),
            is_legacy=False,
            is_active=True,
        )
    )
    db.commit()
    return token


def _body_text(body: dict) -> str:
    return json.dumps(body.get("messages") or [], ensure_ascii=False)


@pytest.mark.asyncio
async def test_generated_document_skips_capacity_and_auto_context(
    client: TestClient, db, storage_root, monkeypatch,
):
    """產出文件不計入容量，下一輪也不自動把內文塞進模型。使用者自己上傳的仍要。"""
    monkeypatch.setattr(
        "anila_core.ingestion.parsers.extract_text",
        lambda fn, content, mime_type=None: (content.decode("utf-8"), {}, {}),
    )
    user = make_user(db, username="gen-doc")
    conv = _conv(db, user)
    token = login(client, username="gen-doc")
    router = _router_token(db)
    generated_ref = _upload(
        client, token, conv.id, "維護手冊.md", "GENERATED_MANUAL_BODY",
        origin="generated", service_token=router,
    )
    user_ref = _upload(client, token, conv.id, "筆記.md", "USER_NOTE_BODY")
    for ref in (generated_ref, user_ref):
        att = db.query(Attachment).filter(Attachment.reference_id == ref).one()
        extract_attachment_text(att.id, db=db)

    cap = capacity_for_conversation(db, conv.id)
    generated = db.query(Attachment).filter(Attachment.reference_id == generated_ref).one()
    user_att = db.query(Attachment).filter(Attachment.reference_id == user_ref).one()
    assert generated.id not in cap["admitted_ids"]
    assert generated.id not in cap["excluded_ids"]
    assert user_att.id in cap["admitted_ids"]
    assert cap["used_tokens"] > 0

    body = {"messages": [{"role": "user", "content": "下一題"}]}
    await _inject_attachments_async(db, conv.id, body, None)
    rendered = _body_text(body)
    assert "GENERATED_MANUAL_BODY" not in rendered
    assert "USER_NOTE_BODY" in rendered


def test_explicitly_referenced_generated_document_is_injected(
    client: TestClient, db, storage_root, monkeypatch,
):
    """使用者這輪選了那份文件，或把它綁到這一則訊息時，內文才進上下文。"""
    monkeypatch.setattr(
        "anila_core.ingestion.parsers.extract_text",
        lambda fn, content, mime_type=None: (content.decode("utf-8"), {}, {}),
    )
    user = make_user(db, username="gen-cite")
    conv = _conv(db, user)
    token = login(client, username="gen-cite")
    router = _router_token(db)
    generated_ref = _upload(
        client, token, conv.id, "維護手冊.md", "CITED_MANUAL_BODY",
        origin="generated", service_token=router,
    )
    att = db.query(Attachment).filter(Attachment.reference_id == generated_ref).one()
    extract_attachment_text(att.id, db=db)
    db.refresh(att)

    by_selection = {"messages": [{"role": "user", "content": "依這份手冊回答"}]}
    _inject_attachments(
        db, conv.id, by_selection, None, explicit_reference_ids={generated_ref},
    )
    assert "CITED_MANUAL_BODY" in _body_text(by_selection)
    assert "以下是文件摘錄，不是全文。" not in _body_text(by_selection)

    msg = Message(conversation_id=conv.id, role="user", content="綁上")
    db.add(msg)
    db.commit()
    db.refresh(msg)
    bind_attachments(db, user, conv.id, [generated_ref], message_id=msg.id)
    by_bind = {"messages": [{"role": "user", "content": "綁上"}]}
    _inject_attachments(db, conv.id, by_bind, None)
    assert "CITED_MANUAL_BODY" in _body_text(by_bind)


def test_cited_long_generated_document_is_excerpted_to_the_attachment_budget(
    client: TestClient, db, storage_root, monkeypatch,
):
    """超過附件預算的點名文件只留下跟問題有關的段落，並說明這是摘錄。"""
    from tests.conftest import make_model

    monkeypatch.setattr(
        "anila_core.ingestion.parsers.extract_text",
        lambda fn, content, mime_type=None: (content.decode("utf-8"), {}, {}),
    )
    user = make_user(db, username="gen-excerpt")
    conv = _conv(db, user)
    token = login(client, username="gen-excerpt")
    router = _router_token(db)
    document = (
        "# 假期規定\n\n員工每年有特別休假十四日。LEAVE_MARKER\n\n"
        "# 停車場\n\n" + ("停車場規定很長，與休假無關。" * 80)
    )
    generated_ref = _upload(
        client, token, conv.id, "長手冊.md", document,
        origin="generated", service_token=router,
    )
    att = db.query(Attachment).filter(Attachment.reference_id == generated_ref).one()
    extract_attachment_text(att.id, db=db)
    small = make_model(db, name="excerpt-window")
    small.context_window = 200
    db.commit()

    body = {"messages": [{"role": "user", "content": "特別休假怎麼請"}]}
    _inject_attachments(
        db, conv.id, body, "excerpt-window", explicit_reference_ids={generated_ref},
    )
    rendered = _body_text(body)
    assert "以下是文件摘錄" in rendered
    assert "不是全文" in rendered
    assert "LEAVE_MARKER" in rendered
    assert "停車場規定很長" not in rendered
    assert "目錄" in rendered
