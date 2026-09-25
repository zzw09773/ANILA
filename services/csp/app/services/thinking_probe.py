# -*- coding: utf-8 -*-
"""Save-time probe: does this endpoint accept the chosen ``thinking_effort``?

2026-09-03, measured against the live fleet: the Qwen endpoint behind
litellm accepts only ``low`` / ``medium`` / ``xhigh`` and answers 400 with
``Unexpected reasoning effort high. Supported types are xhigh (default),
medium, and low.`` for anything else, while the gemma endpoint swallows
every value, bogus ones included. Nothing in the model name or the
registry row distinguishes the two, so the only honest answer is to ask
the endpoint once, at the moment the administrator saves the row —
otherwise the console shows a level that 400s on every later chat call.

Outcomes:

``rejected``
    Upstream said 400 *about the reasoning effort*. The caller turns this
    into a 422 and the row is not written.
``unreachable``
    Anything else — transport error, timeout, 5xx, an unrelated 4xx. Never
    blocks the save; the UI just says the level went in unprobed.
``ok``
    Upstream accepted the call.

Only the extracted upstream sentence is ever returned. The raw litellm
wrapper carries ``Received Model Group=…`` plus the internal fallback
routing table, which must not reach a browser.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Literal

import httpx
from anila_core.security import (
    ENDPOINT_KIND_MODEL,
    UnsafeEndpointError,
    validate_outbound_url,
)

from app.services.proxy.headers import resolve_model_gateway_key
from app.services.proxy.urls import join_upstream_path

logger = logging.getLogger(__name__)

PROBE_TIMEOUT_S = 20.0
# Whole-model budget for discover_thinking_levels (all probeable levels).
DISCOVER_TIMEOUT_S = 20.0
# Canonical order written to thinking_levels_supported.
DISCOVERED_LEVEL_ORDER = ("none", "low", "medium", "high", "xhigh", "max")

# Levels that actually put ``reasoning_effort`` on the wire (mirrors
# ``proxy.sampling.THINKING_ON_LEVELS``); ``none`` / NULL send nothing to
# reject, so they are never probed.
PROBEABLE_LEVELS = frozenset({"low", "medium", "high", "xhigh", "max"})

ProbeStatus = Literal["ok", "unreachable", "rejected"]

# Bound the text we look at — a probe 400 is small, a misconfigured host
# answering with an HTML page is not.
_BODY_SCAN_MAX_CHARS = 4000
_DETAIL_MAX_CHARS = 300

# Matched against the RAW body rather than a parsed field: litellm nests
# the vLLM error as an escaped JSON string inside its own ``message``, so
# one regex bounded by quote / brace / backslash lands on the same
# sentence whether the backend wrapped it or not — and stops before the
# ``Received Model Group=…`` routing dump either way.
_EFFORT_SENTENCE_RE = re.compile(
    r"Unexpected reasoning effort[^\"{}\\]*", re.IGNORECASE
)
_ANY_EFFORT_SENTENCE_RE = re.compile(
    r"[^.\"{}\\]*reasoning effort[^\"{}\\]*", re.IGNORECASE
)
# Belt and braces: a backend that words its error differently must not
# smuggle an internal address into the browser via this passthrough.
_LEAKS_ADDRESS_RE = re.compile(r"https?://|\b\d{1,3}(?:\.\d{1,3}){3}\b")


@dataclass(frozen=True)
class ProbeResult:
    status: ProbeStatus
    detail: str | None = None


@dataclass(frozen=True)
class DiscoverResult:
    """Supported vendor levels, or ``None`` when the endpoint was unprobed.

    ``levels`` is ``None`` when every probeable level was unreachable (or
    the whole gather timed out). ``none`` is always included when any
    level was reachable — it is never sent on the wire.
    """

    levels: list[str] | None


def _extract_upstream_sentence(body_text: str) -> str | None:
    """Pull just the ``Unexpected reasoning effort …`` sentence out of a 400."""
    match = (
        _EFFORT_SENTENCE_RE.search(body_text)
        or _ANY_EFFORT_SENTENCE_RE.search(body_text)
    )
    if not match:
        return None
    sentence = " ".join(match.group(0).split()).strip()
    if not sentence or _LEAKS_ADDRESS_RE.search(sentence):
        return None
    return sentence[:_DETAIL_MAX_CHARS]


async def probe_thinking_effort(model_like: Any, level: str) -> ProbeResult:
    """Send one 1-token completion carrying ``reasoning_effort=level``.

    ``model_like`` needs ``name`` / ``endpoint_url`` / ``api_version`` /
    ``api_key_secret_ref`` — a not-yet-persisted ``ModelRegistry`` or a
    shim carrying the values about to be written both work, so the caller
    can probe before touching the session.
    """
    endpoint_url = getattr(model_like, "endpoint_url", None) or ""
    api_version = getattr(model_like, "api_version", None)
    if api_version not in ("v1", "v2"):
        api_version = "v1"
    # Probe the same URL live traffic will use (proxy.service builds
    # ``/{api_version}/chat/completions`` too) — a probe that disagrees
    # with the call path proves nothing.
    url = join_upstream_path(endpoint_url, f"/{api_version}/chat/completions")

    try:
        # Guard the FINAL url about to be requested — never guard one
        # string and call another (same rule as the bulk-import walk).
        validate_outbound_url(url, endpoint_kind=ENDPOINT_KIND_MODEL)
    except UnsafeEndpointError as exc:
        # The write path already ran its own guard; landing here means the
        # address is not callable at all. Refusing the save is not this
        # probe's job — the guard on the real call path still applies.
        logger.warning(
            "thinking probe skipped: outbound guard rejected the endpoint (%s)",
            exc.reason,
        )
        return ProbeResult("unreachable", "端點未通過出向檢查，未探測")

    # 要讀 400 內文判斷 reasoning_effort，不能走 complete_chat（4xx 會被收成失敗）。
    # 金鑰仍用 proxy 的 resolve_model_gateway_key，不在這裡另做解密。
    headers = {"Content-Type": "application/json"}
    api_key = resolve_model_gateway_key(model_like)
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    payload = {
        "model": getattr(model_like, "name", None),
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 1,
        "reasoning_effort": level,
    }

    try:
        async with httpx.AsyncClient(timeout=PROBE_TIMEOUT_S) as client:
            resp = await client.post(url, json=payload, headers=headers)
    except httpx.HTTPError as exc:
        logger.warning(
            "thinking probe transport error type=%s", type(exc).__name__
        )
        return ProbeResult("unreachable", f"無法連線（{type(exc).__name__}）")

    if resp.status_code == 400:
        body_text = (resp.text or "")[:_BODY_SCAN_MAX_CHARS]
        if "reasoning effort" in body_text.lower():
            return ProbeResult("rejected", _extract_upstream_sentence(body_text))
        logger.info("thinking probe: upstream 400 unrelated to reasoning effort")
        return ProbeResult("unreachable", "上游回 400，但與 reasoning_effort 無關")

    if 200 <= resp.status_code < 300:
        return ProbeResult("ok")
    return ProbeResult("unreachable", f"上游回 HTTP {resp.status_code}")


def _discoverable(model_like: Any) -> bool:
    model_type = getattr(model_like, "model_type", None)
    protocol = (getattr(model_like, "protocol", None) or "openai_compatible").strip()
    return model_type in ("llm", "vlm") and protocol == "openai_compatible"


async def discover_thinking_levels(
    model_like: Any,
    *,
    probe_fn: Callable[[Any, str], Awaitable[ProbeResult]] | None = None,
) -> DiscoverResult:
    """Probe ``low/medium/high/xhigh/max`` in parallel; always add ``none``.

    2xx levels join the supported set. ``rejected`` is omitted. When every
    probeable level is ``unreachable`` (or the 20s budget expires) the
    result is ``None`` so the caller stores NULL.
    """
    if not _discoverable(model_like):
        return DiscoverResult(None)

    probe = probe_fn or probe_thinking_effort

    async def _one(level: str) -> tuple[str, ProbeResult]:
        return level, await probe(model_like, level)

    try:
        pairs = await asyncio.wait_for(
            asyncio.gather(*(_one(level) for level in sorted(PROBEABLE_LEVELS))),
            timeout=DISCOVER_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        logger.warning("thinking discover timed out after %.1fs", DISCOVER_TIMEOUT_S)
        return DiscoverResult(None)

    accepted: set[str] = set()
    any_reachable = False
    for level, result in pairs:
        if result.status == "ok":
            accepted.add(level)
            any_reachable = True
        elif result.status == "rejected":
            any_reachable = True

    if not any_reachable:
        return DiscoverResult(None)

    accepted.add("none")
    return DiscoverResult([lv for lv in DISCOVERED_LEVEL_ORDER if lv in accepted])
