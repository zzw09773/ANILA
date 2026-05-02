"""``MCPClientPool`` — manage N MCP servers concurrently.

A real deployment usually wires several MCP servers at once
(filesystem + github + sentry + corporate ops scripts). The pool:

- Owns the lifecycle (connect / health-check / close) of each client
- Aggregates their Action lists into one flat list ready for an Agent
- Routes ``call_tool(namespaced_name, args)`` to the right client
- Optional per-server restart on transport failure

Usage::

    pool = MCPClientPool([
        MCPServer(name="fs", command="mcp-server-filesystem", args=["/data"]),
        MCPServer(name="gh", command="mcp-server-github"),
    ])
    await pool.connect_all()
    try:
        agent = Agent(
            name="ops",
            actions=tuple(pool.all_actions()),
            ...,
        )
    finally:
        await pool.close_all()

Or as an async-context manager::

    async with MCPClientPool([...]) as pool:
        agent = Agent(actions=tuple(pool.all_actions()), ...)
        ...

Connection failures during ``connect_all`` are partitioned: the pool
records which servers came up cleanly and which didn't via
``connect_results``. Callers can choose to abort on any failure
(``strict=True``) or proceed with a degraded set.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Optional

from agentic_rag.runtime.framework.action import Action
from agentic_rag.runtime.framework.exceptions import UserError
from agentic_rag.runtime.framework.mcp.adapter import all_actions_for_client
from agentic_rag.runtime.framework.mcp.client import MCPClient, MCPServer

logger = logging.getLogger(__name__)


# ── Connect result ──────────────────────────────────────────────────


@dataclass(frozen=True)
class ConnectResult:
    """Outcome of one server's connect attempt.

    ``error`` is ``None`` on success; populated with the wrapping
    exception otherwise. The pool keeps these so dashboards can show
    which servers came up cleanly without each caller having to
    duplicate try/except plumbing.
    """

    server_name: str
    succeeded: bool
    error: Optional[str] = None


# ── Pool ─────────────────────────────────────────────────────────────


class MCPClientPool:
    """Owns N MCP clients keyed by server name.

    Construction takes a list of ``MCPServer`` configs; clients are
    instantiated immediately but no subprocess is spawned until
    ``connect_all()`` (or ``connect(name)`` for a single one).

    Server names must be unique. Re-using a name across configs at
    construction time raises ``UserError``.
    """

    def __init__(self, servers: Sequence[MCPServer]) -> None:
        seen: set[str] = set()
        for s in servers:
            if s.name in seen:
                raise UserError(f"duplicate MCP server name: {s.name!r}")
            seen.add(s.name)
        self._clients: dict[str, MCPClient] = {
            s.name: MCPClient(s) for s in servers
        }
        self._connect_results: dict[str, ConnectResult] = {}

    @property
    def server_names(self) -> list[str]:
        return list(self._clients)

    @property
    def clients(self) -> dict[str, MCPClient]:
        """Live view of all clients (connected or not)."""
        return dict(self._clients)

    @property
    def connect_results(self) -> dict[str, ConnectResult]:
        """Snapshot of the most recent connect outcome per server."""
        return dict(self._connect_results)

    def get(self, server_name: str) -> MCPClient | None:
        return self._clients.get(server_name)

    def require(self, server_name: str) -> MCPClient:
        client = self._clients.get(server_name)
        if client is None:
            raise UserError(
                f"MCPClientPool has no server named {server_name!r}. "
                f"Known: {self.server_names}"
            )
        return client

    # ── Lifecycle ────────────────────────────────────────────────────

    async def connect_all(self, *, strict: bool = False) -> dict[str, ConnectResult]:
        """Bring up every configured server.

        Connects in parallel via ``asyncio.gather``. On failures:

        - ``strict=True`` → raises the first exception; close_all is
          NOT called automatically (caller decides cleanup)
        - ``strict=False`` (default) → records each result, returns
          the dict; partial-success is the typical real-world shape

        Idempotent — re-running connect_all on an already-up pool
        re-fetches each client's tool list (cheap) and re-records
        results.
        """
        names = list(self._clients)

        async def _one(name: str) -> ConnectResult:
            client = self._clients[name]
            try:
                if client.is_connected:
                    await client.refresh_tools()
                else:
                    await client.connect()
                return ConnectResult(server_name=name, succeeded=True)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "MCPClientPool: server %r failed to connect: %s", name, exc
                )
                return ConnectResult(
                    server_name=name, succeeded=False, error=str(exc)
                )

        results = await asyncio.gather(*[_one(n) for n in names])
        self._connect_results = {r.server_name: r for r in results}
        if strict:
            for r in results:
                if not r.succeeded:
                    raise UserError(
                        f"MCPClientPool: server {r.server_name!r} failed: {r.error}"
                    )
        return dict(self._connect_results)

    async def connect(self, server_name: str) -> ConnectResult:
        """Bring up one server. Same semantics as connect_all for that one."""
        client = self.require(server_name)
        try:
            if client.is_connected:
                await client.refresh_tools()
            else:
                await client.connect()
            result = ConnectResult(server_name=server_name, succeeded=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "MCPClientPool: server %r failed to connect: %s", server_name, exc
            )
            result = ConnectResult(
                server_name=server_name, succeeded=False, error=str(exc)
            )
        self._connect_results[server_name] = result
        return result

    async def close_all(self) -> None:
        """Best-effort close on every client. Continues past individual failures."""
        await asyncio.gather(
            *[c.close() for c in self._clients.values()],
            return_exceptions=True,
        )
        self._connect_results.clear()

    async def __aenter__(self) -> "MCPClientPool":
        await self.connect_all()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: object,
    ) -> None:
        await self.close_all()

    # ── Aggregate Action surface ─────────────────────────────────────

    def all_actions(self, *, namespaced: bool = True) -> list[Action]:
        """Return Actions from EVERY successfully-connected client.

        Skips servers whose connect failed — those have no tools
        cached. Logs a one-line warning per skip so callers reading
        the agent's tool list can sanity-check.
        """
        out: list[Action] = []
        for name, client in self._clients.items():
            if not client.is_connected:
                if name in self._connect_results and not self._connect_results[name].succeeded:
                    logger.debug(
                        "MCPClientPool.all_actions: skipping server %r (not connected)", name
                    )
                continue
            out.extend(all_actions_for_client(client, namespaced=namespaced))
        return out

    def actions_for_server(self, server_name: str) -> list[Action]:
        """Return Actions from one specific server."""
        client = self.require(server_name)
        if not client.is_connected:
            return []
        return all_actions_for_client(client, namespaced=True)

    # ── Restart on failure ──────────────────────────────────────────

    async def restart(self, server_name: str) -> ConnectResult:
        """Close + reconnect a single server.

        Useful when a long-running agent has noticed repeated
        ConnectionError on a specific server's tools. Restart
        re-spawns the subprocess; cached tool list is rebuilt.
        """
        client = self.require(server_name)
        try:
            await client.close()
        except Exception:  # noqa: BLE001
            pass
        return await self.connect(server_name)


__all__ = ["ConnectResult", "MCPClientPool"]
