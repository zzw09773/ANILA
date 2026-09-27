"""長文附件沿用對話的機密等級，並受對話容量約束。"""
from __future__ import annotations

import io

import pytest
from fastapi.testclient import TestClient

from app.models.attachment import Attachment
from app.models.conversation import Conversation
from app.services.attachment_context import admit, attachment_budget_tokens
from app.services.attachment_service import extract_attachment_text
from tests.conftest import login, make_user


@pytest.fixture
def storage_root(tmp_path, monkeypatch):
    import app.services.attachment_service as attachment_service

    root = tmp_path / "attachments"
    root.mkdir()
    monkeypatch.setattr(attachment_service, "ATTACHMENT_STORAGE_ROOT", root)
    return root


def _conv(db, user, *, level: str) -> Conversation:
    conv = Conversation(
        user_id=user.id,
        title="長文",
        classification_level=level,
    )
    db.add(conv)
    db.commit()
    db.refresh(conv)
    return conv


def test_upload_inherits_conversation_classification_and_capacity(
    client: TestClient, db, storage_root, monkeypatch,
):
    from app.models.platform_setting import set_setting
    import app.services.attachment_context as attachment_context
    import app.services.attachment_service as attachment_service

    user = make_user(db, username="long-doc-owner")
    conv = _conv(db, user, level="機密")
    token = login(client, username="long-doc-owner")
    set_setting(db, "limits.attachment_budget_ratio", 0.5)
    db.commit()

    def tiny_window(current_db, model_name):
        return 200

    monkeypatch.setattr(attachment_context, "get_context_window", tiny_window)
    monkeypatch.setattr(attachment_service, "get_context_window", tiny_window)
    monkeypatch.setattr(
        "anila_core.ingestion.parsers.extract_text",
        lambda fn, content, mime_type=None: (content.decode("utf-8"), {}, {}),
    )

    # 約 600 個詞，超過這個測試的容量（視窗 200、比例 0.5）。
    body = ("段落內容 " * 600).encode()
    resp = client.post(
        "/api/attachments",
        headers={"Authorization": f"Bearer {token}"},
        files={"file": ("年度報告.md", io.BytesIO(body), "text/markdown")},
        data={"conversation_id": str(conv.id)},
    )
    assert resp.status_code == 201, resp.text
    payload = resp.json()
    assert payload["conversation_capacity"]["budget_tokens"] > 0

    att = (
        db.query(Attachment)
        .filter(Attachment.reference_id == payload["reference_id"])
        .one()
    )
    assert att.conversation_id == conv.id
    assert att.classification_level == "機密"
    assert att.uploaded_by == user.id

    extract_attachment_text(att.id, db=db)
    db.refresh(att)
    budget = attachment_budget_tokens(db, 200)
    _admitted, excluded = admit(db, [att], budget)
    assert att.id in excluded

    plain = _conv(db, user, level="無機密")
    small = client.post(
        "/api/attachments",
        headers={"Authorization": f"Bearer {token}"},
        files={"file": ("短記.md", io.BytesIO("只有一行。".encode()), "text/markdown")},
        data={"conversation_id": str(plain.id)},
    )
    assert small.status_code == 201, small.text
    short = (
        db.query(Attachment)
        .filter(Attachment.reference_id == small.json()["reference_id"])
        .one()
    )
    assert short.classification_level == "無機密"
