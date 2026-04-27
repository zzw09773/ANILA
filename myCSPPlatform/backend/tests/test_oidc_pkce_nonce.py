"""Sprint 6 X / A6: PKCE / nonce / state binding tests.

涵蓋：

1. ``_generate_pkce_pair``：verifier / challenge 應符合 RFC 7636 length
   與 base64url（無 padding）格式。
2. ``issue_external_state`` + ``decode_external_state``：nonce / pkce
   round-trip；nonce 不一致時 callback 應拒絕（透過 authenticate_oidc_code
   行為驗證 — 此處只驗 state binding 完整性）。
3. ``build_oidc_authorization_url``：output URL 應包含 ``code_challenge``,
   ``code_challenge_method=S256``, ``nonce``, ``state``，且 state JWT 解
   開後應含對應 PKCE verifier 與 nonce（且 SHA256(verifier) base64url ==
   challenge）。
"""
from __future__ import annotations

import base64
import hashlib
from urllib.parse import parse_qs, urlparse

import pytest

from app.services.external_auth_service import (
    _generate_pkce_pair,
    decode_external_state,
    issue_external_state,
)


def test_pkce_pair_format():
    verifier, challenge = _generate_pkce_pair()
    # RFC 7636 §4.1：verifier 43–128 字 [A-Z][a-z][0-9]-._~
    assert 43 <= len(verifier) <= 128
    # challenge 是 SHA256(verifier) base64url 無 padding；長度恰好 43
    assert len(challenge) == 43
    assert "=" not in challenge
    expected = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()
    ).rstrip(b"=").decode("ascii")
    assert challenge == expected


def test_state_round_trip_carries_nonce_and_pkce(monkeypatch):
    """issue_external_state → decode_external_state 應保留 nonce + pkce。"""
    # 用一個假的 provider object（只需要 id 屬性）
    class _StubProvider:
        id = 7

    state = issue_external_state(
        _StubProvider(),
        next_path="/",
        nonce="test-nonce-abc",
        code_verifier="test-verifier-xyz",
    )
    decoded = decode_external_state(state)
    assert decoded["provider_id"] == 7
    assert decoded["nonce"] == "test-nonce-abc"
    assert decoded["pkce"] == "test-verifier-xyz"
    assert decoded["next_path"] == "/"


def test_authorization_url_contains_pkce_and_nonce(monkeypatch):
    """build_oidc_authorization_url 必須帶 PKCE + nonce + state。"""
    import asyncio

    from app.services.external_auth_service import build_oidc_authorization_url

    class _StubProvider:
        id = 1
        oidc_client_id = "anila-test"
        oidc_scopes = "openid email"
        oidc_authorization_endpoint = "https://idp.example.com/authorize"
        oidc_token_endpoint = "https://idp.example.com/token"
        oidc_userinfo_endpoint = "https://idp.example.com/userinfo"
        oidc_issuer_url = "https://idp.example.com"

    # Patch _resolve_oidc_metadata so it doesn't call the network.
    async def _fake_metadata(provider):
        return {
            "authorization_endpoint": provider.oidc_authorization_endpoint,
            "token_endpoint": provider.oidc_token_endpoint,
            "userinfo_endpoint": provider.oidc_userinfo_endpoint,
            "issuer": provider.oidc_issuer_url,
            "jwks_uri": "https://idp.example.com/.well-known/jwks.json",
        }

    monkeypatch.setattr(
        "app.services.external_auth_service._resolve_oidc_metadata",
        _fake_metadata,
    )

    url = asyncio.run(
        build_oidc_authorization_url(_StubProvider(), next_path="/dashboard")
    )
    parsed = urlparse(url)
    qs = {k: v[0] for k, v in parse_qs(parsed.query).items()}

    assert qs["response_type"] == "code"
    assert qs["client_id"] == "anila-test"
    assert qs["code_challenge_method"] == "S256"
    assert "code_challenge" in qs and len(qs["code_challenge"]) == 43
    assert "nonce" in qs and len(qs["nonce"]) >= 16
    assert "state" in qs

    # state 拆開後應該夾帶對應的 verifier，且 verifier 的 SHA256 base64url ==
    # challenge — 證明 PKCE pair 是同一組。
    state_payload = decode_external_state(qs["state"])
    verifier = state_payload["pkce"]
    expected_challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()
    ).rstrip(b"=").decode("ascii")
    assert expected_challenge == qs["code_challenge"]
    assert state_payload["nonce"] == qs["nonce"]
    assert state_payload["next_path"] == "/dashboard"
