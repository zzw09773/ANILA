"""Gate 2 G16 scoped JWT revocation boundary tests."""
from __future__ import annotations

from app.services.token_revocation_service import revoke_jti, revoke_sid
from app.utils.security import decode_token
from tests.conftest import make_user


def _login(client, username: str) -> dict:
    response = client.post(
        "/api/auth/login",
        json={"username": username, "password": "password"},
    )
    assert response.status_code == 200, response.text
    client.cookies.clear()
    return response.json()


def _me(client, token: str):
    return client.get(
        "/api/auth/me",
        headers={"Authorization": f"Bearer {token}"},
    )


def test_specific_access_jti_revocation_is_db_backed_and_session_isolated(client, db):
    user = make_user(db, username="gate2_jti_revoke")
    first = _login(client, user.username)
    second = _login(client, user.username)
    first_access = decode_token(first["access_token"])
    first_refresh = decode_token(first["refresh_token"])
    assert first_access is not None and first_refresh is not None
    assert first_access["jti"] != first_refresh["jti"]

    revoke_jti(
        db,
        user_id=user.id,
        jti=first_access["jti"],
        token_type="access",
        reason="focused_test",
        commit=True,
    )

    assert _me(client, first["access_token"]).status_code == 401
    assert _me(client, second["access_token"]).status_code == 200


def test_sid_revocation_invalidates_family_but_not_another_session(client, db):
    user = make_user(db, username="gate2_sid_revoke")
    first = _login(client, user.username)
    second = _login(client, user.username)
    first_claims = decode_token(first["access_token"])
    assert first_claims is not None

    revoke_sid(
        db,
        user_id=user.id,
        sid=first_claims["sid"],
        reason="focused_test",
        commit=True,
    )

    assert _me(client, first["access_token"]).status_code == 401
    assert _me(client, second["access_token"]).status_code == 200

    client.cookies.clear()
    refresh = client.post(
        "/api/auth/refresh",
        json={"refresh_token": first["refresh_token"]},
    )
    assert refresh.status_code == 401


def test_revocation_rows_store_only_identifier_digests(client, db):
    from app.models.token_revocation import TokenRevocation

    user = make_user(db, username="gate2_no_raw_identifier")
    pair = _login(client, user.username)
    claims = decode_token(pair["access_token"])
    assert claims is not None

    row = revoke_jti(
        db,
        user_id=user.id,
        jti=claims["jti"],
        token_type="access",
        reason="no_token_leak",
        commit=True,
    )
    db.refresh(row)
    assert row.scope == "jti"
    assert row.token_jti_hash and len(row.token_jti_hash) == 64
    assert claims["jti"] not in repr(row.__dict__)
    assert db.query(TokenRevocation).filter_by(scope="jti").count() == 1
