"""P2.1 W1: per-dispatch signed identity JWT (CSP signing half).

Acceptance locks:
(a) minted token verifies against the key published by ``_serialize_jwks``
(b) exp − iat == 5 minutes on the production default path
(c) tampering any of the three identity claims fails verification
(d) expired token fails verification
(e) ``build_agent_headers`` emits Bearer + correlation headers only
"""
from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicNumbers
from jose import JWTError, jwt

from app.api.jwks import _serialize_jwks
from app.config import settings
from app.services.proxy.dispatch_token import (
    DISPATCH_TOKEN_AUDIENCE,
    DISPATCH_TOKEN_ISSUER,
    DISPATCH_TOKEN_TTL_MINUTES,
    build_dispatch_claims,
    issue_dispatch_token,
)
from app.services.proxy.headers import build_agent_headers
from app.utils.security import ALGORITHM, get_private_key


def _public_pem_from_published_jwks() -> bytes:
    """Rebuild the verify key from the app's own JWKS serialization.

    Must NOT copy the PEM from disk — the published JWKS is the contract.
    """
    doc = _serialize_jwks()
    key = doc["keys"][0]
    pad_n = "=" * (-len(key["n"]) % 4)
    pad_e = "=" * (-len(key["e"]) % 4)
    n = int.from_bytes(base64.urlsafe_b64decode(key["n"] + pad_n), "big")
    e = int.from_bytes(base64.urlsafe_b64decode(key["e"] + pad_e), "big")
    pub = RSAPublicNumbers(e=e, n=n).public_key()
    return pub.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )


def _verify(token: str) -> dict:
    return jwt.decode(
        token,
        _public_pem_from_published_jwks(),
        algorithms=[ALGORITHM],
        audience=DISPATCH_TOKEN_AUDIENCE,
        issuer=DISPATCH_TOKEN_ISSUER,
    )


def test_minted_token_verifies_against_published_jwks():
    token = issue_dispatch_token(user_id=7, department=3, agent_id=42)
    payload = _verify(token)
    assert payload["user_id"] == 7
    assert payload["department"] == 3
    assert payload["agent_id"] == 42
    assert payload["iss"] == DISPATCH_TOKEN_ISSUER
    assert payload["aud"] == DISPATCH_TOKEN_AUDIENCE
    assert payload["sub"] == "7"
    header = jwt.get_unverified_header(token)
    assert header["alg"] == "RS256"
    # kid must equal the kid the app publishes in JWKS — not merely present.
    published_kid = _serialize_jwks()["keys"][0]["kid"]
    assert header["kid"] == published_kid
    assert published_kid == settings.JWT_KID


def test_ttl_is_exactly_five_minutes():
    """Production default path: identity args only; no time overrides."""
    assert DISPATCH_TOKEN_TTL_MINUTES == 5
    token = issue_dispatch_token(user_id=1, department=None, agent_id=9)
    payload = jwt.get_unverified_claims(token)
    assert payload["exp"] - payload["iat"] == 5 * 60


@pytest.mark.parametrize(
    "claim,new_value",
    [("user_id", 999), ("department", 999), ("agent_id", 999)],
)
def test_tampered_identity_claim_fails_verification(claim, new_value):
    """Swap one identity claim but keep the original signature → verify fails."""
    import json

    token = issue_dispatch_token(user_id=7, department=3, agent_id=42)
    header_b64, payload_b64, sig_b64 = token.split(".")
    pad = "=" * (-len(payload_b64) % 4)
    claims = json.loads(base64.urlsafe_b64decode(payload_b64 + pad))
    assert claims[claim] != new_value
    claims[claim] = new_value
    new_payload = (
        base64.urlsafe_b64encode(json.dumps(claims, separators=(",", ":")).encode())
        .rstrip(b"=")
        .decode()
    )
    tampered = f"{header_b64}.{new_payload}.{sig_b64}"
    with pytest.raises(JWTError):
        _verify(tampered)


def test_expired_token_fails_verification():
    """Expired fixture via build_dispatch_claims — not issue_ overrides."""
    past = datetime.now(timezone.utc) - timedelta(minutes=10)
    claims = build_dispatch_claims(
        user_id=1,
        department=2,
        agent_id=3,
        issued_at=past,
        expires_at=past + timedelta(minutes=DISPATCH_TOKEN_TTL_MINUTES),
    )
    token = jwt.encode(
        claims,
        get_private_key(),
        algorithm=ALGORITHM,
        headers={"kid": settings.JWT_KID, "typ": "JWT"},
    )
    with pytest.raises(JWTError):
        _verify(token)


def test_build_agent_headers_contract():
    h = build_agent_headers(
        user_id=11,
        department=22,
        agent_id=33,
        task_id="task-1",
        trace_id="trace-1",
    )
    assert h["Authorization"].startswith("Bearer ")
    assert h["X-ANILA-Task-Id"] == "task-1"
    assert h["X-ANILA-Trace-Id"] == "trace-1"
    assert "X-CSP-Service-Token" not in h
    assert "X-ANILA-User-Id" not in h
    assert "X-ANILA-User-Email" not in h
    assert "X-ANILA-User-Groups" not in h
    # Bearer payload must itself verify against published JWKS.
    bearer = h["Authorization"].removeprefix("Bearer ").strip()
    payload = _verify(bearer)
    assert payload["user_id"] == 11
    assert payload["department"] == 22
    assert payload["agent_id"] == 33


def test_issue_dispatch_token_has_no_time_override_params():
    """F4: production mint API must not accept issued_at/expires_at."""
    import inspect

    params = inspect.signature(issue_dispatch_token).parameters
    assert "issued_at" not in params
    assert "expires_at" not in params
