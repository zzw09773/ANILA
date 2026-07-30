"""Proxy conversation-id ownership checks.

The /v1 proxy accepts ``X-ANILA-Conversation-Id`` from callers for memory
classification and writes. That header must name a conversation owned by the
authenticated caller (admin-tier may bypass), not an arbitrary row id.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.api.proxy import _require_conversation_access
from app.middleware.caller import Caller
from app.models.conversation import Conversation
from tests.conftest import make_user


def test_proxy_rejects_foreign_conversation_id(db) -> None:
    owner = make_user(db, username="conv-owner")
    attacker = make_user(db, username="conv-attacker")
    conv = Conversation(user_id=owner.id, title="private")
    db.add(conv)
    db.commit()

    with pytest.raises(HTTPException) as exc:
        _require_conversation_access(
            db, Caller(user=attacker, api_key_id=None), conv.id
        )
    assert exc.value.status_code == 404
    assert exc.value.detail == "Conversation not found"


def test_proxy_allows_admin_conversation_id(db) -> None:
    owner = make_user(db, username="conv-owner-admin")
    admin = make_user(db, username="conv-admin", role="admin")
    conv = Conversation(user_id=owner.id, title="private")
    db.add(conv)
    db.commit()

    _require_conversation_access(db, Caller(user=admin, api_key_id=None), conv.id)
