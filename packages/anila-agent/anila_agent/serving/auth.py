"""service-wrapper 入向認證：委派 anila-core 的 JWKS / 派工 JWT 驗章。

P2.1 契約（與 CSP ``dispatch_token`` 對齊）：
  Authorization: Bearer <RS256 JWT>   ← iss=anila-csp, aud=anila-agent
  claims: user_id / department / agent_id
  公鑰：``{CSP_BASE_URL}/.well-known/jwks.json``（``kid`` 選鑰）

本模組**不再手抄**驗章邏輯——密碼學與 JWKS 快取來自
``anila_core.api.middleware``（package 實作）。第三方離線複製請用
``anila_core/contrib/anila_verify.py``（單一檔、stdlib + cryptography）。

相關聯標頭（不參與身分、只做追蹤）仍可讀：
  X-ANILA-Task-Id / X-ANILA-Trace-Id
"""

from __future__ import annotations

import os
from typing import Any

from anila_core.api.middleware.dispatch_jwt import (
    DISPATCH_TOKEN_ALG,
    DISPATCH_TOKEN_AUDIENCE,
    DISPATCH_TOKEN_ISSUER,
    DispatchTokenError,
    extract_bearer,
    parse_unverified_header,
    verify_dispatch_jwt as _core_verify_dispatch_jwt,
)
from anila_core.api.middleware.jwks_client import (
    JwksClient,
    JwksError,
    JwksFetchError,
    JwksKeyNotFoundError,
    derive_jwks_url,
)
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey

__all__ = [
    "DISPATCH_TOKEN_ALG",
    "DISPATCH_TOKEN_AUDIENCE",
    "DISPATCH_TOKEN_ISSUER",
    "DispatchAuthError",
    "JwksClient",
    "configure_jwks_client",
    "derive_jwks_url",
    "extract_bearer",
    "get_jwks_client",
    "identity_from_claims",
    "reset_jwks_client_for_tests",
    "trusted_user_identity",
    "verify_dispatch_authorization",
    "verify_dispatch_jwt",
    "verify_service_token",
]


class DispatchAuthError(Exception):
    """派工 JWT 驗章失敗。"""


_client: JwksClient | None = None


def configure_jwks_client(
    *,
    csp_base_url: str | None = None,
    ca_file: str | None = None,
) -> JwksClient:
    """(Re)create the process-wide JWKS client from platform base URL + CA."""
    global _client
    base = csp_base_url if csp_base_url is not None else os.environ.get(
        "CSP_BASE_URL", ""
    )
    ca = ca_file if ca_file is not None else (
        (os.environ.get("ANILA_CA_FILE") or "").strip() or None
    )
    _client = JwksClient(derive_jwks_url(base or ""), ca_file=ca)
    return _client


def get_jwks_client(
    *,
    csp_base_url: str | None = None,
    ca_file: str | None = None,
) -> JwksClient:
    """Process-wide JWKS client; URL derived from ``CSP_BASE_URL``."""
    global _client
    if _client is None:
        return configure_jwks_client(csp_base_url=csp_base_url, ca_file=ca_file)
    return _client


def reset_jwks_client_for_tests() -> None:
    global _client
    _client = None


def verify_dispatch_jwt(
    token: str,
    public_key: RSAPublicKey,
    *,
    now: float | None = None,
) -> dict[str, Any]:
    """Verify RS256 dispatch JWT via anila-core; map errors to DispatchAuthError."""
    try:
        return _core_verify_dispatch_jwt(token, public_key, now=now)
    except DispatchTokenError as exc:
        raise DispatchAuthError(str(exc)) from exc


async def verify_dispatch_authorization(
    authorization: str | None,
    *,
    jwks_client: JwksClient | None = None,
) -> dict[str, Any]:
    """Verify Bearer dispatch JWT; return claims. Fail-closed on any error."""
    client = jwks_client or get_jwks_client()
    if not client.configured:
        raise DispatchAuthError("JWKS URL unset")
    try:
        token = extract_bearer(authorization)
        header = parse_unverified_header(token)
        kid = header.get("kid")
        if not kid or not isinstance(kid, str):
            raise DispatchAuthError("missing kid")
        public_key = await client.get_public_key(kid)
        return verify_dispatch_jwt(token, public_key)
    except DispatchAuthError:
        raise
    except DispatchTokenError as exc:
        raise DispatchAuthError(str(exc)) from exc
    except JwksKeyNotFoundError as exc:
        raise DispatchAuthError(str(exc)) from exc
    except JwksFetchError as exc:
        raise DispatchAuthError(f"JWKS fetch failed: {exc}") from exc
    except JwksError as exc:
        raise DispatchAuthError(str(exc)) from exc


def identity_from_claims(claims: dict[str, Any]) -> dict[str, Any]:
    """Map verified claims to the identity dict handlers consume."""
    uid = claims.get("user_id")
    return {
        "user_id": str(uid) if uid is not None else None,
        "department": claims.get("department"),
        "agent_id": claims.get("agent_id"),
    }


# Back-compat names used by older unit tests — thin wrappers, fail-closed.
def verify_service_token(
    provided: str | None,
    expected: str,
    *,
    allow_unset: bool = False,
) -> bool:
    """Deprecated. Static csk- comparison removed (P2.1). Always fail-closed.

    ``allow_unset`` is ignored — blank configuration must not open the door.
    """
    del expected, allow_unset, provided
    return False


def trusted_user_identity(
    allowed: bool,
    *,
    user_id: str | None = None,
    email: str | None = None,
    groups: str | None = None,
    claims: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Deprecated plaintext-header path. Prefer :func:`identity_from_claims`."""
    del email, groups
    if claims is not None:
        return identity_from_claims(claims)
    if not allowed:
        return {}
    return {"user_id": user_id, "department": None, "agent_id": None}
