"""Security contract for unauthenticated conversation share links."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from app.api.public_share import get_shared_conversation
from app.config import settings
from app.models.conversation import Conversation
from app.schemas.contracts.classification import ClassificationLevel
from app.services import conversation_service
from tests.conftest import make_user


def _make_conversation(db, user, *, level: ClassificationLevel) -> Conversation:
    conversation = Conversation(
        user_id=user.id,
        title="share security test",
        # Deliberately preserve the legacy mirror semantics: 營業秘密 is
        # still ``classified=False``. Share authorization must use the
        # five-level value, not this compatibility boolean.
        classified=level >= ClassificationLevel.CONFIDENTIAL,
        classification_level=level.to_storage(),
    )
    db.add(conversation)
    db.commit()
    db.refresh(conversation)
    return conversation


def _as_utc(value: datetime) -> datetime:
    # SQLite drops timezone metadata from DateTime columns in the unit suite.
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def test_create_share_rejects_trade_secret_even_when_legacy_flag_is_false(db):
    user = make_user(db)
    conversation = _make_conversation(
        db, user, level=ClassificationLevel.TRADE_SECRET
    )

    assert conversation.classified is False
    with pytest.raises(HTTPException) as exc:
        conversation_service.create_share(db, conversation.id, user)

    assert exc.value.status_code == 403


def test_create_share_assigns_a_bounded_expiration_when_omitted(db, monkeypatch):
    monkeypatch.setattr(settings, "PUBLIC_SHARE_MAX_TTL_HOURS", 24)
    user = make_user(db)
    conversation = _make_conversation(
        db, user, level=ClassificationLevel.UNCLASSIFIED
    )
    before = datetime.now(timezone.utc)

    share = conversation_service.create_share(db, conversation.id, user)

    assert share.expires_at is not None
    expires_at = _as_utc(share.expires_at)
    assert before + timedelta(hours=23, minutes=59) <= expires_at
    assert expires_at <= datetime.now(timezone.utc) + timedelta(hours=24)


def test_create_share_rejects_expiration_beyond_configured_limit(db, monkeypatch):
    monkeypatch.setattr(settings, "PUBLIC_SHARE_MAX_TTL_HOURS", 24)
    user = make_user(db)
    conversation = _make_conversation(
        db, user, level=ClassificationLevel.UNCLASSIFIED
    )

    with pytest.raises(HTTPException) as exc:
        conversation_service.create_share(
            db,
            conversation.id,
            user,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=25),
        )

    assert exc.value.status_code == 400


def test_public_read_rechecks_current_level_and_invalidates_existing_token(
    db, monkeypatch
):
    monkeypatch.setattr(settings, "ENABLE_PUBLIC_SHARE", True)
    monkeypatch.setattr(settings, "PUBLIC_SHARE_MAX_TTL_HOURS", 24)
    user = make_user(db)
    conversation = _make_conversation(
        db, user, level=ClassificationLevel.UNCLASSIFIED
    )
    share = conversation_service.create_share(db, conversation.id, user)

    # The token was valid when created, then the conversation was upgraded to
    # 營業秘密. The legacy boolean intentionally remains false.
    conversation.classification_level = ClassificationLevel.TRADE_SECRET.to_storage()
    conversation.classified = False
    db.commit()

    with pytest.raises(HTTPException) as exc:
        get_shared_conversation(share.token, db=db)

    # Do not disclose whether a valid token now points at classified content.
    assert exc.value.status_code == 404
    db.refresh(share)
    assert share.view_count == 0


def test_public_read_fails_closed_for_unknown_classification(db, monkeypatch):
    monkeypatch.setattr(settings, "ENABLE_PUBLIC_SHARE", True)
    monkeypatch.setattr(settings, "PUBLIC_SHARE_MAX_TTL_HOURS", 24)
    user = make_user(db)
    conversation = _make_conversation(
        db, user, level=ClassificationLevel.UNCLASSIFIED
    )
    share = conversation_service.create_share(db, conversation.id, user)
    conversation.classification_level = "UNKNOWN"
    db.commit()

    with pytest.raises(HTTPException) as exc:
        get_shared_conversation(share.token, db=db)

    assert exc.value.status_code == 404
