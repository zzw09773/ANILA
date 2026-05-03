"""Background asyncio task that polls CSP for runtime_config every 30 s.

Wiring (typical agent ``lifespan``)::

    poller = RuntimeConfigPoller(
        csp_base_url=settings.csp_base_url,
        csp_service_token=settings.csp_service_token,
        registry=tool_registry,
        on_change=lambda snap, caps: my_workspace_factory.update_caps(caps),
    )
    await poller.start()
    try:
        yield
    finally:
        await poller.stop()

Behaviour:

  * First poll runs immediately (so the agent picks up admin-set
    config before serving the first request) — failures don't block
    startup, the agent still serves with code-defined defaults.
  * Subsequent polls every ``interval_seconds`` (default 30).
  * ETag short-circuit — when the server returns the same ``etag`` as
    last apply, the snapshot isn't re-parsed or re-applied.
  * Network / 5xx errors are logged and the poller keeps running with
    the previously-applied snapshot (no fail-open / fail-closed
    decision is forced — agents stay on the last good config).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, Optional

import httpx

from ..router.tool_router import ToolRegistry
from ..workspace.caps import WorkspaceCaps
from .apply import apply_runtime_config
from .snapshot import RuntimeConfigSnapshot, parse_runtime_config


logger = logging.getLogger(__name__)


# Callback fired on every successful apply (whether the snapshot
# changed or not — caller can compare etags if they care). Receives
# the parsed snapshot and the resolved workspace caps. Async-friendly.
OnChangeFn = Optional[
    Callable[[RuntimeConfigSnapshot, WorkspaceCaps], Awaitable[None] | None]
]


class RuntimeConfigPoller:
    """Polls ``GET /api/agents/me/runtime-config`` every 30 s.

    Args:
        csp_base_url: e.g. ``http://csp:8000``.
        csp_service_token: agent's own ``X-CSP-Service-Token``. If
            ``None``, the poller logs a warning and never starts the
            background task — the agent runs on code defaults.
        registry: live tool registry to mutate on each apply.
        base_workspace_caps: caps the agent was built with. Snapshot
            overrides overlay on top.
        on_change: optional callback fired after each successful apply.
        interval_seconds: poll cadence (default 30).
        timeout_seconds: per-request timeout (default 5).
    """

    def __init__(
        self,
        *,
        csp_base_url: str,
        csp_service_token: Optional[str],
        registry: ToolRegistry,
        base_workspace_caps: Optional[WorkspaceCaps] = None,
        on_change: OnChangeFn = None,
        interval_seconds: float = 30.0,
        timeout_seconds: float = 5.0,
    ) -> None:
        self._csp_base_url = csp_base_url.rstrip("/")
        self._service_token = csp_service_token
        self._registry = registry
        self._base_caps = base_workspace_caps
        self._on_change = on_change
        self._interval = interval_seconds
        self._timeout = timeout_seconds
        self._task: Optional[asyncio.Task[None]] = None
        self._last_etag: str = ""
        self._last_snapshot: Optional[RuntimeConfigSnapshot] = None
        self._last_caps: Optional[WorkspaceCaps] = None

    @property
    def last_etag(self) -> str:
        return self._last_etag

    @property
    def last_snapshot(self) -> Optional[RuntimeConfigSnapshot]:
        return self._last_snapshot

    @property
    def last_caps(self) -> Optional[WorkspaceCaps]:
        return self._last_caps

    async def start(self) -> None:
        """Start polling. Returns once the FIRST poll completes (or fails).

        Doing the first poll inline (rather than fire-and-forget) means
        the agent's request-serving code can rely on the runtime config
        being applied before the first inbound HTTP request.
        """
        if self._service_token is None:
            logger.warning(
                "RuntimeConfigPoller: csp_service_token is None — "
                "skipping (agent stays on code defaults).",
            )
            return
        if self._task is not None:
            return
        # First poll inline.
        await self._poll_once()
        # Then schedule the loop.
        self._task = asyncio.create_task(
            self._loop(), name="anila-runtime-config-poller"
        )

    async def stop(self) -> None:
        """Cancel the background task. Idempotent."""
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except (asyncio.CancelledError, Exception):  # pragma: no cover
            pass
        self._task = None

    async def _loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self._interval)
            except asyncio.CancelledError:
                return
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                return
            except Exception as exc:  # pragma: no cover — defensive
                logger.warning(
                    "RuntimeConfigPoller: poll iteration raised %s — "
                    "continuing with previous snapshot.",
                    exc,
                )

    async def _poll_once(self) -> None:
        """Fetch + apply one runtime_config snapshot.

        Catches network / 4xx / 5xx errors; logs and returns without
        modifying the registry. ETag match short-circuits.
        """
        url = f"{self._csp_base_url}/api/agents/me/runtime-config"
        headers = {"X-CSP-Service-Token": self._service_token or ""}
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.get(url, headers=headers)
        except httpx.RequestError as exc:
            logger.warning(
                "RuntimeConfigPoller: GET %s failed: %s — keeping prev",
                url, exc,
            )
            return

        if resp.status_code == 401 or resp.status_code == 403:
            logger.warning(
                "RuntimeConfigPoller: HTTP %s from %s — service token "
                "rejected. Keeping previous snapshot.",
                resp.status_code, url,
            )
            return
        if resp.status_code >= 400:
            logger.warning(
                "RuntimeConfigPoller: HTTP %s from %s — keeping prev",
                resp.status_code, url,
            )
            return

        try:
            body: dict[str, Any] = resp.json()
        except ValueError:
            logger.warning(
                "RuntimeConfigPoller: %s returned non-JSON — keeping prev",
                url,
            )
            return

        etag = str(body.get("etag", ""))
        if etag and etag == self._last_etag:
            # No change since last apply.
            return

        raw_cfg = body.get("runtime_config")
        snapshot = parse_runtime_config(raw_cfg, etag=etag)
        caps = apply_runtime_config(
            snapshot, self._registry, base_workspace_caps=self._base_caps
        )
        self._last_etag = etag
        self._last_snapshot = snapshot
        self._last_caps = caps
        logger.info(
            "RuntimeConfigPoller: applied snapshot etag=%s "
            "(perms=%s, guardrails=%d, workspace_overrides=%s)",
            etag,
            "yes" if snapshot.permissions != snapshot.permissions.__class__()
                  else "no",
            len(snapshot.guardrails),
            "yes" if snapshot.workspace != snapshot.workspace.__class__()
                  else "no",
        )

        if self._on_change is not None:
            result = self._on_change(snapshot, caps)
            if asyncio.iscoroutine(result):
                try:
                    await result
                except Exception as exc:  # pragma: no cover — defensive
                    logger.warning(
                        "RuntimeConfigPoller.on_change raised: %s", exc,
                    )
