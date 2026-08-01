"""OIDC id_token alg must be allowlisted in *this* codebase (MEDIUM finding).

The docstring of ``_verify_id_token`` claims an algorithm allowlist; historically
only ``none`` was rejected and the rest was left to python-jose. This test pins
the explicit allowlist and proves rejection happens *before* ``jose_jwt.decode``.
"""

from __future__ import annotations

import base64
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services import external_auth_service as eas


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _raw_jwt(*, alg: str) -> str:
    header = _b64url(
        json.dumps({"alg": alg, "kid": "k1"}, separators=(",", ":")).encode()
    )
    payload = _b64url(json.dumps({"sub": "u1"}, separators=(",", ":")).encode())
    return f"{header}.{payload}.fakesig"


@pytest.mark.asyncio
async def test_verify_id_token_rejects_alg_outside_allowlist_before_decode():
    """Kill: remove the OIDC id_token algorithm allowlist check.

    ``PS256`` is outside the pinned set. Without the allowlist, the function
    proceeds to JWKS + ``jose_jwt.decode`` (mocked here to succeed), so this
    assertion goes red.
    """
    provider = MagicMock()
    provider.oidc_issuer_url = "https://idp.example"
    provider.oidc_client_id = "anila-client"
    client = AsyncMock()
    token = _raw_jwt(alg="PS256")

    with patch.object(eas, "_fetch_jwks", new_callable=AsyncMock) as fetch:
        fetch.return_value = [{"kid": "k1", "kty": "RSA", "alg": "PS256"}]
        with patch.object(eas, "_select_jwk", return_value={"kid": "k1"}):
            with patch.object(
                eas.jose_jwt,
                "decode",
                return_value={"sub": "u1", "nonce": "n-expected", "aud": "anila-client"},
            ) as decode:
                with pytest.raises(ValueError, match="允許清單"):
                    await eas._verify_id_token(
                        client,
                        provider,
                        {"jwks_uri": "https://idp.example/jwks"},
                        token,
                        expected_nonce="n-expected",
                    )
                decode.assert_not_called()
