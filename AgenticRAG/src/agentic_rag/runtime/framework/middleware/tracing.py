"""TraceMiddleware — opens a Span around each Action invocation.

Spans capture: action name, kind, side-effect class, run id, parent
span id, params, output / error, start / end times, elapsed seconds.
Backends turn the span stream into something useful (stdout, file,
in-memory list for tests, future Postgres / OTLP exporters).

The Span tree is built by the middleware itself — there is no
separate trace context object. ``ActionContext.metadata['_trace_parent_span_id']``
threads the parent span id through nested invocations (handoff
targets, nested middleware that re-enters the chain). Consumers that
care about the tree shape walk Spans by ``parent_span_id``.

Why "Span" instead of inventing a new noun: the term has a settled
meaning in OpenTelemetry / Jaeger / Datadog land. Using it here means
exporters to those systems are a thin adapter, not a translation
layer.
"""

from __future__ import annotations

import json
import logging
import sys
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol, TextIO, runtime_checkable

from agentic_rag.runtime.framework.action import Action, ActionContext, ActionResult
from agentic_rag.runtime.framework.middleware.protocol import NextHandler

logger = logging.getLogger(__name__)


# ── Span ───────────────────────────────────────────────────────────────


@dataclass
class Span:
    """One trace event around a single Action invocation.

    Mutable for ergonomic in-place set on close (start_time → end_time
    → elapsed_seconds → output / error). Once a backend has flushed it,
    the span is conceptually frozen; we don't enforce immutability
    because freezing midway through the close path adds copy noise
    without security benefit.
    """

    span_id: str
    parent_span_id: str | None
    run_id: str
    agent_name: str
    action_name: str
    action_kind: str
    side_effect_class: str
    params: dict[str, Any]
    started_at: datetime
    ended_at: datetime | None = None
    elapsed_seconds: float | None = None
    output: Any = None
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def is_open(self) -> bool:
        return self.ended_at is None

    def to_dict(self) -> dict[str, Any]:
        """JSON-friendly dict for backend serialisation."""
        return {
            "span_id": self.span_id,
            "parent_span_id": self.parent_span_id,
            "run_id": self.run_id,
            "agent_name": self.agent_name,
            "action_name": self.action_name,
            "action_kind": self.action_kind,
            "side_effect_class": self.side_effect_class,
            "params": self.params,
            "started_at": self.started_at.isoformat(),
            "ended_at": self.ended_at.isoformat() if self.ended_at else None,
            "elapsed_seconds": self.elapsed_seconds,
            "output": self.output,
            "error": self.error,
            "metadata": self.metadata,
        }


# ── Backend Protocol ───────────────────────────────────────────────────


@runtime_checkable
class TracingBackend(Protocol):
    """Where spans go.

    Backend impls decide their own batching / flushing / I/O strategy.
    The middleware just calls ``record(span)`` once when the span
    closes — fast, async, and free to spawn a background task if the
    backend prefers. Backend errors must NOT propagate; tracing is
    best-effort and never breaks the run.
    """

    async def record(self, span: Span) -> None:
        ...


# ── Reference backends ────────────────────────────────────────────────


class InMemoryBackend:
    """Buffers spans in a list. Useful for tests and one-shot scripts."""

    def __init__(self) -> None:
        self.spans: list[Span] = []

    async def record(self, span: Span) -> None:
        self.spans.append(span)

    def clear(self) -> None:
        self.spans.clear()

    def by_run(self, run_id: str) -> list[Span]:
        return [s for s in self.spans if s.run_id == run_id]


class StdoutBackend:
    """Pretty-prints spans line-by-line to a stream (default stderr).

    JSON output by default — pipeable into ``jq`` / fluent-bit / etc.
    Set ``human=True`` for a one-line tab-separated readable form.
    """

    def __init__(
        self,
        stream: TextIO | None = None,
        *,
        human: bool = False,
    ) -> None:
        self._stream = stream if stream is not None else sys.stderr
        self._human = human

    async def record(self, span: Span) -> None:
        try:
            if self._human:
                line = (
                    f"[trace] {span.elapsed_seconds:.3f}s  "
                    f"{span.run_id[:10]}  {span.agent_name}  {span.action_name}  "
                    f"{'ERR ' + span.error if span.error else 'ok'}"
                )
            else:
                line = json.dumps(span.to_dict(), default=str, ensure_ascii=False)
            self._stream.write(line + "\n")
            self._stream.flush()
        except Exception:  # noqa: BLE001
            # Backend failures are silent on purpose — never let tracing
            # take down a run.
            logger.exception("StdoutBackend.record failed")


# ── Middleware ────────────────────────────────────────────────────────


_PARENT_SPAN_KEY = "_trace_parent_span_id"
"""Key used in ActionContext.metadata to pass parent span id down the chain."""


class TraceMiddleware:
    """Wraps every Action call with a Span and dispatches to the backend.

    Construction takes the backend; subsequent invocations are
    stateless (one Span per call). Span ids are uuid4 hex prefixes —
    plenty of collision resistance for run-scoped trees, much shorter
    than full UUIDs in logs.

    ``capture_params`` / ``capture_output`` toggle whether params /
    output payloads land in the span. Default ``True`` for both;
    deployments that ingest spans into shared dashboards may want to
    set them ``False`` so PII / IP doesn't leak through tracing.
    """

    def __init__(
        self,
        backend: TracingBackend,
        *,
        capture_params: bool = True,
        capture_output: bool = True,
    ) -> None:
        self._backend = backend
        self._capture_params = capture_params
        self._capture_output = capture_output

    async def __call__(
        self,
        action: Action,
        context: ActionContext,
        next_: NextHandler,
    ) -> ActionResult:
        parent_id = context.metadata.get(_PARENT_SPAN_KEY) if context.metadata else None
        span_id = _new_span_id()

        # Build the span; thread our id down to anything ``next_`` invokes
        # so nested calls (handoffs that recurse, future BG_TASK spawns)
        # know who their parent is.
        span = Span(
            span_id=span_id,
            parent_span_id=parent_id,
            run_id=context.run_id,
            agent_name=context.agent_name,
            action_name=action.name,
            action_kind=action.kind.value,
            side_effect_class=action.side_effect_class.value,
            params=dict(context.params) if self._capture_params else {},
            started_at=datetime.now(timezone.utc),
        )

        with _push_parent(context, span_id) as inner_ctx:
            started = time.perf_counter()
            try:
                result = await next_(inner_ctx)
            except Exception as exc:
                # Re-raise after closing the span — tracing must not
                # swallow exceptions, but it must record them.
                span.ended_at = datetime.now(timezone.utc)
                span.elapsed_seconds = time.perf_counter() - started
                span.error = f"{type(exc).__name__}: {exc}"
                await _safe_record(self._backend, span)
                raise

        span.ended_at = datetime.now(timezone.utc)
        span.elapsed_seconds = time.perf_counter() - started
        if result.is_error:
            span.error = result.error
        elif self._capture_output:
            span.output = result.output
        await _safe_record(self._backend, span)
        return result


# ── Helpers ────────────────────────────────────────────────────────────


def _new_span_id() -> str:
    return uuid.uuid4().hex[:16]


@contextmanager
def _push_parent(context: ActionContext, span_id: str) -> Iterator[ActionContext]:
    """Yield a new ActionContext with the parent-span pointer set.

    ActionContext is frozen, so we can't mutate ``context.metadata`` in
    place without breaking the contract. We build a sibling context
    that inherits everything but overrides metadata. The yielded
    context replaces the original for the wrapped ``next_`` call only.
    """
    new_metadata = dict(context.metadata)
    new_metadata[_PARENT_SPAN_KEY] = span_id
    new_ctx = ActionContext(
        run_id=context.run_id,
        agent_name=context.agent_name,
        params=context.params,
        history=context.history,
        metadata=new_metadata,
    )
    yield new_ctx


async def _safe_record(backend: TracingBackend, span: Span) -> None:
    """Backend failures must never crash the run."""
    try:
        await backend.record(span)
    except Exception:  # noqa: BLE001
        logger.exception("tracing backend %r failed", type(backend).__name__)


__all__ = [
    "InMemoryBackend",
    "Span",
    "StdoutBackend",
    "TraceMiddleware",
    "TracingBackend",
]
