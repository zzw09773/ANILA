"""anila-studio Full Trace span emitter (Slice 8b, frozen wire contract).

Mirrors the minimal standalone emitter in
``packages/anila-agent/anila_agent/tracing.py`` — but studio **must NOT
import ``anila_core`` / ``agents``**. It re-implements just enough of the
FROZEN wire contract to report the artifact-generation trace:

    POST {csp}/v1/traces/{trace_id}/spans
    JSON {"spans":[{span_id, parent_span_id?, span_type, name,
                    started_at, ended_at?, status, attributes?,
                    producer:"studio"}...]}
    ≤256 spans/batch, Authorization: Bearer <user JWT>, → 202

Design invariants (identical spirit to the agent emitter):
- **No trace_id / no endpoint → ``active`` is False → every method no-ops.**
  Requests without a trace pay zero cost and see zero behaviour change.
- **Ship failure is drop-and-log; ``flush`` never raises.** A trace hiccup
  must not break generation.
- One root ``studio.job`` span + one ``studio.stage`` span per pipeline
  step, each carrying ``started_at`` + ``ended_at`` (single-record form of
  the frozen shape).
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any


logger = logging.getLogger("anila.trace.studio")

# FROZEN: CSP ingestion caps each batch at 256 spans.
MAX_SPANS_PER_BATCH = 256

_SPAN_JOB = "studio.job"
_SPAN_STAGE = "studio.stage"


def _now() -> str:
    """UTC ISO-8601 (ms + Z). CSP orders spans by this."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _new_id() -> str:
    return "span_" + uuid.uuid4().hex[:24]


class StudioTraceEmitter:
    """Buffer + batch-ship studio spans back to CSP.

    ``active`` is False (no trace_id / no endpoint) → all methods no-op,
    guaranteeing requests without a trace header behave exactly as before.
    """

    def __init__(
        self,
        *,
        trace_id: str | None,
        endpoint: str | None,
        bearer: str | None = None,
        task_id: str | None = None,
        classification_level: str | None = None,
        verify_ssl: bool = True,
        timeout: float = 5.0,
    ) -> None:
        self.trace_id = (trace_id or "").strip() or None
        self.endpoint = ((endpoint or "").rstrip("/")) or None
        self.bearer = bearer or None
        self.task_id = task_id
        self.classification_level = classification_level
        self.verify_ssl = verify_ssl
        self.timeout = timeout
        self._root: dict[str, Any] | None = None
        self._current: dict[str, Any] | None = None
        self._stages: list[dict[str, Any]] = []

    @property
    def active(self) -> bool:
        return self.trace_id is not None and self.endpoint is not None

    @property
    def root_span_id(self) -> str | None:
        return self._root["span_id"] if self._root else None

    def start_root(self, name: str, *, attributes: dict[str, Any] | None = None) -> None:
        """Open the root ``studio.job`` span (parent of every stage)."""
        if not self.active:
            return
        attrs = dict(attributes or {})
        if self.task_id and "task_id" not in attrs:
            attrs["task_id"] = self.task_id
        if self.classification_level and "classification_level" not in attrs:
            attrs["classification_level"] = self.classification_level
        self._root = {
            "span_id": _new_id(),
            "span_type": _SPAN_JOB,
            "name": name,
            "started_at": _now(),
            "status": "ok",
            "attributes": attrs,
            "producer": "studio",
        }

    def stage(self, name: str, *, attributes: dict[str, Any] | None = None) -> None:
        """Close the previous stage span, open a new one under the root."""
        if not self.active or self._root is None:
            return
        self._close_current()
        self._current = {
            "span_id": _new_id(),
            "parent_span_id": self._root["span_id"],
            "span_type": _SPAN_STAGE,
            "name": name,
            "started_at": _now(),
            "status": "ok",
            "attributes": dict(attributes or {}),
            "producer": "studio",
        }

    def _close_current(self, *, status: str = "ok") -> None:
        if self._current is not None:
            self._current["ended_at"] = _now()
            self._current["status"] = status
            self._stages.append(self._current)
            self._current = None

    def finish(self, *, status: str = "ok", attributes: dict[str, Any] | None = None) -> None:
        """Close the current stage + the root span."""
        if not self.active or self._root is None:
            return
        self._close_current(status=status)
        if attributes:
            self._root["attributes"].update(attributes)
        self._root["ended_at"] = _now()
        self._root["status"] = status

    async def flush(self) -> None:
        """POST buffered spans back to CSP. Drop-and-log; never raises."""
        if not self.active or self._root is None:
            return
        spans = [self._root, *self._stages]
        # Clear buffer regardless of outcome so a retry can't double-ship.
        self._root = None
        self._current = None
        self._stages = []
        try:
            import httpx

            url = f"{self.endpoint}/v1/traces/{self.trace_id}/spans"
            headers = {"Content-Type": "application/json"}
            if self.bearer:
                headers["Authorization"] = f"Bearer {self.bearer}"
            async with httpx.AsyncClient(
                verify=self.verify_ssl, timeout=self.timeout
            ) as client:
                for i in range(0, len(spans), MAX_SPANS_PER_BATCH):
                    chunk = spans[i : i + MAX_SPANS_PER_BATCH]
                    resp = await client.post(url, headers=headers, json={"spans": chunk})
                    if resp.status_code >= 400:
                        logger.warning(
                            "studio trace ship rejected: HTTP %s (%d spans dropped)",
                            resp.status_code,
                            len(chunk),
                        )
        except Exception as exc:  # noqa: BLE001 — trace must never break gen
            logger.warning("studio trace ship failed: %s (%d spans dropped)", exc, len(spans))
