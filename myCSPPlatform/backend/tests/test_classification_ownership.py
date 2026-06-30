"""IDOR regression: the classification latch must only affect the
requester's OWN conversation.

Reproduces and guards the cross-user classification write where the
``X-ANILA-Conversation-Id`` header was trusted as the UPDATE key without
any ownership check — letting an authenticated user permanently flip
``classified=true`` on another user's conversation (one-way, no declassify).
"""

from __future__ import annotations

from app.api.proxy import (
    _latch_agent_classification,
    _latch_inherited_classification,
)
from app.models.conversation import Conversation

OWNER_ID = 1
ATTACKER_ID = 2


def _make_conversation(db, *, user_id: int) -> Conversation:
    conv = Conversation(user_id=user_id, title="t")
    db.add(conv)
    db.commit()
    db.refresh(conv)
    return conv


def test_inherited_latch_ignores_other_users_conversation(db):
    conv = _make_conversation(db, user_id=OWNER_ID)
    _latch_inherited_classification(db, conv.id, user_id=ATTACKER_ID)
    db.refresh(conv)
    assert conv.classified is False
    assert conv.classification_inherited is False


def test_inherited_latch_applies_for_owner(db):
    conv = _make_conversation(db, user_id=OWNER_ID)
    _latch_inherited_classification(db, conv.id, user_id=OWNER_ID)
    db.refresh(conv)
    assert conv.classified is True
    assert conv.classification_inherited is True


def test_agent_latch_ignores_other_users_conversation(db):
    conv = _make_conversation(db, user_id=OWNER_ID)
    _latch_agent_classification(db, conv.id, user_id=ATTACKER_ID)
    db.refresh(conv)
    assert conv.classified is False


def test_agent_latch_applies_for_owner(db):
    conv = _make_conversation(db, user_id=OWNER_ID)
    _latch_agent_classification(db, conv.id, user_id=OWNER_ID)
    db.refresh(conv)
    assert conv.classified is True
