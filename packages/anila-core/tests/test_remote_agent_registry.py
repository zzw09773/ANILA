"""Regression tests for the caller-scoped remote agent registry cache."""

from __future__ import annotations

import httpx
import pytest
import respx

from anila_core.registry import remote_agent_manifest as registry_module
from anila_core.registry.remote_agent_manifest import RemoteAgentRegistry


@pytest.mark.asyncio
@respx.mock
async def test_fresh_host_fetches_then_caches_legal_empty_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing timestamps are stale; a fetched empty result still obeys TTL."""

    registry = RemoteAgentRegistry("https://csp.invalid", ttl=60.0)
    now = [5.0]
    route = respx.get("https://csp.invalid/v1/agents").mock(
        side_effect=lambda _request: httpx.Response(200, json={"data": []})
    )

    monkeypatch.setattr(registry_module.time, "monotonic", lambda: now[0])

    await registry.ensure_fresh("sk-fresh-host")
    await registry.ensure_fresh("sk-fresh-host")

    assert route.call_count == 1
    assert registry.list_agents("sk-fresh-host") == []

    now[0] = 66.0
    await registry.ensure_fresh("sk-fresh-host")

    assert route.call_count == 2
