"""``anila_trace_sdk`` — export ANILA spans to the CSP Full-Trace endpoint.

This module is the producer side of the Full Trace Protocol
(doc-05 §6 / doc-09 §10). It is deliberately *additive* and
*fail-open*: tracing must NEVER break the host application. Every
network / serialisation path swallows its own exceptions and degrades
to drop-and-log, and the in-memory queue is capped so a wedged CSP
endpoint can never grow memory without bound.

Three public pieces:

* :class:`TraceExporter` — thread-safe, batching background exporter
  that POSTs spans to the FROZEN wire endpoint
  ``POST {base_url}/v1/traces/{trace_id}/spans`` with body
  ``{"spans": [ <span dict>, ... ]}`` (≤256 per batch). Auth reuses the
  CSP data-plane bearer mechanics via ``Authorization: Bearer …``, supplied
  lazily by ``token_provider``. Agent/service credentials and user/API-key
  credentials intentionally share that wire shape at the trace ingest edge.

* :class:`TraceSession` — per-``trace_id`` span factory. ``span()`` /
  ``async_span()`` context managers auto-generate the span id, time the
  body, mark ``status`` ok/error (re-raising on exception) and enqueue
  the finished span dict onto the exporter. Auto-parents nested spans.

* :class:`ExportingProcessor` — a :class:`SpanProcessor` that bridges
  the existing :mod:`anila_core.tracing` hooks (``Tracer`` / ``Span``)
  to a :class:`TraceExporter`, mapping the in-tree :class:`SpanKind`
  onto the doc-05 §6 span-type strings.

Span dict shape (canonical — mirrored verbatim into the ``anila.spans``
SSE event and the callback POST body), matching the FROZEN wire
contract's enumerated keys::

    {span_id, parent_span_id?, span_type, name,
     started_at, ended_at?, status, attributes?, producer?}
"""

from __future__ import annotations

import logging
import threading
import uuid
from collections import deque
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Callable, Iterator, Optional

from .span import Span, SpanKind, SpanStatus

logger = logging.getLogger(__name__)

# Hard ceiling from the FROZEN wire contract: ≤256 spans per POST batch.
_MAX_BATCH = 256

TokenProvider = Callable[[], Optional[str]]


def _now_iso() -> str:
    """UTC ISO-8601 with a trailing ``Z`` (matches doc-05 span examples)."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _new_span_id() -> str:
    return f"span-{uuid.uuid4().hex[:16]}"


# ---------------------------------------------------------------------------
# doc-05 §6 — the 13 REQUIRED span-type strings (exact). Exposed so callers /
# tests can reference them without re-typing (and drift-proof).
# ---------------------------------------------------------------------------
SPAN_TYPES: tuple[str, ...] = (
    "agent.run.started",
    "agent.step.started",
    "agent.step.finished",
    "agent.model_call.started",
    "agent.model_call.finished",
    "agent.tool_call.started",
    "agent.tool_call.finished",
    "agent.retrieval.started",
    "agent.retrieval.finished",
    "agent.output.started",
    "agent.output.finished",
    "agent.error",
    "agent.run.finished",
)

# Bridge the in-tree SpanKind (run/agent/llm/tool/handoff/interrupt/internal)
# onto doc-05 span-types. A processor's ``on_end`` fires on completion, so a
# finished span carries both timestamps and maps to the pair's ``.finished``
# member (``started_at`` conveys when it began). Errors override to
# ``agent.error``.
_KIND_TO_SPAN_TYPE: dict[SpanKind, str] = {
    SpanKind.RUN: "agent.run.finished",
    SpanKind.AGENT: "agent.step.finished",
    SpanKind.LLM: "agent.model_call.finished",
    SpanKind.TOOL: "agent.tool_call.finished",
    SpanKind.HANDOFF: "agent.step.finished",
    SpanKind.INTERRUPT: "agent.step.finished",
    SpanKind.INTERNAL: "agent.step.finished",
}


# ---------------------------------------------------------------------------
# Span handle
# ---------------------------------------------------------------------------


class SpanHandle:
    """A mutable, open span. Built by :class:`TraceSession`.

    Callers may enrich attributes / flag errors on the handle before it
    closes; the terminal :meth:`to_dict` renders the FROZEN wire shape.
    """

    __slots__ = (
        "span_id",
        "trace_id",
        "span_type",
        "name",
        "parent_span_id",
        "producer",
        "attributes",
        "status",
        "started_at",
        "ended_at",
    )

    def __init__(
        self,
        *,
        span_id: str,
        trace_id: str,
        span_type: str,
        name: str,
        parent_span_id: Optional[str],
        producer: Optional[str],
        attributes: Optional[dict[str, Any]],
    ) -> None:
        self.span_id = span_id
        self.trace_id = trace_id
        self.span_type = span_type
        self.name = name
        self.parent_span_id = parent_span_id
        self.producer = producer
        self.attributes: dict[str, Any] = dict(attributes or {})
        self.status = "ok"
        self.started_at = _now_iso()
        self.ended_at: Optional[str] = None

    def set_attribute(self, key: str, value: Any) -> None:
        self.attributes[key] = value

    def update(self, attributes: dict[str, Any]) -> None:
        self.attributes.update(attributes)

    def set_error(self, message: Optional[str] = None) -> None:
        """Flag the span as failed without raising (for graceful error paths)."""
        self.status = "error"
        if message:
            self.attributes.setdefault("error", str(message)[:500])

    def to_dict(self) -> dict[str, Any]:
        """Render exactly the FROZEN wire keys; omit empty optionals."""
        d: dict[str, Any] = {
            "span_id": self.span_id,
            "span_type": self.span_type,
            "name": self.name,
            "started_at": self.started_at,
            "status": self.status,
        }
        if self.parent_span_id:
            d["parent_span_id"] = self.parent_span_id
        if self.ended_at:
            d["ended_at"] = self.ended_at
        if self.attributes:
            d["attributes"] = dict(self.attributes)
        if self.producer:
            d["producer"] = self.producer
        return d


# ---------------------------------------------------------------------------
# Exporter
# ---------------------------------------------------------------------------


class TraceExporter:
    """Thread-safe, batching background exporter to the CSP trace endpoint.

    Spans are enqueued (non-blocking) and flushed by a daemon worker on a
    fixed interval or once ``batch_size`` accrues. The queue is capped at
    ``max_queue``; overflow drops the *newest* span and increments the
    drop counter (bounded memory, back-pressure). All POST failures are
    swallowed (drop-and-log) — tracing never propagates errors to the
    host.
    """

    def __init__(
        self,
        base_url: str,
        token_provider: TokenProvider,
        *,
        batch_size: int = 64,
        flush_interval: float = 2.0,
        timeout: float = 5.0,
        max_queue: int = 10_000,
        header_name: str = "Authorization",
        producer: Optional[str] = None,
        start_worker: bool = True,
        client_factory: Optional[Callable[[], Any]] = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token_provider = token_provider
        self._batch_size = max(1, min(int(batch_size), _MAX_BATCH))
        self._flush_interval = max(0.05, float(flush_interval))
        self._timeout = float(timeout)
        self._max_queue = max(1, int(max_queue))
        self._header_name = header_name
        self._producer = producer
        self._client_factory = client_factory

        self._queue: deque[tuple[str, dict[str, Any]]] = deque()
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stopping = threading.Event()

        # Counters (best-effort; read via :meth:`stats`).
        self.dropped = 0
        self.sent = 0
        self.failed = 0

        self._worker: Optional[threading.Thread] = None
        if start_worker:
            self.start()

    # -- lifecycle ------------------------------------------------------

    def start(self) -> None:
        if self._worker is not None and self._worker.is_alive():
            return
        self._stopping.clear()
        self._worker = threading.Thread(
            target=self._run, name="anila-trace-exporter", daemon=True
        )
        self._worker.start()

    def shutdown(self, *, timeout: float = 5.0) -> None:
        """Stop the worker and flush whatever is queued."""
        self._stopping.set()
        self._wake.set()
        worker = self._worker
        if worker is not None and worker.is_alive():
            worker.join(timeout=timeout)
        # Final drain in the caller's thread in case the worker already exited.
        self.flush()

    # -- enqueue --------------------------------------------------------

    def enqueue(self, trace_id: str, span: dict[str, Any]) -> None:
        """Queue one span for ``trace_id``. Non-blocking, never raises."""
        if not trace_id or not isinstance(span, dict):
            return
        with self._lock:
            if len(self._queue) >= self._max_queue:
                self.dropped += 1
                return
            self._queue.append((trace_id, span))
            should_wake = len(self._queue) >= self._batch_size
        if should_wake:
            self._wake.set()

    def stats(self) -> dict[str, int]:
        with self._lock:
            queued = len(self._queue)
        return {
            "queued": queued,
            "dropped": self.dropped,
            "sent": self.sent,
            "failed": self.failed,
        }

    # -- worker ---------------------------------------------------------

    def _run(self) -> None:
        while not self._stopping.is_set():
            self._wake.wait(timeout=self._flush_interval)
            self._wake.clear()
            try:
                self.flush()
            except Exception:  # pragma: no cover — defensive; never break worker
                logger.exception("trace exporter flush loop error")

    def _drain_batches(self) -> list[tuple[str, list[dict[str, Any]]]]:
        """Pop the queue and group by trace_id into ≤256-span batches."""
        with self._lock:
            items = list(self._queue)
            self._queue.clear()
        # Preserve order but group contiguous-by-trace to respect the
        # per-trace endpoint while capping each POST at _MAX_BATCH.
        batches: list[tuple[str, list[dict[str, Any]]]] = []
        grouped: dict[str, list[dict[str, Any]]] = {}
        order: list[str] = []
        for trace_id, span in items:
            if trace_id not in grouped:
                grouped[trace_id] = []
                order.append(trace_id)
            grouped[trace_id].append(span)
        for trace_id in order:
            spans = grouped[trace_id]
            for i in range(0, len(spans), _MAX_BATCH):
                batches.append((trace_id, spans[i : i + _MAX_BATCH]))
        return batches

    def flush(self) -> None:
        """Synchronously POST all queued spans. Never raises."""
        batches = self._drain_batches()
        if not batches:
            return
        for trace_id, spans in batches:
            self._post(trace_id, spans)

    def _build_client(self) -> Any:
        if self._client_factory is not None:
            return self._client_factory()
        import httpx

        return httpx.Client(timeout=self._timeout)

    def _post(self, trace_id: str, spans: list[dict[str, Any]]) -> None:
        url = f"{self._base_url}/v1/traces/{trace_id}/spans"
        headers = {"Content-Type": "application/json"}
        try:
            token = self._token_provider()
        except Exception:
            token = None
        if token:
            headers[self._header_name] = (
                f"Bearer {token}"
                if self._header_name.lower() == "authorization"
                else token
            )
        try:
            client = self._build_client()
            try:
                resp = client.post(url, json={"spans": spans}, headers=headers)
            finally:
                close = getattr(client, "close", None)
                if callable(close):
                    close()
            status = getattr(resp, "status_code", 0)
            if 200 <= status < 300:
                self.sent += len(spans)
            else:
                self.failed += len(spans)
                body = getattr(resp, "text", "")
                logger.warning(
                    "trace export HTTP %s for trace %s (dropped %d spans): %s",
                    status, trace_id, len(spans), str(body)[:200],
                )
        except Exception as exc:  # drop-and-log; tracing never breaks the host
            self.failed += len(spans)
            logger.warning(
                "trace export failed for trace %s (dropped %d spans): %s",
                trace_id, len(spans), exc,
            )


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------


class TraceSession:
    """Per-``trace_id`` span factory + auto-parenting stack.

    ``exporter`` may be ``None`` (SSE-only mode): spans are still recorded
    on :attr:`spans` for mirroring, just not shipped to CSP.
    """

    def __init__(
        self,
        exporter: Optional[TraceExporter],
        trace_id: str,
        *,
        producer: Optional[str] = None,
    ) -> None:
        self._exporter = exporter
        self.trace_id = trace_id
        self._producer = producer
        self._stack: list[str] = []
        # Finished span dicts, in close order — mirrored into anila.spans SSE.
        self.spans: list[dict[str, Any]] = []

    # -- open / close (also used directly for streaming interleave) -----

    def open(
        self,
        span_type: str,
        name: str,
        *,
        parent_span_id: Optional[str] = None,
        attributes: Optional[dict[str, Any]] = None,
    ) -> SpanHandle:
        parent = parent_span_id or (self._stack[-1] if self._stack else None)
        handle = SpanHandle(
            span_id=_new_span_id(),
            trace_id=self.trace_id,
            span_type=span_type,
            name=name,
            parent_span_id=parent,
            producer=self._producer,
            attributes=attributes,
        )
        self._stack.append(handle.span_id)
        return handle

    def close(self, handle: SpanHandle) -> dict[str, Any]:
        if handle.ended_at is None:
            handle.ended_at = _now_iso()
        # Pop this span off the stack (best-effort; tolerate misuse).
        if self._stack and self._stack[-1] == handle.span_id:
            self._stack.pop()
        else:
            try:
                self._stack.remove(handle.span_id)
            except ValueError:
                pass
        span_dict = handle.to_dict()
        self.spans.append(span_dict)
        if self._exporter is not None:
            try:
                self._exporter.enqueue(self.trace_id, span_dict)
            except Exception:  # pragma: no cover — exporter is fail-open already
                logger.debug("trace enqueue failed", exc_info=True)
        return span_dict

    # -- context managers (required public surface) ---------------------

    @contextmanager
    def span(
        self,
        span_type: str,
        name: str,
        *,
        parent_span_id: Optional[str] = None,
        attributes: Optional[dict[str, Any]] = None,
    ) -> Iterator[SpanHandle]:
        handle = self.open(
            span_type, name, parent_span_id=parent_span_id, attributes=attributes
        )
        try:
            yield handle
        except BaseException as exc:
            handle.set_error(f"{type(exc).__name__}: {exc}")
            self.close(handle)
            raise
        else:
            self.close(handle)

    @asynccontextmanager
    async def async_span(
        self,
        span_type: str,
        name: str,
        *,
        parent_span_id: Optional[str] = None,
        attributes: Optional[dict[str, Any]] = None,
    ) -> AsyncIterator[SpanHandle]:
        handle = self.open(
            span_type, name, parent_span_id=parent_span_id, attributes=attributes
        )
        try:
            yield handle
        except BaseException as exc:
            handle.set_error(f"{type(exc).__name__}: {exc}")
            self.close(handle)
            raise
        else:
            self.close(handle)


# ---------------------------------------------------------------------------
# Processor bridge
# ---------------------------------------------------------------------------


class ExportingProcessor:
    """A :class:`~anila_core.tracing.processor.SpanProcessor` that ships every
    finished :class:`Span` (from the in-tree ``Tracer`` / ``TracingHooks``)
    to a :class:`TraceExporter`, mapping ``SpanKind`` onto doc-05 span-types.
    """

    def __init__(
        self, exporter: TraceExporter, *, producer: Optional[str] = None
    ) -> None:
        self._exporter = exporter
        self._producer = producer

    def _span_type(self, span: Span) -> str:
        if span.status == SpanStatus.ERROR:
            return "agent.error"
        return _KIND_TO_SPAN_TYPE.get(span.kind, "agent.step.finished")

    def _to_wire(self, span: Span) -> dict[str, Any]:
        started = datetime.fromtimestamp(span.start_time, tz=timezone.utc)
        d: dict[str, Any] = {
            "span_id": span.span_id,
            "span_type": self._span_type(span),
            "name": span.name,
            "started_at": started.isoformat(timespec="milliseconds").replace(
                "+00:00", "Z"
            ),
            "status": span.status.value if span.status != SpanStatus.UNSET else "ok",
        }
        if span.parent_id:
            d["parent_span_id"] = span.parent_id
        if span.end_time is not None:
            ended = datetime.fromtimestamp(span.end_time, tz=timezone.utc)
            d["ended_at"] = ended.isoformat(timespec="milliseconds").replace(
                "+00:00", "Z"
            )
        attributes = dict(span.attributes)
        if span.error:
            attributes.setdefault("error", str(span.error)[:500])
        if attributes:
            d["attributes"] = attributes
        if self._producer:
            d["producer"] = self._producer
        return d

    def on_end(self, span: Span) -> None:
        try:
            self._exporter.enqueue(span.trace_id, self._to_wire(span))
        except Exception:  # pragma: no cover — never break the trace path
            logger.debug("ExportingProcessor.on_end failed", exc_info=True)


__all__ = [
    "TraceExporter",
    "TraceSession",
    "SpanHandle",
    "ExportingProcessor",
    "SPAN_TYPES",
]
