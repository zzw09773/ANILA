"""Remote agent registry — fetches user-scoped agents from myCSPPlatform."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx

from anila_core.http_pool import get_http_client  # OPT-1

logger = logging.getLogger(__name__)

# Hard ceiling on distinct caller credentials retained in memory. Chosen to
# cover a full-campus concurrent population (~3000) with headroom; eviction is
# LRU so the bound does not grow with JWT rotations / uptime.
_DEFAULT_MAX_ENTRIES = 4096


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
    """TTL-cached registry of agents available to each caller's API key.

    Cache keys remain ``sha256(api_key)`` so callers never share agent lists.
    Entries are kept in an ``OrderedDict`` LRU capped at ``max_entries`` so
    hourly JWT rotation cannot accumulate unbounded permanent entries.
    """

    def __init__(
        self,
        csp_base_url: str,
        ttl: float = 60.0,
        timeout: float = 10.0,
        max_entries: int = _DEFAULT_MAX_ENTRIES,
    ) -> None:
        if max_entries < 1:
            raise ValueError("max_entries must be >= 1")
        self._csp_base_url = csp_base_url.rstrip("/")
        self._ttl = ttl
        self._timeout = timeout
        self._max_entries = max_entries
        # OrderedDict: most-recently-used at the end; popitem(last=False) = LRU.
        self._agents_by_key: OrderedDict[str, dict[str, RemoteAgentManifest]] = OrderedDict()
        self._last_refresh_by_key: dict[str, float] = {}
        self._last_refresh_error: Optional[str] = None
        self._last_refresh_at: Optional[float] = None
        self._lock = asyncio.Lock()

    @property
    def last_refresh_error(self) -> Optional[str]:
        """Most recent refresh error across any caller, or None if the last refresh succeeded."""
        return self._last_refresh_error

    @property
    def last_refresh_at(self) -> Optional[float]:
        """Unix timestamp of the last completed refresh attempt (success or failure)."""
        return self._last_refresh_at

    @property
    def caller_entry_count(self) -> int:
        """Number of distinct caller cache slots currently retained."""
        return len(self._agents_by_key)

    def _cache_key(self, api_key: str) -> str:
        return hashlib.sha256(api_key.encode()).hexdigest()

    def _touch(self, cache_key: str) -> None:
        """Mark ``cache_key`` as most-recently used if present."""
        if cache_key in self._agents_by_key:
            self._agents_by_key.move_to_end(cache_key)

    def _evict_overflow(self) -> None:
        """Drop least-recently-used caller slots until within ``max_entries``."""
        while len(self._agents_by_key) > self._max_entries:
            evicted, _ = self._agents_by_key.popitem(last=False)
            self._last_refresh_by_key.pop(evicted, None)

    def _store(self, cache_key: str, agents: dict[str, RemoteAgentManifest]) -> None:
        self._agents_by_key[cache_key] = agents
        self._agents_by_key.move_to_end(cache_key)
        self._last_refresh_by_key[cache_key] = time.monotonic()
        self._evict_overflow()

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
        self._last_refresh_at = time.time()
        try:
            # OPT-1: shared client
            client = get_http_client()
            resp = await client.get(
                url,
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=self._timeout,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            err_msg = f"{type(exc).__name__}: {exc}"
            self._last_refresh_error = err_msg
            logger.warning("RemoteAgentRegistry: failed to fetch %s — %s", url, err_msg)
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
                requires_encryption=bool(item.get("requires_encryption", False)),
            )
            agents[manifest.agent_id] = manifest

        cache_key = self._cache_key(api_key)
        self._store(cache_key, agents)
        self._last_refresh_error = None
        logger.info("RemoteAgentRegistry: loaded %d agents", len(agents))

    async def ensure_fresh(self, api_key: str) -> None:
        """Refresh only if TTL has expired for the current caller."""
        if self._is_stale(api_key):
            async with self._lock:
                if self._is_stale(api_key):
                    await self._do_refresh(api_key)
        else:
            self._touch(self._cache_key(api_key))

    def list_agents(self, api_key: str) -> list[RemoteAgentManifest]:
        cache_key = self._cache_key(api_key)
        self._touch(cache_key)
        return list(self._agents_by_key.get(cache_key, {}).values())

    def get(self, api_key: str, agent_id: str) -> Optional[RemoteAgentManifest]:
        cache_key = self._cache_key(api_key)
        self._touch(cache_key)
        return self._agents_by_key.get(cache_key, {}).get(agent_id)

    def __len__(self) -> int:
        return sum(len(agents) for agents in self._agents_by_key.values())
