"""Focused tests for durable card-login challenge orchestration."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from jose import jwt as jose_jwt
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.models.card_login_challenge import CardLoginChallenge
from app.services.card_auth_service import (
    CARD_CHALLENGE_AUDIENCE,
    CardLoginRejected,
    _consume_card_challenge,
    _decode_card_challenge_claims,
    decode_card_challenge,
    issue_card_challenge,
)


def test_decode_card_challenge_preserves_nonce_return_contract(db):
    token, nonce, _expires_in = issue_card_challenge(db)

    assert decode_card_challenge(token) == nonce
    decoded_nonce, jti = _decode_card_challenge_claims(token)
    assert decoded_nonce == nonce
    assert isinstance(jti, str) and 16 <= len(jti) <= 64


def test_decode_card_challenge_rejects_legacy_token_without_jti():
    now = datetime.now(timezone.utc)
    token = jose_jwt.encode(
        {
            "aud": CARD_CHALLENGE_AUDIENCE,
            "nonce": "legacy-stateless-nonce",
            "iat": now,
            "exp": now + timedelta(seconds=120),
        },
        settings.SECRET_KEY,
        algorithm=settings.ALGORITHM,
    )

    with pytest.raises(CardLoginRejected, match="jti"):
        decode_card_challenge(token)


def test_challenge_is_consumed_once_across_database_sessions(db_engine):
    """The database, not worker-local state, arbitrates the one winner."""
    Session = sessionmaker(bind=db_engine)
    first_worker = Session()
    second_worker = Session()
    try:
        token, nonce, _expires_in = issue_card_challenge(first_worker)
        _decoded_nonce, jti = _decode_card_challenge_claims(token)

        _consume_card_challenge(first_worker, jti=jti, nonce=nonce)
        with pytest.raises(CardLoginRejected, match="已使用或過期"):
            _consume_card_challenge(second_worker, jti=jti, nonce=nonce)

        assert second_worker.query(CardLoginChallenge).count() == 0
    finally:
        first_worker.close()
        second_worker.close()
