"""Verify CSP ``router-context/v1`` provenance assertions.

The Router is a consumer of CSP authority.  This verifier intentionally has
no local signing material and never falls back to raw ``X-ANILA-*`` headers.
It fetches CSP's JWKS, caches every published ``kid`` (so overlap during key
rotation works), and performs one forced refresh when a request presents an
unknown key id.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urlsplit

import httpx
from jose import JWTError, jwt  # type: ignore[import-untyped]
from anila_contracts.contexts import AuthAssurance


ROUTER_CONTEXT_HEADER = "X-ANILA-Router-Context"
ROUTER_CONTEXT_HEADER_TYP = "anila-router-context"
ROUTER_CONTEXT_TOKEN_TYPE = "router-context/v1"
ROUTER_CONTEXT_AUDIENCE = "anila-router"
ROUTER_CONTEXT_MAX_TTL_SECONDS = 60
ROUTER_CONTEXT_CLAIMS = frozenset(
    {
        "iss",
        "aud",
        "iat",
        "exp",
        "jti",
        "type",
        "sub",
        "caller_user_id",
        "owner_id",
        "task_id",
        "run_id",
        "source_snapshot_id",
        "trace_id",
        "invocation_id",
        "session_id",
        "task_type",
        "classification",
        "scopes",
        "required_capabilities",
        "auth_assurance",
        "body_sha256",
    }
)


class RouterContextVerificationError(ValueError):
    """Raised for any malformed, untrusted, stale, or body-mismatched token."""


@dataclass(frozen=True, slots=True)
class RouterContextClaims:
    """Validated, CSP-authored Router context values."""

    issuer: str
    audience: str
    issued_at: int
    expires_at: int
    jti: str
    token_type: str
    sub: str
    caller_user_id: str
    owner_id: str
    task_id: str
    run_id: str
    source_snapshot_id: str
    trace_id: str
    invocation_id: str
    session_id: str
    task_type: str
    classification: str
    scopes: tuple[str, ...]
    required_capabilities: tuple[str, ...]
    auth_assurance: dict[str, Any]
    body_sha256: str

    def context_values(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        """Build the Router runtime input using only verified claims."""

        return {
            "identity": self.caller_user_id,
            "owner_id": self.owner_id,
            "session_id": self.session_id,
            "task_id": int(self.task_id),
            "run_id": int(self.run_id),
            "source_snapshot_id": int(self.source_snapshot_id),
            "trace_id": self.trace_id,
            "invocation_id": self.invocation_id,
            "task_type": self.task_type,
            "classification": self.classification,
            "scopes": self.scopes,
            "required_capabilities": self.required_capabilities,
            "auth_assurance": self.auth_assurance,
            "messages": messages,
        }


def canonical_router_body_bytes(body: Mapping[str, Any]) -> bytes:
    """Match CSP's canonical JSON digest for the finalized request body."""

    if not isinstance(body, Mapping):
        raise RouterContextVerificationError("Router body 必須是 JSON object")
    try:
        text = json.dumps(
            dict(body),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise RouterContextVerificationError("Router body canonicalization 失敗") from exc
    return text.encode("utf-8")


def canonical_router_body_sha256(body: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_router_body_bytes(body)).hexdigest()


def _safe_text(value: object, *, name: str) -> str:
    if not isinstance(value, str):
        raise RouterContextVerificationError(f"{name} 必須是非空單行字串")
    text = value.strip()
    if not text or any(ord(char) < 0x20 or ord(char) == 0x7F for char in text):
        raise RouterContextVerificationError(f"{name} 必須是非空單行字串")
    return text


def _positive_decimal(value: object, *, name: str) -> str:
    text = _safe_text(value, name=name)
    if not text.isascii() or not text.isdecimal() or text.startswith("0"):
        raise RouterContextVerificationError(f"{name} 必須是 positive numeric id")
    return text


def _token_sequence(value: object, *, name: str) -> tuple[str, ...]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise RouterContextVerificationError(f"{name} 必須是 token sequence")
    result: list[str] = []
    for item in value:
        token = _safe_text(item, name=name)
        if token in result:
            raise RouterContextVerificationError(f"{name} 不得有重複值")
        result.append(token)
    return tuple(result)


def _validate_auth_assurance(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise RouterContextVerificationError("auth_assurance 必須是 object")
    result = dict(value)
    required = {"sid", "amr", "acr", "auth_time", "break_glass"}
    if set(result) != required:
        raise RouterContextVerificationError("auth_assurance 欄位不符合 canonical schema")
    _safe_text(result["sid"], name="auth_assurance.sid")
    _safe_text(result["acr"], name="auth_assurance.acr")
    if not isinstance(result["amr"], list):
        raise RouterContextVerificationError("auth_assurance.amr 無效")
    amr = []
    for method in result["amr"]:
        amr.append(_safe_text(method, name="auth_assurance.amr"))
    if len(amr) != len(set(amr)):
        raise RouterContextVerificationError("auth_assurance.amr 不得重複")
    if not isinstance(result["auth_time"], str) or not result["auth_time"].strip():
        raise RouterContextVerificationError("auth_assurance.auth_time 無效")
    if not isinstance(result["break_glass"], bool):
        raise RouterContextVerificationError("auth_assurance.break_glass 無效")
    result["amr"] = amr
    try:
        AuthAssurance.model_validate(result)
    except (TypeError, ValueError) as exc:
        raise RouterContextVerificationError("auth_assurance schema 無效") from exc
    return result


def _body_session_id(body: Mapping[str, Any]) -> str | None:
    standard = body.get("session_id")
    extension = body.get("anila_session_id")
    values = [value for value in (standard, extension) if value not in (None, "")]
    if any(not isinstance(value, str) for value in values):
        raise RouterContextVerificationError("request body session_id 無效")
    if len(values) == 2 and values[0] != values[1]:
        raise RouterContextVerificationError("request body session_id 欄位不一致")
    return values[0] if values else None


def _claims_from_payload(payload: Mapping[str, Any]) -> RouterContextClaims:
    if set(payload) != ROUTER_CONTEXT_CLAIMS:
        raise RouterContextVerificationError("router-context claims schema 不符")
    issuer = _safe_text(payload["iss"], name="iss")
    audience = _safe_text(payload["aud"], name="aud")
    token_type = _safe_text(payload["type"], name="type")
    if token_type != ROUTER_CONTEXT_TOKEN_TYPE:
        raise RouterContextVerificationError("router-context type 不符")
    numeric: dict[str, int] = {}
    for name in ("iat", "exp"):
        value = payload[name]
        if isinstance(value, bool) or not isinstance(value, int):
            raise RouterContextVerificationError(f"{name} 必須是 integer")
        numeric[name] = value
    if numeric["exp"] <= numeric["iat"]:
        raise RouterContextVerificationError("router-context exp 必須晚於 iat")
    if numeric["exp"] - numeric["iat"] > ROUTER_CONTEXT_MAX_TTL_SECONDS:
        raise RouterContextVerificationError("router-context TTL 超過 60 秒")
    jti = _safe_text(payload["jti"], name="jti")
    sub = _positive_decimal(payload["sub"], name="sub")
    caller = _positive_decimal(payload["caller_user_id"], name="caller_user_id")
    owner = _positive_decimal(payload["owner_id"], name="owner_id")
    if not (sub == caller == owner):
        raise RouterContextVerificationError("sub/caller_user_id/owner_id 不一致")
    task_id = _positive_decimal(payload["task_id"], name="task_id")
    run_id = _positive_decimal(payload["run_id"], name="run_id")
    source_snapshot_id = _positive_decimal(
        payload["source_snapshot_id"], name="source_snapshot_id"
    )
    text_fields = {
        name: _safe_text(payload[name], name=name)
        for name in (
            "trace_id",
            "invocation_id",
            "session_id",
            "task_type",
            "classification",
            "body_sha256",
        )
    }
    if len(text_fields["body_sha256"]) != 64 or any(
        char not in "0123456789abcdef" for char in text_fields["body_sha256"]
    ):
        raise RouterContextVerificationError("body_sha256 格式無效")
    return RouterContextClaims(
        issuer=issuer,
        audience=audience,
        issued_at=numeric["iat"],
        expires_at=numeric["exp"],
        jti=jti,
        token_type=token_type,
        sub=sub,
        caller_user_id=caller,
        owner_id=owner,
        task_id=task_id,
        run_id=run_id,
        source_snapshot_id=source_snapshot_id,
        trace_id=text_fields["trace_id"],
        invocation_id=text_fields["invocation_id"],
        session_id=text_fields["session_id"],
        task_type=text_fields["task_type"],
        classification=text_fields["classification"],
        scopes=_token_sequence(payload["scopes"], name="scopes"),
        required_capabilities=_token_sequence(
            payload["required_capabilities"], name="required_capabilities"
        ),
        auth_assurance=_validate_auth_assurance(payload["auth_assurance"]),
        body_sha256=text_fields["body_sha256"],
    )


class RouterContextTokenVerifier:
    """JWKS-backed verifier with bounded cache and one unknown-kid refresh."""

    def __init__(
        self,
        jwks_url: str,
        *,
        issuer: str,
        audience: str = ROUTER_CONTEXT_AUDIENCE,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 10.0,
        cache_ttl_seconds: float = 300.0,
        now: Callable[[], float] | None = None,
    ) -> None:
        parsed = urlsplit(jwks_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("CSP JWKS URL 必須是絕對 HTTP(S) URL")
        self.jwks_url = jwks_url
        self.issuer = _safe_text(issuer, name="issuer")
        self.audience = _safe_text(audience, name="audience")
        self.transport = transport
        self.timeout = timeout
        self.cache_ttl_seconds = cache_ttl_seconds
        self._now = now or time.time
        self._keys: dict[str, dict[str, Any]] = {}
        self._loaded_at = 0.0
        self._refresh_lock = asyncio.Lock()

    @property
    def cached_kids(self) -> tuple[str, ...]:
        return tuple(sorted(self._keys))

    async def _refresh(self, *, force: bool = False) -> None:
        current = self._now()
        if not force and self._keys and current - self._loaded_at < self.cache_ttl_seconds:
            return
        async with self._refresh_lock:
            current = self._now()
            if not force and self._keys and current - self._loaded_at < self.cache_ttl_seconds:
                return
            try:
                async with httpx.AsyncClient(
                    timeout=self.timeout, transport=self.transport
                ) as client:
                    response = await client.get(self.jwks_url)
                    response.raise_for_status()
                    payload = response.json()
            except (httpx.HTTPError, ValueError, TypeError) as exc:
                raise RouterContextVerificationError("CSP JWKS 無法取得") from exc
            if not isinstance(payload, Mapping) or not isinstance(payload.get("keys"), list):
                raise RouterContextVerificationError("CSP JWKS 格式無效")
            refreshed: dict[str, dict[str, Any]] = {}
            for key in payload["keys"]:
                if not isinstance(key, Mapping):
                    continue
                kid = key.get("kid")
                if not isinstance(kid, str) or not kid.strip():
                    continue
                if key.get("kty") != "RSA" or key.get("alg") not in (None, "RS256"):
                    continue
                refreshed[kid] = dict(key)
            if not refreshed:
                raise RouterContextVerificationError("CSP JWKS 缺少可用 RS256 key")
            # The JWKS publication is the source of truth.  A rotation can
            # overlap old/new keys simply by publishing both in this response;
            # retaining keys that CSP has removed would keep revoked kids
            # trusted indefinitely.
            self._keys = refreshed
            self._loaded_at = current

    async def verify(
        self,
        token: str,
        body: Mapping[str, Any],
    ) -> RouterContextClaims:
        if not isinstance(token, str) or not token.strip():
            raise RouterContextVerificationError("缺少 Router context token")
        try:
            header = jwt.get_unverified_header(token)
        except JWTError as exc:
            raise RouterContextVerificationError("Router context JWT header 無效") from exc
        if (
            header.get("alg") != "RS256"
            or header.get("typ") != ROUTER_CONTEXT_HEADER_TYP
            or not isinstance(header.get("kid"), str)
            or not header["kid"].strip()
        ):
            raise RouterContextVerificationError("Router context JWT header 不符")
        kid = header["kid"]
        await self._refresh()
        key = self._keys.get(kid)
        if key is None:
            # One and only one forced refresh for this verification attempt.
            await self._refresh(force=True)
            key = self._keys.get(kid)
        if key is None:
            raise RouterContextVerificationError("Router context kid 未發佈")
        try:
            payload = jwt.decode(
                token,
                key,
                algorithms=["RS256"],
                issuer=self.issuer,
                audience=self.audience,
                options={
                    "require_exp": True,
                    "require_iat": True,
                    "require_iss": True,
                    "require_aud": True,
                    "require_jti": True,
                },
            )
        except JWTError as exc:
            raise RouterContextVerificationError("Router context JWT signature/claims 無效") from exc
        claims = _claims_from_payload(payload)
        now = int(self._now())
        if claims.issued_at > now or claims.expires_at <= now:
            raise RouterContextVerificationError("Router context 已過期或尚未生效")
        body_digest = canonical_router_body_sha256(body)
        if body_digest != claims.body_sha256:
            raise RouterContextVerificationError("Router request body digest 不一致")
        body_session = _body_session_id(body)
        if body_session != claims.session_id:
            raise RouterContextVerificationError("Router session_id 與 token 不一致")
        return claims


__all__ = [
    "ROUTER_CONTEXT_AUDIENCE",
    "ROUTER_CONTEXT_CLAIMS",
    "ROUTER_CONTEXT_HEADER",
    "ROUTER_CONTEXT_HEADER_TYP",
    "ROUTER_CONTEXT_MAX_TTL_SECONDS",
    "ROUTER_CONTEXT_TOKEN_TYPE",
    "RouterContextClaims",
    "RouterContextTokenVerifier",
    "RouterContextVerificationError",
    "canonical_router_body_bytes",
    "canonical_router_body_sha256",
]
