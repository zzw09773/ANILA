"""User-facing model lists must hide the internal router sentinel.

``anila-router`` remains a valid chat target (POST /v1/chat/completions).
Filtering uses ``_is_internal_router_model``:

* ``GET /v1/models`` — always exclude
* ``GET /api/models`` — exclude for non-admin/non-owner only (governance UI
  for admin/owner still shows the sentinel)
* ``GET /api/users/me/allowed-models`` — always exclude
"""
from __future__ import annotations

import os

os.environ.setdefault("ANILA_ALLOW_DEV_SECRET", "1")

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.user import UserModelPermission
from app.services.auth_service import create_tokens

from tests.conftest import make_model, make_user


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _grant_models(db: Session, user_id: int, *model_ids: int) -> None:
    for mid in model_ids:
        db.add(UserModelPermission(user_id=user_id, model_id=mid))
    db.commit()


def test_v1_models_excludes_active_anila_router_sentinel(
    client: TestClient, db: Session,
):
    """Active anila-router + a normal model → list returns only the normal one."""
    admin = make_user(db, username="models_list_admin", role="admin")
    make_model(db, name="anila-router")
    make_model(db, name="gpt-oss-20b")

    resp = client.get(
        "/v1/models",
        headers=_bearer(create_tokens(admin)["access_token"]),
    )

    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["object"] == "list"
    ids = [row["id"] for row in payload["data"]]
    assert "gpt-oss-20b" in ids
    assert "anila-router" not in ids
    assert all(row["object"] == "model" for row in payload["data"])


def test_api_models_hides_sentinel_for_non_admin_keeps_for_admin(
    client: TestClient, db: Session,
):
    """Non-admin /api/models omits anila-router; admin/owner still see it."""
    admin = make_user(db, username="api_models_admin", role="admin")
    user = make_user(db, username="api_models_user", role="user")
    sentinel = make_model(db, name="anila-router")
    normal = make_model(db, name="gpt-oss-20b")
    _grant_models(db, user.id, sentinel.id, normal.id)

    user_resp = client.get(
        "/api/models",
        headers=_bearer(create_tokens(user)["access_token"]),
    )
    assert user_resp.status_code == 200, user_resp.text
    user_names = [row["name"] for row in user_resp.json()]
    assert "gpt-oss-20b" in user_names
    assert "anila-router" not in user_names

    admin_resp = client.get(
        "/api/models",
        headers=_bearer(create_tokens(admin)["access_token"]),
    )
    assert admin_resp.status_code == 200, admin_resp.text
    admin_names = [row["name"] for row in admin_resp.json()]
    assert "gpt-oss-20b" in admin_names
    assert "anila-router" in admin_names


def test_me_allowed_models_excludes_anila_router_sentinel(
    client: TestClient, db: Session,
):
    """GET /api/users/me/allowed-models always hides the router sentinel."""
    admin = make_user(db, username="allowed_models_admin", role="admin")
    user = make_user(db, username="allowed_models_user", role="user")
    sentinel = make_model(db, name="anila-router")
    normal = make_model(db, name="gpt-oss-20b")
    _grant_models(db, user.id, sentinel.id, normal.id)

    for actor in (user, admin):
        resp = client.get(
            "/api/users/me/allowed-models",
            headers=_bearer(create_tokens(actor)["access_token"]),
        )
        assert resp.status_code == 200, resp.text
        ids = {row["id"] for row in resp.json()}
        names_via_display = {row["display_name"] for row in resp.json()}
        assert normal.id in ids
        assert sentinel.id not in ids
        assert "anila-router" not in names_via_display
