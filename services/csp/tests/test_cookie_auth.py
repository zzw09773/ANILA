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
from starlette.responses import Response

from app.middleware import cookies as cookie_module
from app.middleware.cookies import (
    ACCESS_COOKIE_LEGACY_PATH,
    ACCESS_COOKIE_NAME,
    ACCESS_COOKIE_PATHS,
    CSRF_COOKIE_NAME,
    CSRF_COOKIE_PATH,
    REFRESH_COOKIE_NAME,
    REFRESH_COOKIE_PATH,
    clear_session_cookies,
    set_session_cookies,
)
from app.models.platform_setting import PlatformSetting
from app.services.auth_service import TOKEN_LIFETIMES_KEY, create_tokens

from tests.conftest import make_user


def _login(client: TestClient, username: str, password: str = "password") -> dict:
    resp = client.post(
        "/api/auth/login",
        json={"username": username, "password": password},
    )
    assert resp.status_code == 200, resp.text
    return resp


def test_production_cookie_secure_flag_is_true():
    """The production helper itself must keep session cookies HTTPS-only."""
    assert cookie_module._cookie_secure() is True


def test_session_cookies_reuse_lifetimes_resolved_for_token_issuance(db):
    """Cookie Max-Age must not reread a setting changed after signing."""
    user = make_user(db, username="lifetime-user")
    db.add_all(
        [
            PlatformSetting(
                key="auth.access_token_expire_minutes", value="7"
            ),
            PlatformSetting(key="auth.refresh_token_expire_days", value="2"),
        ]
    )
    db.commit()

    tokens = create_tokens(user, db, include_lifetimes=True)
    assert tokens[TOKEN_LIFETIMES_KEY] == (7, 2)

    db.get(PlatformSetting, "auth.access_token_expire_minutes").value = "13"
    db.get(PlatformSetting, "auth.refresh_token_expire_days").value = "30"
    db.commit()

    response = Response()
    set_session_cookies(
        response,
        access_token=tokens["access_token"],
        refresh_token=tokens["refresh_token"],
        db=db,
        token_lifetimes=tokens[TOKEN_LIFETIMES_KEY],
    )
    set_cookies = [
        value.decode("latin-1")
        for name, value in response.raw_headers
        if name == b"set-cookie"
    ]
    access_cookies = [
        cookie for cookie in set_cookies if cookie.startswith(f"{ACCESS_COOKIE_NAME}=")
    ]
    refresh_cookie = next(
        cookie for cookie in set_cookies if cookie.startswith(f"{REFRESH_COOKIE_NAME}=")
    )
    assert len(access_cookies) == len(ACCESS_COOKIE_PATHS)
    assert all("Max-Age=420" in cookie for cookie in access_cookies)
    assert "Max-Age=172800" in refresh_cookie


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


def test_pending_password_login_returns_stable_error_code(client: TestClient, db):
    make_user(db, username="pending-login", is_approved=False)

    resp = client.post(
        "/api/auth/login",
        json={"username": "pending-login", "password": "password"},
    )

    assert resp.status_code == 403
    assert resp.json()["detail"] == {
        "code": "pending_approval",
        "message": "等待核准中，請通知 admin",
    }


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
    assert client.cookies.get(ACCESS_COOKIE_NAME, path="/api")


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


def _set_cookie_headers(response) -> list[str]:
    return [
        value.decode("latin-1")
        for name, value in response.raw_headers
        if name == b"set-cookie"
    ]


def _cookie_path(header: str) -> str | None:
    lower = header.lower()
    idx = lower.find("; path=")
    if idx < 0:
        idx = lower.find("path=")
        if idx < 0:
            return None
        rest = header[idx + len("path="):]
    else:
        rest = header[idx + len("; path="):]
    return rest.split(";", 1)[0]


def test_session_cookies_use_csp_paths_not_site_root(db):
    """Access JWT is scoped off /n8n /gitlab; CSRF stays readable at /."""
    user = make_user(db, username="path-user")
    tokens = create_tokens(user, db, include_lifetimes=True)
    response = Response()
    set_session_cookies(
        response,
        access_token=tokens["access_token"],
        refresh_token=tokens["refresh_token"],
        db=db,
        token_lifetimes=tokens[TOKEN_LIFETIMES_KEY],
    )
    set_cookies = _set_cookie_headers(response)
    access = [
        c for c in set_cookies if c.startswith(f"{ACCESS_COOKIE_NAME}=")
    ]
    refresh = [
        c for c in set_cookies if c.startswith(f"{REFRESH_COOKIE_NAME}=")
    ]
    csrf = [c for c in set_cookies if c.startswith(f"{CSRF_COOKIE_NAME}=")]
    access_paths = {_cookie_path(c) for c in access}
    assert access_paths == set(ACCESS_COOKIE_PATHS)
    assert ACCESS_COOKIE_LEGACY_PATH not in access_paths
    assert {_cookie_path(c) for c in refresh} == {REFRESH_COOKIE_PATH}
    assert {_cookie_path(c) for c in csrf} == {CSRF_COOKIE_PATH}


def test_clear_session_cookies_expires_legacy_root_access_path():
    response = Response()
    clear_session_cookies(response)
    set_cookies = _set_cookie_headers(response)
    access = [
        c for c in set_cookies if c.startswith(f"{ACCESS_COOKIE_NAME}=")
    ]
    assert {_cookie_path(c) for c in access} == set(ACCESS_COOKIE_PATHS) | {
        ACCESS_COOKIE_LEGACY_PATH
    }


def test_access_cookie_paths_cover_admin_docs_pair_not_static():
    """/docs HTML fetches /openapi.json; both need the JWT. /static does not."""
    assert "/docs" in ACCESS_COOKIE_PATHS
    assert "/openapi.json" in ACCESS_COOKIE_PATHS
    assert not any(path == "/static" or path.startswith("/static/") for path in ACCESS_COOKIE_PATHS)


def test_admin_docs_and_openapi_accept_scoped_session_cookie(client: TestClient, db):
    """Cookie Path 收斂後，admin 瀏覽器開 /docs 仍帶得到 access JWT。"""
    make_user(db, username="docs-admin", role="admin")
    _login(client, "docs-admin")

    docs = client.get("/docs")
    assert docs.status_code == 200, docs.text
    assert "swagger" in docs.text.lower() or "openapi" in docs.text.lower()

    schema = client.get("/openapi.json")
    assert schema.status_code == 200, schema.text
    body = schema.json()
    assert "openapi" in body or "paths" in body


def test_non_admin_docs_forbidden_with_scoped_session_cookie(client: TestClient, db):
    make_user(db, username="docs-user", role="user")
    _login(client, "docs-user")

    docs = client.get("/docs")
    assert docs.status_code == 403, docs.text
