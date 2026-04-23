"""Regression tests for SSO API key minting.

The OIDC callback mints a 24h ANILA runtime key on every successful login
(browser sessionStorage is tab-scoped, so each SSO round-trip needs a fresh
key to hand back to the SPA). Before the fix, each hit produced a new row
in ``api_keys`` and left the older SSO-minted rows ``is_active=True`` —
slowly polluting the DB with orphan keys that no SPA tab still holds.

This test pins the expected behavior: newly minted SSO keys deactivate any
prior ``sso-*`` key for the same user so only one SSO key is active at a
time. Non-SSO (user-named) keys are never touched.
"""

from __future__ import annotations

from app.models.api_key import ApiKey
from app.models.model_registry import ModelRegistry
from app.services.api_key_service import create_api_key

from tests.conftest import make_model, make_user


def _seed_model(db) -> ModelRegistry:
    return make_model(db, name="test-llm")


def test_mint_sso_api_key_revokes_prior_sso_keys(db) -> None:
    """Second SSO login deactivates the first SSO key; non-SSO keys survive."""
    from app.api.auth import _mint_sso_api_key

    user = make_user(db, username="alice")
    model = _seed_model(db)

    # A user-named key that must not be touched by the SSO flow.
    user_named, _ = create_api_key(
        db,
        user_id=user.id,
        name="my-cli",
        model_ids=[model.id],
    )
    assert user_named.is_active is True

    first_raw = _mint_sso_api_key(db, user)
    assert first_raw is not None
    sso_keys = (
        db.query(ApiKey)
        .filter(ApiKey.user_id == user.id, ApiKey.name.like("sso-%"))
        .all()
    )
    assert len(sso_keys) == 1
    assert sso_keys[0].is_active is True

    second_raw = _mint_sso_api_key(db, user)
    assert second_raw is not None
    assert second_raw != first_raw

    sso_keys_after = (
        db.query(ApiKey)
        .filter(ApiKey.user_id == user.id, ApiKey.name.like("sso-%"))
        .order_by(ApiKey.id)
        .all()
    )
    assert len(sso_keys_after) == 2, "second mint must not delete history"
    assert sso_keys_after[0].is_active is False, "older SSO key must be revoked"
    assert sso_keys_after[1].is_active is True, "latest SSO key stays active"

    db.refresh(user_named)
    assert user_named.is_active is True, "non-SSO keys must not be revoked"


def test_mint_sso_api_key_only_touches_same_user(db) -> None:
    """SSO minting for Alice must not revoke Bob's SSO key."""
    from app.api.auth import _mint_sso_api_key

    alice = make_user(db, username="alice")
    bob = make_user(db, username="bob")
    _seed_model(db)

    bob_raw = _mint_sso_api_key(db, bob)
    assert bob_raw is not None

    _mint_sso_api_key(db, alice)

    bob_key = (
        db.query(ApiKey)
        .filter(ApiKey.user_id == bob.id, ApiKey.name.like("sso-%"))
        .one()
    )
    assert bob_key.is_active is True, "other users' SSO keys must stay active"
