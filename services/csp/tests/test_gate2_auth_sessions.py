"""Gate 2 G14/G16/G18 authentication and revocation contracts.

The tests intentionally exercise the public HTTP boundary wherever possible:
cookies and SDK bearer tokens must keep working while refresh replay and
session-scoped logout fail closed.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from jose import jwt

from app.config import settings
from app.models.auth_session import AuthSession
from app.services.auth_service import create_tokens
from app.utils.security import decode_token, get_private_key
from tests.conftest import make_user


_REQUIRED_SESSION_CLAIMS = {
    "jti",
    "sid",
    "iat",
    "iss",
    "aud",
    "amr",
    "acr",
    "auth_time",
    "break_glass",
}


def _login(client, username: str) -> dict:
    response = client.post(
        "/api/auth/login",
        json={"username": username, "password": "password"},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_login_pair_has_distinct_jti_common_sid_and_complete_assurance(client, db):
    make_user(db, username="gate2_claims")

    pair = _login(client, "gate2_claims")
    access = decode_token(pair["access_token"])
    refresh = decode_token(pair["refresh_token"])

    assert access is not None and refresh is not None
    assert _REQUIRED_SESSION_CLAIMS <= access.keys()
    assert _REQUIRED_SESSION_CLAIMS <= refresh.keys()
    assert access["jti"] != refresh["jti"]
    assert access["sid"] == refresh["sid"]
    assert access["amr"] == refresh["amr"] == ["pwd"]
    assert access["acr"] == refresh["acr"] == "urn:anila:acr:password"
    assert access["auth_time"] == refresh["auth_time"]
    assert access["break_glass"] is refresh["break_glass"] is False
    assert access["iss"] == refresh["iss"] == settings.JWT_ISSUER
    assert access["aud"] == refresh["aud"] == settings.JWT_AUDIENCE


def test_decode_rejects_wrong_issuer_and_audience(client, db):
    make_user(db, username="gate2_wrong_trust")
    valid = decode_token(_login(client, "gate2_wrong_trust")["access_token"])
    assert valid is not None

    for field, bad_value in (("iss", "https://attacker.invalid"), ("aud", "other-service")):
        forged_payload = dict(valid)
        forged_payload[field] = bad_value
        forged_payload["exp"] = datetime.now(timezone.utc) + timedelta(minutes=5)
        forged = jwt.encode(
            forged_payload,
            get_private_key(),
            algorithm="RS256",
            headers={"kid": settings.JWT_KID, "typ": "JWT"},
        )
        assert decode_token(forged) is None, field


def test_decode_rejects_missing_required_registered_or_session_claim(client, db):
    make_user(db, username="gate2_missing_claim")
    valid = decode_token(_login(client, "gate2_missing_claim")["access_token"])
    assert valid is not None

    for field in (
        "exp",
        "iat",
        "iss",
        "aud",
        "jti",
        "sid",
        "amr",
        "acr",
        "auth_time",
        "break_glass",
    ):
        forged_payload = dict(valid)
        forged_payload.pop(field)
        forged = jwt.encode(
            forged_payload,
            get_private_key(),
            algorithm="RS256",
            headers={"kid": settings.JWT_KID, "typ": "JWT"},
        )
        assert decode_token(forged) is None, field


def test_decode_rejects_contradictory_or_ill_typed_assurance(client, db):
    make_user(db, username="gate2_bad_assurance")
    valid = decode_token(_login(client, "gate2_bad_assurance")["access_token"])
    assert valid is not None

    mutations = (
        {"amr": ["sc"], "acr": "urn:anila:acr:password"},
        {"amr": "sc"},
        {"amr": ["unknown"]},
        {
            "break_glass": True,
            "amr": ["sc"],
            "acr": "urn:anila:acr:smart-card",
        },
        {"auth_time": valid["iat"] + 1},
        {
            "iat": valid["iat"] + settings.JWT_LEEWAY_SECONDS + 120,
            "auth_time": valid["iat"] + settings.JWT_LEEWAY_SECONDS + 120,
        },
    )
    for mutation in mutations:
        forged_payload = {**valid, **mutation}
        forged = jwt.encode(
            forged_payload,
            get_private_key(),
            algorithm="RS256",
            headers={"kid": settings.JWT_KID, "typ": "JWT"},
        )
        assert decode_token(forged) is None, mutation


def test_refresh_rotates_once_and_preserves_session_assurance(client, db):
    make_user(db, username="gate2_rotate")
    original = _login(client, "gate2_rotate")
    old_refresh = original["refresh_token"]
    old_claims = decode_token(old_refresh)
    assert old_claims is not None

    client.cookies.clear()
    first = client.post("/api/auth/refresh", json={"refresh_token": old_refresh})
    assert first.status_code == 200, first.text
    rotated = first.json()
    access_claims = decode_token(rotated["access_token"])
    refresh_claims = decode_token(rotated["refresh_token"])
    assert access_claims is not None and refresh_claims is not None

    assert access_claims["sid"] == refresh_claims["sid"] == old_claims["sid"]
    for field in ("amr", "acr", "auth_time", "break_glass"):
        assert access_claims[field] == refresh_claims[field] == old_claims[field]
    assert old_claims["jti"] not in {
        access_claims["jti"],
        refresh_claims["jti"],
    }
    assert access_claims["jti"] != refresh_claims["jti"]

    client.cookies.clear()
    replay = client.post("/api/auth/refresh", json={"refresh_token": old_refresh})
    assert replay.status_code == 401, replay.text

    from app.models.audit_log import AuditLog

    event = db.query(AuditLog).filter_by(action="auth.refresh_reuse").one()
    metadata = json.loads(event.metadata_json)
    assert old_claims["jti"] not in event.metadata_json
    assert len(metadata["token_jti_hash"]) == 64
    assert len(metadata["session_id_hash"]) == 64

    # Reuse is an incident signal: the whole refresh family/session is revoked.
    probe = client.get("/api/auth/me", headers=_bearer(rotated["access_token"]))
    assert probe.status_code == 401, probe.text


def test_logout_revokes_only_the_presented_session(client, db):
    user = make_user(db, username="gate2_two_sessions")
    first = _login(client, user.username)
    client.cookies.clear()
    second = _login(client, user.username)
    first_claims = decode_token(first["access_token"])
    second_claims = decode_token(second["access_token"])
    assert first_claims is not None and second_claims is not None
    assert first_claims["sid"] != second_claims["sid"]

    logout = client.post(
        "/api/auth/logout",
        headers=_bearer(first["access_token"]),
    )
    assert logout.status_code == 200, logout.text

    assert client.get(
        "/api/auth/me", headers=_bearer(first["access_token"])
    ).status_code == 401
    still_active = client.get(
        "/api/auth/me", headers=_bearer(second["access_token"])
    )
    assert still_active.status_code == 200, still_active.text

    db.refresh(user)
    assert user.token_version == 0


def test_named_break_glass_password_session_has_explicit_assurance(
    client, db, monkeypatch,
):
    user = make_user(db, username="gate2_breakglass", role="owner")
    monkeypatch.setattr(settings, "REQUIRE_CARD_LOGIN_ONLY", True)
    monkeypatch.setattr(
        settings, "ANILA_DEPLOYMENT_PROFILE", "prod-intranet-card-breakglass"
    )
    monkeypatch.setenv("ANILA_BREAK_GLASS_OWNER", "gate2-owner")
    monkeypatch.setenv("ANILA_BREAK_GLASS_TICKET", "INC-GATE2-001")
    monkeypatch.setenv(
        "ANILA_BREAK_GLASS_EXPIRES_AT",
        (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
    )

    claims = decode_token(_login(client, user.username)["access_token"])
    assert claims is not None
    assert claims["amr"] == ["pwd"]
    assert claims["acr"] == "urn:anila:acr:break-glass"
    assert claims["break_glass"] is True


def test_oidc_session_assurance_is_not_upgraded_to_card(db):
    from app.services.auth_service import create_tokens

    user = make_user(db, username="gate2_oidc")
    claims = decode_token(create_tokens(user, amr=("oidc",))["access_token"])
    assert claims is not None
    assert claims["amr"] == ["oidc"]
    assert claims["acr"] == "urn:anila:acr:federated"
    assert claims["break_glass"] is False


def test_issuer_rejects_unknown_or_contradictory_assurance(db):
    user = make_user(db, username="gate2_bad_issuer_assurance")
    with pytest.raises(ValueError, match="unsupported authentication"):
        create_tokens(user, amr=("attacker-defined",))
    with pytest.raises(ValueError, match="ACR contradicts"):
        create_tokens(
            user,
            amr=("pwd",),
            acr="urn:anila:acr:smart-card",
        )
    with pytest.raises(ValueError, match="password-only"):
        create_tokens(user, amr=("sc",), break_glass=True)


@pytest.mark.parametrize(
    "mutation",
    [
        {
            "amr": ("oidc",),
            "acr": "urn:anila:acr:federated",
            "auth_time_delta": 0,
            "break_glass": False,
        },
        {
            "amr": ("pwd",),
            "acr": "urn:anila:acr:password",
            "auth_time_delta": -1,
            "break_glass": False,
        },
        {
            "amr": ("pwd",),
            "acr": "urn:anila:acr:break-glass",
            "auth_time_delta": 0,
            "break_glass": True,
        },
    ],
    ids=("amr-acr", "auth-time", "break-glass"),
)
def test_existing_sid_cannot_be_reissued_with_different_assurance(db, mutation):
    user = make_user(db, username=f"gate2_sid_drift_{mutation['acr'].rsplit(':', 1)[-1]}")
    original = create_tokens(user, db=db, amr=("pwd",))
    db.commit()
    claims = decode_token(original["access_token"])
    assert claims is not None

    with pytest.raises(ValueError, match="cannot change assurance"):
        create_tokens(
            user,
            db=db,
            sid=claims["sid"],
            amr=mutation["amr"],
            acr=mutation["acr"],
            auth_time=claims["auth_time"] + mutation["auth_time_delta"],
            break_glass=mutation["break_glass"],
        )

    assert db.query(AuthSession).filter_by(sid=claims["sid"]).count() == 1


@pytest.mark.parametrize(
    "assurance_mutation",
    [
        {
            "amr": ["oidc"],
            "acr": "urn:anila:acr:federated",
        },
        {"auth_time_delta": -1},
        {
            "amr": ["pwd"],
            "acr": "urn:anila:acr:break-glass",
            "break_glass": True,
        },
    ],
    ids=("amr-acr", "auth-time", "break-glass"),
)
def test_signed_token_assurance_must_match_durable_session(
    client,
    db,
    assurance_mutation,
):
    user = make_user(
        db,
        username=f"gate2_token_drift_{assurance_mutation.get('acr', 'time').rsplit(':', 1)[-1]}",
    )
    valid = decode_token(_login(client, user.username)["access_token"])
    assert valid is not None

    forged_payload = dict(valid)
    auth_time_delta = assurance_mutation.pop("auth_time_delta", 0)
    forged_payload.update(assurance_mutation)
    forged_payload["auth_time"] += auth_time_delta
    forged = jwt.encode(
        forged_payload,
        get_private_key(),
        algorithm="RS256",
        headers={"kid": settings.JWT_KID, "typ": "JWT"},
    )
    assert decode_token(forged) is not None

    response = client.get("/api/auth/me", headers=_bearer(forged))

    assert response.status_code == 401, response.text
    assert "保證狀態不一致" in response.json()["detail"]


def test_malformed_durable_assurance_fails_closed_at_request_boundary(client, db):
    user = make_user(db, username="gate2_corrupt_session")
    pair = _login(client, user.username)
    claims = decode_token(pair["access_token"])
    assert claims is not None
    session = db.get(AuthSession, claims["sid"])
    assert session is not None
    session.amr_json = '{"not":"an-amr-list"}'
    db.commit()

    response = client.get(
        "/api/auth/me",
        headers=_bearer(pair["access_token"]),
    )

    assert response.status_code == 401, response.text
    assert "保證狀態無效" in response.json()["detail"]
