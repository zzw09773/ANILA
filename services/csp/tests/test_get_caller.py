"""Tests for the unified data-plane auth dependency ``get_caller``.

Pins the contract:
- ``sk-*`` Bearer → Caller(user=..., api_key_id=<int>)
- JWT Bearer → Caller(user=..., api_key_id=None)
- Cookie-based JWT (Wave 2 forward-compat) → Caller(user=..., api_key_id=None)
- Missing / malformed / wrong-type credentials → 401
- A JWT that happens to start with ``sk-`` would be mis-routed, but JWTs
  use base64url segments joined by dots and the minter never emits an
  ``sk-``-prefixed JWT — so the discriminator is safe in practice. We
  still assert that a non-API-key ``sk-*`` string raises 401 (not a crash).
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.database import get_db
from app.middleware.caller import ACCESS_COOKIE_NAME, Caller, get_caller
from app.services.api_key_service import create_api_key
from app.services.auth_service import create_tokens
from app.models.model_registry import ModelRegistry

from tests.conftest import make_model, make_user


@pytest.fixture
def client_with_caller(db_engine):
    """A minimal FastAPI app exposing a single route guarded by get_caller.

    Intentionally avoids importing the full app so these tests stay focused
    on the dependency and don't require the whole middleware stack.
    """
    from sqlalchemy.orm import sessionmaker

    app = FastAPI()

    @app.get("/_probe")
    def probe(caller: Caller = __import__("fastapi").Depends(get_caller)):
        return {
            "user_id": caller.user.id,
            "username": caller.user.username,
            "api_key_id": caller.api_key_id,
        }

    Session = sessionmaker(bind=db_engine)

    def override_get_db():
        s = Session()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = override_get_db
    return TestClient(app)


def _issue_access_token(user) -> str:
    return create_tokens(user)["access_token"]


def test_api_key_bearer_returns_caller_with_api_key_id(db, client_with_caller):
    user = make_user(db, username="alice")
    model = make_model(db, name="gpt-4o-mini")
    api_key, raw = create_api_key(
        db, user_id=user.id, name="cli", model_ids=[model.id]
    )

    resp = client_with_caller.get(
        "/_probe", headers={"Authorization": f"Bearer {raw}"}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["user_id"] == user.id
    assert body["username"] == "alice"
    assert body["api_key_id"] == api_key.id


def test_jwt_bearer_returns_caller_with_null_api_key_id(db, client_with_caller):
    user = make_user(db, username="bob")
    token = _issue_access_token(user)

    resp = client_with_caller.get(
        "/_probe", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["user_id"] == user.id
    assert body["api_key_id"] is None


def test_jwt_via_cookie_returns_caller(db, client_with_caller):
    """Wave 2 forward-compat: cookie is read when no Authorization header."""
    user = make_user(db, username="carol")
    token = _issue_access_token(user)

    resp = client_with_caller.get(
        "/_probe", cookies={ACCESS_COOKIE_NAME: token}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["user_id"] == user.id


def test_header_precedes_cookie(db, client_with_caller):
    """If both header and cookie are present, header wins (explicit intent)."""
    alice = make_user(db, username="alice")
    bob = make_user(db, username="bob")
    alice_token = _issue_access_token(alice)
    bob_token = _issue_access_token(bob)

    resp = client_with_caller.get(
        "/_probe",
        headers={"Authorization": f"Bearer {alice_token}"},
        cookies={ACCESS_COOKIE_NAME: bob_token},
    )
    assert resp.status_code == 200
    assert resp.json()["username"] == "alice"


def test_missing_credentials_returns_401(client_with_caller):
    resp = client_with_caller.get("/_probe")
    assert resp.status_code == 401
    assert "認證資訊" in resp.json()["detail"]


def test_malformed_bearer_header_returns_401(client_with_caller):
    # "Bearer " with nothing after it.
    resp = client_with_caller.get(
        "/_probe", headers={"Authorization": "Bearer "}
    )
    assert resp.status_code == 401
    # Plain token without "Bearer" prefix.
    resp2 = client_with_caller.get(
        "/_probe", headers={"Authorization": "not-a-bearer-header"}
    )
    assert resp2.status_code == 401


def test_invalid_api_key_returns_401(client_with_caller):
    resp = client_with_caller.get(
        "/_probe", headers={"Authorization": "Bearer sk-does-not-exist"}
    )
    assert resp.status_code == 401
    assert "API Key" in resp.json()["detail"]


def test_invalid_jwt_returns_401(client_with_caller):
    resp = client_with_caller.get(
        "/_probe", headers={"Authorization": "Bearer not.a.real.jwt"}
    )
    assert resp.status_code == 401


def test_refresh_token_rejected_as_access(db, client_with_caller):
    """A refresh JWT must not be accepted as access (wrong "type" claim)."""
    user = make_user(db, username="dave")
    refresh = create_tokens(user)["refresh_token"]
    resp = client_with_caller.get(
        "/_probe", headers={"Authorization": f"Bearer {refresh}"}
    )
    assert resp.status_code == 401


def test_caller_dataclass_is_immutable(db):
    """Caller is frozen so middleware cannot accidentally mutate identity."""
    user = make_user(db, username="erin")
    caller = Caller(user=user, api_key_id=None)
    with pytest.raises(FrozenInstanceError):
        caller.api_key_id = 999  # type: ignore[misc]
