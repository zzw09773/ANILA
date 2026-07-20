"""Focused CSP router-context/v1 signing contract tests."""

from __future__ import annotations

import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization
from jose import jwt

from app.config import settings
from app.services.proxy.headers import build_router_model_gateway_headers
from app.services.router_context_token import (
    ROUTER_CONTEXT_HEADER_TYP,
    ROUTER_CONTEXT_TOKEN_TYPE,
    canonical_router_body_sha256,
)
from app.services import router_context_token


@pytest.fixture
def signing_key(monkeypatch):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    monkeypatch.setattr(router_context_token, "get_private_key", lambda: private)
    monkeypatch.setattr(settings, "JWT_KID", "router-test-kid")
    monkeypatch.setattr(settings, "JWT_ISSUER", "https://csp.test/issuer")
    return key


@pytest.fixture
def context() -> dict:
    return {
        "task_id": 11,
        "run_id": 12,
        "source_snapshot_id": 13,
        "trace_id": "trace-abc",
        "invocation_id": "invocation-abc",
        "session_id": "session-abc",
        "task_type": "query",
        "classification_level": "無機密",
        "scopes": ("agent:invoke",),
        "required_capabilities": ("retrieval",),
        "auth_assurance": {
            "sid": "auth-session",
            "amr": ("pwd",),
            "acr": "aal2",
            "auth_time": "2026-07-15T00:00:00+00:00",
            "break_glass": False,
        },
        "owner_id": 42,
    }


def test_router_builder_emits_one_body_bound_token(
    signing_key, context: dict
) -> None:
    body = {
        "model": "anila-router",
        "messages": [{"role": "user", "content": "hello"}],
        "anila_session_id": "session-abc",
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    headers = build_router_model_gateway_headers(
        "990000002",
        router_caller_user_id=42,
        router_context=context,
        request_body=body,
    )
    token = headers["X-ANILA-Router-Context"]
    header = jwt.get_unverified_header(token)
    claims = jwt.get_unverified_claims(token)
    assert header["typ"] == ROUTER_CONTEXT_HEADER_TYP
    assert header["kid"] == "router-test-kid"
    assert claims["type"] == ROUTER_CONTEXT_TOKEN_TYPE
    assert claims["sub"] == claims["caller_user_id"] == claims["owner_id"] == "42"
    assert claims["session_id"] == body["anila_session_id"]
    assert claims["body_sha256"] == canonical_router_body_sha256(body)
    assert "X-ANILA-Caller-User-Id" not in headers
    assert "X-ANILA-Task-Id" not in headers
    assert "X-ANILA-Auth-Assurance" not in headers


def test_router_token_body_binding_rejects_session_or_body_change(
    signing_key, context: dict
) -> None:
    body = {"model": "anila-router", "anila_session_id": "session-abc"}
    headers = build_router_model_gateway_headers(
        None,
        router_caller_user_id=42,
        router_context=context,
        request_body=body,
    )
    claims = jwt.get_unverified_claims(headers["X-ANILA-Router-Context"])
    assert claims["body_sha256"] != canonical_router_body_sha256(
        {**body, "anila_session_id": "other-session"}
    )


def test_router_token_claim_schema_has_short_ttl_and_jti(
    signing_key, context: dict
) -> None:
    body = {"model": "anila-router", "anila_session_id": "session-abc"}
    headers = build_router_model_gateway_headers(
        None,
        router_caller_user_id=42,
        router_context=context,
        request_body=body,
    )
    claims = jwt.get_unverified_claims(headers["X-ANILA-Router-Context"])
    assert claims["exp"] - claims["iat"] <= 60
    assert isinstance(claims["jti"], str) and claims["jti"]
    assert claims["iss"] == settings.JWT_ISSUER
    assert claims["aud"] == "anila-router"
