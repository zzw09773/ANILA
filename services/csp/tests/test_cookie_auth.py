"""Tests for cookie-based SPA authentication (Wave 2a).

Pins the contract:
- ``POST /api/auth/login`` sets three cookies: access + refresh (httpOnly)
  + csrf (non-httpOnly, double-submit token).
- ``/api/auth/me`` accepts the session cookie without any Authorization
  header (the SPA's default flow after login).
- ``POST /api/auth/refresh`` rotates tokens using only the cookie.
- ``POST /api/auth/logout`` clears cookies and bumps ``token_version``
  so any outstanding JWT copies become invalid immediately.
- CSRF middleware rejects mutating cookie-authenticated requests without
  a matching ``X-CSRF-Token`` header.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.middleware.cookies import (
    ACCESS_COOKIE_NAME,
    CSRF_COOKIE_NAME,
    REFRESH_COOKIE_NAME,
)

from tests.conftest import make_user


def _login(client: TestClient, username: str, password: str = "password") -> dict:
    resp = client.post(
        "/api/auth/login",
        json={"username": username, "password": password},
    )
    assert resp.status_code == 200, resp.text
    return resp


def test_login_sets_three_cookies(client: TestClient, db):
    make_user(db, username="alice")

    resp = _login(client, "alice")
    cookies = {c.name: c for c in client.cookies.jar}

    assert ACCESS_COOKIE_NAME in cookies
    assert REFRESH_COOKIE_NAME in cookies
    assert CSRF_COOKIE_NAME in cookies

    # Body must also surface csrf_token for SPA bootstrap convenience.
    body = resp.json()
    assert body["csrf_token"] == cookies[CSRF_COOKIE_NAME].value


def test_me_accepts_session_cookie_without_authorization(client: TestClient, db):
    make_user(db, username="bob")
    _login(client, "bob")

    # No Authorization header; httpOnly cookie carries the session.
    resp = client.get("/api/auth/me")
    assert resp.status_code == 200, resp.text
    assert resp.json()["username"] == "bob"


def test_refresh_via_cookie_rotates_tokens(client: TestClient, db):
    make_user(db, username="carol")
    _login(client, "carol")

    # Refresh cookie is scoped to /api/auth/refresh; TestClient jar honors
    # it. We assert the endpoint succeeds + the JSON body contains a new
    # access_token; comparing byte-level equality against the prior token
    # is fragile because JWT exp has second precision and login+refresh
    # can land in the same second.
    resp = client.post(
        "/api/auth/refresh",
        headers={"X-CSRF-Token": client.cookies.get(CSRF_COOKIE_NAME)},
    )
    assert resp.status_code == 200, resp.text
    new_body = resp.json()
    assert new_body["access_token"]
    assert new_body["refresh_token"]

    # New cookies should have been set too.
    assert client.cookies.get(ACCESS_COOKIE_NAME)


def test_logout_clears_cookies_and_bumps_token_version(client: TestClient, db):
    user = make_user(db, username="dave")
    _login(client, "dave")
    prior_tv = user.token_version or 0

    resp = client.post(
        "/api/auth/logout",
        headers={"X-CSRF-Token": client.cookies.get(CSRF_COOKIE_NAME)},
    )
    assert resp.status_code == 200

    # Post-logout the session cookie should not work any more.
    probe = client.get("/api/auth/me")
    # Either 401 (cookies cleared) or 401 (token_version mismatch) —
    # both surface as 401.
    assert probe.status_code == 401

    db.refresh(user)
    assert user.token_version == prior_tv + 1


def test_csrf_required_on_mutating_cookie_request(client: TestClient, db):
    make_user(db, username="erin")
    _login(client, "erin")

    # Missing X-CSRF-Token → 403
    resp = client.post("/api/auth/logout")
    assert resp.status_code == 403, resp.text
    assert "CSRF" in resp.json()["detail"]


def test_csrf_skipped_for_bearer_authorization(client: TestClient, db):
    """SDK / curl flow presents Authorization header; CSRF must not block."""
    from app.services.auth_service import create_tokens

    user = make_user(db, username="sdk_caller")
    token = create_tokens(user)["access_token"]

    # Clear any residual cookies from prior logins.
    client.cookies.clear()

    # POST without CSRF header but with Authorization: Bearer — must pass
    # the CSRF middleware (dependency below may still enforce other rules,
    # but it won't be a 403 from the middleware).
    resp = client.post(
        "/api/auth/logout",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code != 403, resp.text


def test_safe_methods_never_need_csrf(client: TestClient, db):
    make_user(db, username="frank")
    _login(client, "frank")

    # GET without X-CSRF-Token succeeds.
    resp = client.get("/api/auth/me")
    assert resp.status_code == 200
