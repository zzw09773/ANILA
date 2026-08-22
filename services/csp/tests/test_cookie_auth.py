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

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from starlette.responses import Response

from app.middleware import cookies as cookie_module
from app.middleware.cookies import (
    ACCESS_COOKIE_NAME,
    CSRF_COOKIE_NAME,
    REFRESH_COOKIE_NAME,
    REFRESH_COOKIE_PATH,
    clear_session_cookies,
    set_session_cookies,
)
from app.models.platform_setting import PlatformSetting
from app.schemas.user import UserResponse
from app.services.auth_service import TOKEN_LIFETIMES_KEY, create_tokens
from app.utils.security import create_access_token

from tests.conftest import make_user


def _set_cookie_headers(response) -> list[str]:
    return [
        value.decode("latin-1")
        for name, value in response.raw_headers
        if name == b"set-cookie"
    ]


def _cookie_attr(header: str, attr: str) -> str | None:
    prefix = f"{attr}="
    for part in header.split(";"):
        trimmed = part.strip()
        if trimmed.lower().startswith(prefix.lower()):
            return trimmed.split("=", 1)[1]
    return None


def _cookie_flags(header: str) -> set[str]:
    return {part.strip() for part in header.split(";") if "=" not in part}


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
    access_cookie = next(
        cookie for cookie in set_cookies if cookie.startswith(f"{ACCESS_COOKIE_NAME}=")
    )
    refresh_cookie = next(
        cookie for cookie in set_cookies if cookie.startswith(f"{REFRESH_COOKIE_NAME}=")
    )
    assert "Max-Age=420" in access_cookie
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


def test_user_response_does_not_invent_role_user():
    created = datetime.now(timezone.utc)
    profile = UserResponse(
        id=7,
        username="dev.lin",
        role="developer",
        is_active=True,
        created_at=created,
    )
    assert profile.role == "developer"
    with pytest.raises(ValidationError):
        UserResponse(
            id=7,
            username="dev.lin",
            is_active=True,
            created_at=created,
        )


def test_developer_me_keeps_role_after_refresh_and_is_not_cacheable(
    client: TestClient, db
):
    """A developer cookie must not hydrate as role=user after reload.

    /me is cookie-keyed. A cached regular-user 200, or UserBase's
    default role="user", is how testers saw developer chrome collapse
    to 工作臺 on F5 while the first landing still looked right.
    """
    make_user(db, username="dev.lin", role="developer")
    _login(client, "dev.lin")

    me = client.get("/api/auth/me")
    assert me.status_code == 200, me.text
    body = me.json()
    assert body["username"] == "dev.lin"
    assert body["role"] == "developer"
    cache_control = me.headers.get("cache-control", "").lower()
    assert "no-store" in cache_control
    assert "cookie" in me.headers.get("vary", "").lower()

    rotated = client.post(
        "/api/auth/refresh",
        headers={"X-CSRF-Token": client.cookies.get(CSRF_COOKIE_NAME)},
    )
    assert rotated.status_code == 200, rotated.text

    again = client.get("/api/auth/me")
    assert again.status_code == 200, again.text
    assert again.json()["username"] == "dev.lin"
    assert again.json()["role"] == "developer"


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


def test_clear_session_cookies_matches_secure_httponly_and_refresh_path():
    """Browsers keep a Secure refresh cookie if logout expires it without Secure."""
    response = Response()
    clear_session_cookies(response)
    headers = _set_cookie_headers(response)

    refresh = [
        header
        for header in headers
        if header.startswith(f"{REFRESH_COOKIE_NAME}=")
        and _cookie_attr(header, "Path") == REFRESH_COOKIE_PATH
        and "Secure" in _cookie_flags(header)
        and "HttpOnly" in _cookie_flags(header)
    ]
    access = [
        header
        for header in headers
        if header.startswith(f"{ACCESS_COOKIE_NAME}=")
        and _cookie_attr(header, "Path") == "/"
        and "Secure" in _cookie_flags(header)
        and "HttpOnly" in _cookie_flags(header)
    ]
    csrf = [
        header
        for header in headers
        if header.startswith(f"{CSRF_COOKIE_NAME}=")
        and _cookie_attr(header, "Path") == "/"
        and "Secure" in _cookie_flags(header)
    ]
    assert refresh, headers
    assert access, headers
    assert csrf, headers
    assert all("Max-Age=0" in header for header in refresh)
    assert all("Max-Age=0" in header for header in access)


def test_logout_clears_cookies_and_bumps_token_version(client: TestClient, db):
    user = make_user(db, username="dave")
    _login(client, "dave")
    prior_tv = user.token_version or 0

    resp = client.post(
        "/api/auth/logout",
        headers={"X-CSRF-Token": client.cookies.get(CSRF_COOKIE_NAME)},
    )
    assert resp.status_code == 200

    set_cookies = resp.headers.get_list("set-cookie")
    refresh_expired = [
        header
        for header in set_cookies
        if header.startswith(f"{REFRESH_COOKIE_NAME}=")
        and _cookie_attr(header, "Path") == REFRESH_COOKIE_PATH
    ]
    assert refresh_expired, set_cookies
    assert any("Max-Age=0" in header for header in refresh_expired)
    assert any("HttpOnly" in header for header in refresh_expired)

    # Post-logout the session cookie should not work any more.
    probe = client.get("/api/auth/me")
    # Either 401 (cookies cleared) or 401 (token_version mismatch) —
    # both surface as 401.
    assert probe.status_code == 401

    # A leftover refresh cookie used to resurrect the session on bare /login.
    assert client.post("/api/auth/refresh").status_code == 401

    db.refresh(user)
    assert user.token_version == prior_tv + 1


def test_logout_bumps_token_version_when_access_cookie_is_expired(
    client: TestClient, db
):
    user = make_user(db, username="stale-access")
    _login(client, "stale-access")
    prior_tv = user.token_version or 0
    expired = create_access_token(
        {
            "sub": str(user.id),
            "username": user.username,
            "role": user.role,
            "tv": user.token_version or 0,
        },
        lifetime_minutes=-1,
    )
    client.cookies.set(ACCESS_COOKIE_NAME, expired)

    resp = client.post(
        "/api/auth/logout",
        headers={"X-CSRF-Token": client.cookies.get(CSRF_COOKIE_NAME)},
    )
    assert resp.status_code == 200
    db.refresh(user)
    assert user.token_version == prior_tv + 1
    assert client.get("/api/auth/me").status_code == 401
    # Access leftover in the TestClient jar can trip CSRF (403). Either
    # way refresh must not mint a new session after the version bump.
    csrf = client.cookies.get(CSRF_COOKIE_NAME)
    headers = {"X-CSRF-Token": csrf} if csrf else {}
    refreshed = client.post("/api/auth/refresh", headers=headers)
    assert refreshed.status_code != 200


def test_refresh_logout_revokes_leftover_cookies_after_proxy_drop(
    client: TestClient, db
):
    """Leftover cookies must not authenticate after 登出.

    a76cbcbd only expired cookies on POST /logout. The refresh cookie is
    Path=/api/auth/refresh so that request never saw it, and a Vite proxy
    can drop the expire Set-Cookie headers. Restoring the pre-logout
    cookies simulates both. POST /api/auth/refresh/logout is the path
    that still receives the refresh cookie and must bump token_version
    so GET /me and POST /refresh stay 401.
    """
    user = make_user(db, username="leftover")
    _login(client, "leftover")
    access = client.cookies.get(ACCESS_COOKIE_NAME)
    refresh = client.cookies.get(REFRESH_COOKIE_NAME)
    assert access and refresh
    prior_tv = user.token_version or 0

    resp = client.post("/api/auth/refresh/logout")
    assert resp.status_code == 200, resp.text

    client.cookies.set(ACCESS_COOKIE_NAME, access, path="/")
    assert client.get("/api/auth/me").status_code == 401

    # Shell remount path: leftover refresh, no access. CSRF is skipped
    # when the access cookie is gone — this must still be 401, not a
    # newly minted session.
    client.cookies.clear()
    client.cookies.set(REFRESH_COOKIE_NAME, refresh, path="/api/auth/refresh")
    assert client.post("/api/auth/refresh").status_code == 401
    db.refresh(user)
    assert user.token_version == prior_tv + 1


def test_logout_without_csrf_still_revokes(client: TestClient, db):
    """A dropped csrf cookie must not leave a live access JWT."""
    user = make_user(db, username="no-csrf-logout")
    _login(client, "no-csrf-logout")
    access = client.cookies.get(ACCESS_COOKIE_NAME)
    prior_tv = user.token_version or 0

    resp = client.post("/api/auth/logout")
    assert resp.status_code == 200, resp.text
    client.cookies.set(ACCESS_COOKIE_NAME, access, path="/")
    assert client.get("/api/auth/me").status_code == 401
    db.refresh(user)
    assert user.token_version == prior_tv + 1


def test_csrf_required_on_mutating_cookie_request(client: TestClient, db):
    make_user(db, username="erin")
    _login(client, "erin")

    # Missing X-CSRF-Token → 403. Logout is exempt; rotation is not.
    resp = client.post("/api/auth/refresh")
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
