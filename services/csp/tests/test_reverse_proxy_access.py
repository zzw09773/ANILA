"""Regression coverage for nginx ``auth_request`` policy endpoints.

The reverse proxy uses these endpoints as the first authentication and
authorization boundary for retained intranet developer tools.  The tools keep
their own login as a second boundary; this endpoint only decides whether the
request may reach them at all.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.config import settings
from app.middleware.cookies import ACCESS_COOKIE_NAME
from app.services.auth_service import create_tokens
from app.utils.security import create_access_token, create_refresh_token, decode_token
from tests.conftest import make_user


def _claims_for(user, *, token_role: str | None = None, amr=None, include_amr=True):
    claims = {
        "sub": str(user.id),
        "username": user.username,
        "role": token_role if token_role is not None else user.role,
        "tv": user.token_version,
    }
    if include_amr:
        claims["amr"] = amr
    return claims


def _set_session_cookie(client, user, *, amr: list[str] | None = None) -> None:
    token = create_access_token(
        {
            "sub": str(user.id),
            "username": user.username,
            "role": user.role,
            "tv": user.token_version,
            **({"amr": amr} if amr is not None else {}),
        }
    )
    client.cookies.set(ACCESS_COOKIE_NAME, token)


def _activate_break_glass(monkeypatch, *, minutes: int = 5) -> None:
    monkeypatch.setattr(settings, "REQUIRE_CARD_LOGIN_ONLY", True)
    monkeypatch.setattr(
        settings,
        "ANILA_DEPLOYMENT_PROFILE",
        "prod-intranet-card-breakglass",
    )
    monkeypatch.setenv("ANILA_BREAK_GLASS_OWNER", "system-owner")
    monkeypatch.setenv("ANILA_BREAK_GLASS_TICKET", "INC-PROXY-ACCESS-001")
    monkeypatch.setenv(
        "ANILA_BREAK_GLASS_EXPIRES_AT",
        (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat(),
    )


def test_proxy_access_rejects_unauthenticated_request(client) -> None:
    response = client.get("/api/auth/proxy-access/card-session")

    assert response.status_code == 401


def test_proxy_access_allows_authenticated_user_for_private_assets(client, db) -> None:
    user = make_user(db, username="asset-user", role="user")
    _set_session_cookie(client, user, amr=["sc"])

    response = client.get("/api/auth/proxy-access/card-session")

    assert response.status_code == 204
    assert response.content == b""


def test_proxy_access_rejects_regular_user_for_developer_tools(client, db) -> None:
    user = make_user(db, username="regular-user", role="user")
    _set_session_cookie(client, user, amr=["sc"])

    response = client.get("/api/auth/proxy-access/card-developer")

    assert response.status_code == 403


@pytest.mark.parametrize("role", ["developer", "admin", "owner"])
def test_proxy_access_allows_developer_tier(client, db, role: str) -> None:
    user = make_user(db, username=f"{role}-user", role=role)
    _set_session_cookie(client, user, amr=["sc"])

    response = client.get("/api/auth/proxy-access/card-developer")

    assert response.status_code == 204
    assert response.content == b""


def test_proxy_access_rejects_developer_for_admin_tool(client, db) -> None:
    user = make_user(db, username="developer-not-admin", role="developer")
    _set_session_cookie(client, user, amr=["sc"])

    response = client.get("/api/auth/proxy-access/card-admin")

    assert response.status_code == 403


@pytest.mark.parametrize("role", ["admin", "owner"])
def test_proxy_access_allows_card_admin_tier(client, db, role: str) -> None:
    user = make_user(db, username=f"card-{role}", role=role)
    _set_session_cookie(client, user, amr=["sc"])

    response = client.get("/api/auth/proxy-access/card-admin")

    assert response.status_code == 204
    assert response.content == b""


@pytest.mark.parametrize("amr", [None, [], ["pwd"], ["oidc"]])
def test_proxy_access_requires_card_session_for_developer_tools(
    client, db, amr: list[str] | None
) -> None:
    user = make_user(db, username=f"non-card-{amr}", role="developer")
    _set_session_cookie(client, user, amr=amr)

    response = client.get("/api/auth/proxy-access/card-developer")

    assert response.status_code == 403


def test_proxy_access_does_not_accept_unknown_scope(client, db) -> None:
    user = make_user(db, username="unknown-scope-user", role="owner")
    _set_session_cookie(client, user)

    response = client.get("/api/auth/proxy-access/anything")

    assert response.status_code == 404


def test_refresh_preserves_smart_card_authentication_method(client, db) -> None:
    user = make_user(db, username="refresh-card", role="developer")
    refresh_token = create_tokens(user, amr=("sc",))["refresh_token"]

    response = client.post(
        "/api/auth/refresh",
        json={"refresh_token": refresh_token},
    )

    assert response.status_code == 200
    assert decode_token(response.json()["access_token"])["amr"] == ["sc"]
    assert decode_token(response.json()["refresh_token"])["amr"] == ["sc"]


def test_refresh_does_not_upgrade_legacy_token_to_card_session(client, db) -> None:
    user = make_user(db, username="refresh-legacy", role="developer")
    legacy_refresh = create_refresh_token(
        {
            "sub": str(user.id),
            "username": user.username,
            "role": user.role,
            "tv": user.token_version,
        }
    )

    response = client.post(
        "/api/auth/refresh",
        json={"refresh_token": legacy_refresh},
    )

    assert response.status_code == 200
    assert decode_token(response.json()["access_token"])["amr"] == []


@pytest.mark.parametrize(
    ("db_role", "token_role", "amr"),
    [
        pytest.param("developer", "developer", ["sc"], id="smart-card"),
    ],
)
def test_card_only_access_accepts_assured_sessions(
    client, db, monkeypatch, db_role: str, token_role: str, amr: list[str]
) -> None:
    monkeypatch.setattr(settings, "REQUIRE_CARD_LOGIN_ONLY", True)
    user = make_user(db, username=f"formal-access-{db_role}", role=db_role)
    token = create_access_token(
        _claims_for(user, token_role=token_role, amr=amr)
    )

    response = client.get(
        "/api/auth/me",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert response.json()["role"] == db_role


def test_card_only_access_accepts_owner_password_only_during_break_glass(
    client, db, monkeypatch
) -> None:
    _activate_break_glass(monkeypatch)
    user = make_user(db, username="break-glass-access-owner", role="owner")
    token = create_access_token(_claims_for(user, token_role="user", amr=["pwd"]))

    response = client.get(
        "/api/auth/me", headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 200
    assert response.json()["role"] == "owner"


@pytest.mark.parametrize(
    ("db_role", "token_role", "amr", "include_amr"),
    [
        pytest.param("developer", "owner", ["pwd"], True, id="spoofed-owner-role"),
        pytest.param("developer", "developer", ["oidc"], True, id="oidc"),
        pytest.param("developer", "developer", None, False, id="legacy"),
        pytest.param("owner", "owner", ["oidc"], True, id="owner-oidc"),
        pytest.param("owner", "owner", ["pwd"], True, id="owner-pwd-normal-profile"),
    ],
)
def test_card_only_access_rejects_unassured_sessions(
    client,
    db,
    monkeypatch,
    db_role: str,
    token_role: str,
    amr: list[str] | None,
    include_amr: bool,
) -> None:
    monkeypatch.setattr(settings, "REQUIRE_CARD_LOGIN_ONLY", True)
    user = make_user(
        db,
        username=f"formal-reject-{db_role}-{token_role}-{amr}",
        role=db_role,
    )
    token = create_access_token(
        _claims_for(
            user,
            token_role=token_role,
            amr=amr,
            include_amr=include_amr,
        )
    )

    response = client.get(
        "/api/auth/me",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 401


@pytest.mark.parametrize(
    ("db_role", "token_role", "amr"),
    [
        pytest.param("developer", "developer", ["sc"], id="smart-card"),
    ],
)
def test_card_only_refresh_accepts_assured_sessions(
    client, db, monkeypatch, db_role: str, token_role: str, amr: list[str]
) -> None:
    monkeypatch.setattr(settings, "REQUIRE_CARD_LOGIN_ONLY", True)
    user = make_user(db, username=f"formal-refresh-{db_role}", role=db_role)
    token = create_refresh_token(
        _claims_for(user, token_role=token_role, amr=amr)
    )

    response = client.post("/api/auth/refresh", json={"refresh_token": token})

    assert response.status_code == 200
    assert decode_token(response.json()["access_token"])["amr"] == amr


def test_card_only_refresh_accepts_owner_password_only_during_break_glass(
    client, db, monkeypatch
) -> None:
    _activate_break_glass(monkeypatch)
    user = make_user(db, username="break-glass-refresh-owner", role="owner")
    token = create_refresh_token(_claims_for(user, token_role="user", amr=["pwd"]))

    response = client.post("/api/auth/refresh", json={"refresh_token": token})

    assert response.status_code == 200
    assert decode_token(response.json()["access_token"])["amr"] == ["pwd"]


def test_break_glass_password_refresh_is_rejected_after_expiry(
    client, db, monkeypatch
) -> None:
    _activate_break_glass(monkeypatch, minutes=-1)
    user = make_user(db, username="expired-break-glass-owner", role="owner")
    token = create_refresh_token(_claims_for(user, amr=["pwd"]))

    response = client.post("/api/auth/refresh", json={"refresh_token": token})

    assert response.status_code == 401


@pytest.mark.parametrize(
    ("db_role", "amr", "include_amr"),
    [
        pytest.param("developer", ["pwd"], True, id="non-owner-password"),
        pytest.param("developer", ["oidc"], True, id="oidc"),
        pytest.param("developer", None, False, id="legacy"),
        pytest.param("owner", ["oidc"], True, id="owner-oidc"),
    ],
)
def test_card_only_refresh_rejects_unassured_sessions(
    client,
    db,
    monkeypatch,
    db_role: str,
    amr: list[str] | None,
    include_amr: bool,
) -> None:
    monkeypatch.setattr(settings, "REQUIRE_CARD_LOGIN_ONLY", True)
    user = make_user(db, username=f"formal-refresh-reject-{db_role}-{amr}", role=db_role)
    token = create_refresh_token(
        _claims_for(user, amr=amr, include_amr=include_amr)
    )

    response = client.post("/api/auth/refresh", json={"refresh_token": token})

    assert response.status_code == 401
