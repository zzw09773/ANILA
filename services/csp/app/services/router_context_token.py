"""CSP-signed provenance token for the internal ``anila-router`` model.

The public CSP proxy is the only component allowed to mint this envelope.
It deliberately uses the existing CSP RS256 key pair and JWKS publication;
the Router never receives a shared secret and never treats caller supplied
``X-ANILA-*`` fields as authority.

``router-context/v1`` is a short-lived, body-bound transport assertion.  The
body digest is calculated from a deterministic JSON representation of the
*final* body passed to ``httpx`` (including the generated session id and the
stream options added by the streaming proxy path).
"""

from __future__ import annotations

import hashlib
import json
import secrets
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

from jose import jwt

from app.config import settings
from app.utils.security import ALGORITHM, get_private_key


ROUTER_CONTEXT_HEADER = "X-ANILA-Router-Context"
ROUTER_CONTEXT_HEADER_TYP = "anila-router-context"
ROUTER_CONTEXT_TOKEN_TYPE = "router-context/v1"
ROUTER_CONTEXT_AUDIENCE = "anila-router"
ROUTER_CONTEXT_MAX_TTL_SECONDS = 60

_POSITIVE_FIELDS = frozenset(
    {"caller_user_id", "owner_id", "task_id", "run_id", "source_snapshot_id"}
)
_TEXT_FIELDS = frozenset(
    {
        "sub",
        "trace_id",
        "invocation_id",
        "session_id",
        "task_type",
        "classification",
        "body_sha256",
    }
)
_SEQUENCE_FIELDS = frozenset({"scopes", "required_capabilities"})
_CLAIM_NAMES = frozenset(
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


def _safe_text(value: object, *, name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} 必須是非空單行字串")
    text = value.strip()
    if not text or any(ord(char) < 0x20 or ord(char) == 0x7F for char in text):
        raise ValueError(f"{name} 必須是非空單行字串")
    return text


def _positive_decimal(value: object, *, name: str) -> str:
    if isinstance(value, bool):
        raise ValueError(f"{name} 必須是 positive numeric id")
    text = str(value).strip()
    if not text.isascii() or not text.isdecimal() or text.startswith("0"):
        raise ValueError(f"{name} 必須是 positive numeric id")
    return text


def _token_sequence(value: object, *, name: str) -> list[str]:
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise ValueError(f"{name} 必須是 token sequence")
    result: list[str] = []
    for item in value:
        token = _safe_text(item, name=name)
        if token in result:
            raise ValueError(f"{name} 不得有重複值")
        result.append(token)
    return result


def canonical_router_body_bytes(body: Mapping[str, Any]) -> bytes:
    """Serialize the finalized Router body using one stable JSON form."""

    if not isinstance(body, Mapping):
        raise TypeError("Router body 必須是 JSON object")
    try:
        serialized = json.dumps(
            dict(body),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("Router body 不是可 canonicalize 的 JSON") from exc
    return serialized.encode("utf-8")


def canonical_router_body_sha256(body: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_router_body_bytes(body)).hexdigest()


def _auth_assurance(value: object) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    if not isinstance(value, Mapping):
        raise ValueError("auth_assurance 必須是 object")
    result = dict(value)
    required = {"sid", "amr", "acr", "auth_time", "break_glass"}
    if set(result) != required:
        raise ValueError("auth_assurance 欄位不符合 canonical schema")
    _safe_text(result["sid"], name="auth_assurance.sid")
    _safe_text(result["acr"], name="auth_assurance.acr")
    if isinstance(result["amr"], tuple):
        result["amr"] = list(result["amr"])
    if not isinstance(result["amr"], list) or any(
        not isinstance(item, str) or not item.strip() for item in result["amr"]
    ):
        raise ValueError("auth_assurance.amr 無效")
    if not isinstance(result["auth_time"], str) or not result["auth_time"].strip():
        raise ValueError("auth_assurance.auth_time 無效")
    if not isinstance(result["break_glass"], bool):
        raise ValueError("auth_assurance.break_glass 無效")
    return result


def build_router_context_claims(
    *,
    router_context: Mapping[str, object],
    request_body: Mapping[str, Any],
    caller_user_id: int,
    now: datetime | None = None,
    ttl_seconds: int = ROUTER_CONTEXT_MAX_TTL_SECONDS,
) -> dict[str, Any]:
    """Build and validate the exact signed claim set.

    The context mapping is produced by CSP's task/auth admission code.  All
    identifiers are re-normalized here so an accidental object/string mix-up
    cannot produce a token the Router interprets differently.
    """

    if not isinstance(router_context, Mapping):
        raise TypeError("router_context 必須是 mapping")
    if not isinstance(request_body, Mapping):
        raise TypeError("request_body 必須是 JSON object")
    allowed_context = {
        "task_id",
        "run_id",
        "source_snapshot_id",
        "trace_id",
        "invocation_id",
        "session_id",
        "task_type",
        "classification_level",
        "scopes",
        "required_capabilities",
        "auth_assurance",
        "owner_id",
    }
    unknown = set(router_context) - allowed_context
    if unknown:
        raise ValueError("router_context 含有未允許欄位")
    missing = allowed_context - set(router_context)
    if missing:
        raise ValueError("router_context 缺少必要欄位: " + ", ".join(sorted(missing)))

    caller = _positive_decimal(caller_user_id, name="caller_user_id")
    owner = _positive_decimal(router_context["owner_id"], name="owner_id")
    if caller != owner:
        raise ValueError("caller_user_id 與 owner_id 必須一致")
    session = _safe_text(router_context["session_id"], name="session_id")
    body_session = request_body.get("session_id") or request_body.get("anila_session_id")
    if body_session != session:
        raise ValueError("session_id 與 finalized request body 不一致")

    issued = now or datetime.now(timezone.utc)
    if issued.tzinfo is None:
        issued = issued.replace(tzinfo=timezone.utc)
    issued_epoch = int(issued.timestamp())
    if isinstance(ttl_seconds, bool) or not 1 <= int(ttl_seconds) <= ROUTER_CONTEXT_MAX_TTL_SECONDS:
        raise ValueError("router-context TTL 必須介於 1 到 60 秒")
    expires_epoch = issued_epoch + int(ttl_seconds)

    claims: dict[str, Any] = {
        "iss": settings.JWT_ISSUER,
        "aud": ROUTER_CONTEXT_AUDIENCE,
        "iat": issued_epoch,
        "exp": expires_epoch,
        "jti": secrets.token_urlsafe(24),
        "type": ROUTER_CONTEXT_TOKEN_TYPE,
        "sub": caller,
        "caller_user_id": caller,
        "owner_id": owner,
        "task_id": _positive_decimal(router_context["task_id"], name="task_id"),
        "run_id": _positive_decimal(router_context["run_id"], name="run_id"),
        "source_snapshot_id": _positive_decimal(
            router_context["source_snapshot_id"], name="source_snapshot_id"
        ),
        "trace_id": _safe_text(router_context["trace_id"], name="trace_id"),
        "invocation_id": _safe_text(
            router_context["invocation_id"], name="invocation_id"
        ),
        "session_id": session,
        "task_type": _safe_text(router_context["task_type"], name="task_type"),
        "classification": _safe_text(
            router_context["classification_level"], name="classification"
        ),
        "scopes": _token_sequence(router_context["scopes"], name="scopes"),
        "required_capabilities": _token_sequence(
            router_context["required_capabilities"], name="required_capabilities"
        ),
        "auth_assurance": _auth_assurance(router_context["auth_assurance"]),
        "body_sha256": canonical_router_body_sha256(request_body),
    }
    if set(claims) != _CLAIM_NAMES:
        raise AssertionError("router-context claim set drift")
    return claims


def issue_router_context_token(
    *,
    router_context: Mapping[str, object],
    request_body: Mapping[str, Any],
    caller_user_id: int,
    now: datetime | None = None,
    ttl_seconds: int = ROUTER_CONTEXT_MAX_TTL_SECONDS,
) -> str:
    """Sign a short-lived CSP→Router context assertion."""

    claims = build_router_context_claims(
        router_context=router_context,
        request_body=request_body,
        caller_user_id=caller_user_id,
        now=now,
        ttl_seconds=ttl_seconds,
    )
    return jwt.encode(
        claims,
        get_private_key(),
        algorithm=ALGORITHM,
        headers={"kid": settings.JWT_KID, "typ": ROUTER_CONTEXT_HEADER_TYP},
    )


__all__ = [
    "ROUTER_CONTEXT_AUDIENCE",
    "ROUTER_CONTEXT_HEADER",
    "ROUTER_CONTEXT_HEADER_TYP",
    "ROUTER_CONTEXT_MAX_TTL_SECONDS",
    "ROUTER_CONTEXT_TOKEN_TYPE",
    "build_router_context_claims",
    "canonical_router_body_bytes",
    "canonical_router_body_sha256",
    "issue_router_context_token",
]
