"""User-tenant memory client for AgenticRAG (vendored, decoupled from anila-core).

When CSP forwards a chat request to an AgenticRAG instance, it sets:

* ``X-ANILA-User-Id`` — which user the agent is serving
* ``X-CSP-Service-Token`` — this agent's own service credential (csk-)
* ``X-ANILA-User-Email`` — convenience for audit / logging

This module turns those headers into a typed
:class:`AgenticRagCallerContext` and provides
:func:`fetch_user_facts` to read the user's long-term facts from
CSP at ``GET /api/memory/users/{user_id}/facts``.

Why this is **vendored locally** rather than imported from
``anila_core.memory.long_term``:

* AgenticRAG and ANILA platform are intentionally decoupled. The
  template should be deployable without taking a hard dependency
  on anila-core's evolving memory module.
* The wire contract (HTTP endpoint + headers) is a stable
  cross-service interface — clients can be written in Go / Node /
  any language without touching anila-core.
* Vendoring ~120 lines is cheaper than the maintenance + import
  cost of pulling in the SDK's whole long_term namespace just for
  one HTTP call.

If anila-core's adapter Protocol or DTO shape changes, this client
keeps working as long as CSP's HTTP response shape is stable. CSP's
``FactResponse`` schema is the contract.

Configuration: ``ANILA_CSP_BASE_URL`` env var (set by the agent's
deployment, not by CSP). When unset, :func:`fetch_user_facts`
returns an empty list — the agent degrades to "no user memory"
rather than failing.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Optional

import httpx
from fastapi import Header, Request

logger = logging.getLogger(__name__)


_DEFAULT_BASE_URL_ENV = "ANILA_CSP_BASE_URL"
_DEFAULT_TIMEOUT_SECONDS = 5.0


# ── Data classes ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class UserFact:
    """A single fact CSP has remembered about the calling user.

    Mirrors CSP's ``FactResponse`` schema's required fields. Optional
    timestamps / source-provenance fields are dropped — the agent
    only needs key/value/confidence to personalise responses.
    """

    key: str
    value: str
    confidence: float = 1.0


@dataclass(frozen=True)
class AgenticRagCallerContext:
    """Per-request identity surfaced from CSP-forwarded headers.

    All fields optional so the dataclass remains constructable in
    dev / test scenarios (curl with no proxy in front). Code that
    needs a particular field should None-check before use.
    """

    user_id: Optional[int] = None
    user_email: Optional[str] = None
    service_token: Optional[str] = None
    csp_base_url: Optional[str] = None

    @property
    def can_read_user_memory(self) -> bool:
        """True when the agent has everything it needs to call back
        into CSP for the user's memory.
        """
        return (
            self.user_id is not None
            and self.service_token is not None
            and self.csp_base_url is not None
        )


# ── FastAPI dependency ───────────────────────────────────────────────────────


def extract_caller_context(
    request: Request,
    x_anila_user_id: Optional[str] = Header(default=None, alias="X-ANILA-User-Id"),
    x_anila_user_email: Optional[str] = Header(default=None, alias="X-ANILA-User-Email"),
    x_csp_service_token: Optional[str] = Header(default=None, alias="X-CSP-Service-Token"),
) -> AgenticRagCallerContext:
    """FastAPI dependency reading CSP-forwarded identity headers.

    Tolerates malformed / missing headers — agents should degrade
    to "no user attribution" rather than 500 on a junk header.
    """
    user_id: Optional[int] = None
    if x_anila_user_id:
        try:
            user_id = int(x_anila_user_id)
        except ValueError:
            user_id = None

    base = os.environ.get(_DEFAULT_BASE_URL_ENV)
    if base:
        base = base.rstrip("/")

    ctx = AgenticRagCallerContext(
        user_id=user_id,
        user_email=x_anila_user_email or None,
        service_token=x_csp_service_token or None,
        csp_base_url=base,
    )
    request.state.agentic_rag_caller = ctx
    return ctx


# ── HTTP client ───────────────────────────────────────────────────────────────


async def fetch_user_facts(
    caller: AgenticRagCallerContext,
    *,
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
) -> list[UserFact]:
    """GET ``{csp}/api/memory/users/{user_id}/facts`` with the agent's token.

    Returns ``[]`` (never raises) when:

    * caller is missing required fields (no env, no headers)
    * CSP is unreachable / returns non-200
    * response payload is malformed

    The empty-list-on-failure pattern is deliberate — user memory
    is a "nice to have" enrichment for AgenticRAG, never a required
    input. A misconfigured ``ANILA_CSP_BASE_URL`` shouldn't break
    chat for an entire user.
    """
    if not caller.can_read_user_memory:
        return []
    # caller.can_read_user_memory guards all three fields, but the
    # type checker still sees Optional — assert to narrow.
    assert caller.csp_base_url is not None
    assert caller.service_token is not None
    assert caller.user_id is not None

    url = f"{caller.csp_base_url}/api/memory/users/{caller.user_id}/facts"
    headers = {"X-CSP-Service-Token": caller.service_token}

    try:
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            resp = await client.get(url, headers=headers)
    except httpx.RequestError as exc:
        logger.warning(
            "agentic_rag.user_memory: connect failed user_id=%s: %s",
            caller.user_id,
            exc,
        )
        return []

    if resp.status_code != 200:
        logger.warning(
            "agentic_rag.user_memory: non-200 user_id=%s status=%s body=%r",
            caller.user_id,
            resp.status_code,
            resp.text[:200],
        )
        return []

    try:
        payload = resp.json()
        raw_facts = payload.get("facts") or []
    except (ValueError, AttributeError) as exc:
        logger.warning(
            "agentic_rag.user_memory: bad payload user_id=%s: %s",
            caller.user_id,
            exc,
        )
        return []

    facts: list[UserFact] = []
    for item in raw_facts:
        if not isinstance(item, dict):
            continue
        key = item.get("key")
        value = item.get("value")
        if not isinstance(key, str) or not isinstance(value, str):
            continue
        try:
            confidence = float(item.get("confidence", 1.0))
        except (TypeError, ValueError):
            confidence = 1.0
        facts.append(UserFact(key=key, value=value, confidence=confidence))
    return facts


# ── Convenience: format facts as a system-prompt prefix ──────────────────────


def format_user_facts_block(facts: list[UserFact]) -> Optional[str]:
    """Render facts as a Markdown block for prepending to the agent's
    system prompt. Returns ``None`` when ``facts`` is empty so
    callers can do ``"".join(filter(None, [block, base_prompt]))``.

    Format mirrors CSP's own block style for visual consistency
    when the user sees both the CSP-injected block (path A) and an
    AgenticRAG-rendered block (path B) — though in normal flow only
    one is present at a time.
    """
    if not facts:
        return None
    lines = ["## 使用者背景（已記住的事實）"]
    for f in facts:
        lines.append(f"- **{f.key}**: {f.value}")
    lines.append("")
    lines.append(
        "以上是平台對使用者的長期記憶，請參考但不要原文照抄；"
        "若記憶內容與本次對話矛盾，以本次對話為準。"
    )
    return "\n".join(lines)
