"""Refusal monitoring (harness §6-9, wired 2026-09-02): measure, never bypass.

``refusal_detector.looks_like_refusal`` existed but nothing called it. Now:
  1. an assistant message whose content looks like a refusal is stored with
     ``metadata.refusal_suspected = True`` — on the non-stream persist path
     (``append_message``) and on the streamed finalize path
     (``update_message_content`` with content);
  2. ordinary answers and user messages are never flagged;
  3. the governance feedback summary reports how many assistant messages in
     the window were flagged, so an operator sees the rate without reading
     conversations.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.api.admin import feedback as feedback_api
from app.models.conversation import Conversation
from app.models.message import Message
from app.services import conversation_service as cs
from app.services.auth_service import create_tokens
from tests.conftest import make_user

REFUSAL_ZH = "本系統僅提供法律文件檢索與解釋服務，無法回答與法律無關的問題。請提出與法律相關的查詢。"
REFUSAL_EN = "I'm sorry, but I can't help with that request."
NORMAL = "軍人的違紀行為可分為勤務上與勤務外兩類 [1]。"


def _conv(db, user) -> Conversation:
    conv = Conversation(user_id=user.id, title="refusal-seed", classification_level="無機密")
    db.add(conv)
    db.commit()
    db.refresh(conv)
    return conv


@pytest.mark.parametrize("text", [REFUSAL_ZH, REFUSAL_EN])
def test_append_message_flags_an_assistant_refusal(db, text):
    user = make_user(db, "rf_a")
    conv = _conv(db, user)
    cs.append_message(db, conv.id, user, role="user", content="幫我查差勤")
    msg = cs.append_message(db, conv.id, user, role="assistant", content=text)
    assert (msg.metadata_ or {}).get("refusal_suspected") is True


def test_append_message_does_not_flag_normal_answers_or_user_text(db):
    user = make_user(db, "rf_b")
    conv = _conv(db, user)
    u = cs.append_message(db, conv.id, user, role="user", content=REFUSAL_ZH)
    a = cs.append_message(db, conv.id, user, role="assistant", content=NORMAL)
    assert not (u.metadata_ or {}).get("refusal_suspected")
    assert not (a.metadata_ or {}).get("refusal_suspected")


def test_streamed_finalize_flags_refusal_and_keeps_other_metadata(db):
    user = make_user(db, "rf_c")
    conv = _conv(db, user)
    cs.append_message(db, conv.id, user, role="user", content="q")
    msg = cs.append_message(db, conv.id, user, role="assistant", content="", metadata={"citations": []})
    patched = cs.update_message_content(db, conv.id, msg.id, user, content=REFUSAL_ZH)
    assert patched.metadata_.get("refusal_suspected") is True
    assert patched.metadata_.get("citations") == []


def test_feedback_summary_counts_flagged_assistant_messages(client, db):
    admin = make_user(db, "rf_admin", role="admin")
    user = make_user(db, "rf_d")
    conv = _conv(db, user)
    for text in (REFUSAL_ZH, NORMAL, REFUSAL_EN):
        db.add(Message(conversation_id=conv.id, role="assistant", content=text, classification_level="無機密",
                       metadata_={"refusal_suspected": True} if text != NORMAL else None,
                       created_at=datetime.now(timezone.utc)))
    db.commit()
    token = create_tokens(admin)["access_token"]
    resp = client.get("/api/admin/feedback?days=7", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["summary"]["refusal_suspected"] == 2
