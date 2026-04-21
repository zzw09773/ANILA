"""Remote agent registry — fetches user-scoped agents from myCSPPlatform."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)


@dataclass
class RemoteAgentManifest:
    """Manifest of a registered agent fetched from CSP /v1/agents."""

    agent_id: str
    name: str
    description_for_router: str
    endpoint_url: str
    capabilities: dict[str, Any] = field(default_factory=dict)
    input_schema: Optional[dict[str, Any]] = None
    requires_encryption: bool = False

    def to_tool_description(self) -> str:
        """Short description the Router LLM uses when choosing agents."""
        return f"{self.name} ({self.agent_id}): {self.description_for_router}"


class RemoteAgentRegistry:
    """TTL-cached registry of agents available to each caller's API key."""

    def __init__(
        self,
        csp_base_url: str,
        ttl: float = 60.0,
        timeout: float = 10.0,
    ) -> None:
        self._csp_base_url = csp_base_url.rstrip("/")
        self._ttl = ttl
        self._timeout = timeout
        self._agents_by_key: dict[str, dict[str, RemoteAgentManifest]] = {}
        self._last_refresh_by_key: dict[str, float] = {}
        self._lock = asyncio.Lock()

    def _cache_key(self, api_key: str) -> str:
        return hashlib.sha256(api_key.encode()).hexdigest()

    def _is_stale(self, api_key: str) -> bool:
        cache_key = self._cache_key(api_key)
        last_refresh = self._last_refresh_by_key.get(cache_key, 0.0)
        return (time.monotonic() - last_refresh) >= self._ttl

    async def refresh(self, api_key: str) -> None:
        """Force a refresh from CSP for the given API key."""
        async with self._lock:
            await self._do_refresh(api_key)

    async def _do_refresh(self, api_key: str) -> None:
        url = f"{self._csp_base_url}/v1/agents"
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.get(
                    url,
                    headers={"Authorization": f"Bearer {api_key}"},
                )
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:
            logger.warning("RemoteAgentRegistry: failed to fetch %s — %s", url, exc)
            return

        agents: dict[str, RemoteAgentManifest] = {}
        for item in data.get("data", []):
            manifest = RemoteAgentManifest(
                agent_id=item["id"],
                name=item.get("name", item["id"]),
                description_for_router=item.get("description_for_router", ""),
                endpoint_url=item.get("endpoint_url", ""),
                capabilities=item.get("capabilities") or {},
                input_schema=item.get("input_schema"),
                requires_encryption=bool(item.get("requires_encryption")),
            )
            agents[manifest.agent_id] = manifest

        cache_key = self._cache_key(api_key)
        self._agents_by_key[cache_key] = agents
        self._last_refresh_by_key[cache_key] = time.monotonic()
        logger.info("RemoteAgentRegistry: loaded %d agents", len(agents))

    async def ensure_fresh(self, api_key: str) -> None:
        """Refresh only if TTL has expired for the current caller."""
        if self._is_stale(api_key):
            async with self._lock:
                if self._is_stale(api_key):
                    await self._do_refresh(api_key)

    def list_agents(self, api_key: str) -> list[RemoteAgentManifest]:
        return list(self._agents_by_key.get(self._cache_key(api_key), {}).values())

    def get(self, api_key: str, agent_id: str) -> Optional[RemoteAgentManifest]:
        return self._agents_by_key.get(self._cache_key(api_key), {}).get(agent_id)

    def __len__(self) -> int:
        return sum(len(agents) for agents in self._agents_by_key.values())
