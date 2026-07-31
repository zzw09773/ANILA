"""RemoteAgentRegistry cache bounds and caller isolation (X.8)."""

from __future__ import annotations

import sys
from typing import Any

import httpx
import pytest
import respx

from anila_core.registry.remote_agent_manifest import RemoteAgentRegistry

CSP = "http://csp.test"
AGENTS_URL = f"{CSP}/v1/agents"


def _agents_payload(*agent_ids: str) -> dict[str, Any]:
    return {
        "data": [
            {
                "id": aid,
                "name": aid,
                "description_for_router": f"desc-{aid}",
                "endpoint_url": f"http://{aid}/",
                "capabilities": {},
            }
            for aid in agent_ids
        ]
    }


def _approx_bytes(obj: Any, seen: set[int] | None = None) -> int:
    if seen is None:
        seen = set()
    oid = id(obj)
    if oid in seen:
        return 0
    seen.add(oid)
    size = sys.getsizeof(obj)
    if isinstance(obj, dict):
        for k, v in obj.items():
            size += _approx_bytes(k, seen) + _approx_bytes(v, seen)
    elif isinstance(obj, (list, tuple, set)):
        for x in obj:
            size += _approx_bytes(x, seen)
    elif hasattr(obj, "__dict__"):
        size += _approx_bytes(vars(obj), seen)
    return size


def _cache_bytes(reg: RemoteAgentRegistry) -> int:
    return _approx_bytes(reg._agents_by_key) + _approx_bytes(reg._last_refresh_by_key)


@pytest.mark.asyncio
@respx.mock
async def test_cache_bounded_across_token_rotations() -> None:
    """Many rotations of one caller's token must not grow cache with N.

    Mutant (revert): remove ``_evict_overflow`` body / ``_store`` eviction —
    ``caller_entry_count`` grows with ``n_rotations`` and this assertion fails.
    """
    max_entries = 8
    n_rotations = 200
    respx.get(AGENTS_URL).mock(
        return_value=httpx.Response(200, json=_agents_payload("shared-agent"))
    )
    reg = RemoteAgentRegistry(csp_base_url=CSP, ttl=60.0, max_entries=max_entries)

    for i in range(n_rotations):
        token = f"eyJhbGciOiJSUzI1NiJ9.rot{i}.sig{i}"
        await reg.refresh(token)

    assert reg.caller_entry_count <= max_entries
    assert reg.caller_entry_count == max_entries
    # Must not track every historical token.
    assert reg.caller_entry_count < n_rotations
    assert _cache_bytes(reg) < 50_000  # well under unbounded ~N growth


@pytest.mark.asyncio
@respx.mock
async def test_caller_isolation_different_agent_lists() -> None:
    """Two callers must keep distinct agent lists (isolation invariant).

    Mutant (revert): key every caller on a constant string instead of
    ``sha256(api_key)`` — both callers would see the same (last-written) list
    and this assertion fails.
    """

    def _handler(request: httpx.Request) -> httpx.Response:
        auth = request.headers.get("Authorization", "")
        if auth.endswith("caller-a-token"):
            return httpx.Response(200, json=_agents_payload("agent-a-only"))
        if auth.endswith("caller-b-token"):
            return httpx.Response(200, json=_agents_payload("agent-b-only"))
        return httpx.Response(500, json={"error": "unexpected caller"})

    respx.get(AGENTS_URL).mock(side_effect=_handler)
    reg = RemoteAgentRegistry(csp_base_url=CSP, ttl=60.0, max_entries=32)

    await reg.refresh("caller-a-token")
    await reg.refresh("caller-b-token")

    ids_a = {m.agent_id for m in reg.list_agents("caller-a-token")}
    ids_b = {m.agent_id for m in reg.list_agents("caller-b-token")}
    assert ids_a == {"agent-a-only"}
    assert ids_b == {"agent-b-only"}
    assert ids_a.isdisjoint(ids_b)
    assert reg.caller_entry_count == 2


@pytest.mark.asyncio
@respx.mock
async def test_lru_retains_recent_caller_after_overflow() -> None:
    """After overflow, a recently touched caller still sees their agents."""
    respx.get(AGENTS_URL).mock(
        return_value=httpx.Response(200, json=_agents_payload("keep-me"))
    )
    reg = RemoteAgentRegistry(csp_base_url=CSP, ttl=60.0, max_entries=2)

    await reg.refresh("token-old")
    await reg.refresh("token-mid")
    # Touch old so mid becomes the LRU victim when a third enters.
    assert reg.list_agents("token-old")
    await reg.refresh("token-new")

    assert reg.caller_entry_count == 2
    assert {m.agent_id for m in reg.list_agents("token-old")} == {"keep-me"}
    assert {m.agent_id for m in reg.list_agents("token-new")} == {"keep-me"}
    assert reg.list_agents("token-mid") == []
