"""Tracer — owns current-span stack via contextvars + dispatches to processors."""

from __future__ import annotations

from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from typing import AsyncIterator, Iterator, Optional

from .processor import SpanProcessor
from .span import Span, SpanKind, SpanStatus, _new_trace_id


# Per-task current span stack. New spans use the top of the stack as
# parent; ``end_span`` pops. contextvars propagate correctly into
# asyncio tasks created from a context that has the var bound.
_current_stack: ContextVar[list[Span]] = ContextVar("anila_span_stack")


def _stack() -> list[Span]:
    try:
        return _current_stack.get()
    except LookupError:
        s: list[Span] = []
        _current_stack.set(s)
        return s


class Tracer:
    """Lightweight tracer.

    Args:
        processors: list of :class:`SpanProcessor` to fan finished spans
            out to. Pass an :class:`InMemoryProcessor` for tests / to
            embed the trace tree in API responses.
        trace_id: optional override. Useful when you want to correlate
            with an external trace_id (e.g. carrying through CSP).
    """

    def __init__(
        self,
        processors: Optional[list[SpanProcessor]] = None,
        trace_id: Optional[str] = None,
    ) -> None:
        self._processors: list[SpanProcessor] = list(processors or [])
        self._trace_id_override = trace_id

    def start_span(
        self,
        name: str,
        kind: SpanKind = SpanKind.INTERNAL,
        **attributes: object,
    ) -> Span:
        """Open a new span; auto-attaches the top-of-stack span as parent."""
        stack = _stack()
        parent = stack[-1] if stack else None
        span = Span(
            name=name,
            kind=kind,
            parent_id=parent.span_id if parent else None,
            trace_id=parent.trace_id if parent else (
                self._trace_id_override or _new_trace_id()
            ),
            attributes=dict(attributes),
        )
        stack.append(span)
        return span

    def end_span(
        self,
        span: Span,
        *,
        status: Optional[SpanStatus] = None,
        error: Optional[str] = None,
    ) -> None:
        """Close a span + dispatch to processors. Idempotent.

        Pops the span off the current-task stack; if the span isn't on
        top (rare misuse), pops anyway and logs no error — processors
        still see it.
        """
        if error is not None and status is None:
            status = SpanStatus.ERROR
        if error is not None:
            span.set_status(SpanStatus.ERROR, error)
        span.end(status=status)
        stack = _stack()
        if stack and stack[-1].span_id == span.span_id:
            stack.pop()
        else:
            # Best-effort: remove by id if found, otherwise leave stack alone.
            for i, s in enumerate(stack):
                if s.span_id == span.span_id:
                    stack.pop(i)
                    break
        for proc in self._processors:
            try:
                proc.on_end(span)
            except Exception:
                # Processors must never break the trace path; swallow.
                pass

    @contextmanager
    def span(
        self,
        name: str,
        kind: SpanKind = SpanKind.INTERNAL,
        **attributes: object,
    ) -> Iterator[Span]:
        """Synchronous context-manager wrapper around start/end."""
        sp = self.start_span(name, kind, **attributes)
        try:
            yield sp
            self.end_span(sp)
        except Exception as exc:
            self.end_span(sp, error=f"{type(exc).__name__}: {exc}")
            raise

    @asynccontextmanager
    async def async_span(
        self,
        name: str,
        kind: SpanKind = SpanKind.INTERNAL,
        **attributes: object,
    ) -> AsyncIterator[Span]:
        """Async context-manager wrapper around start/end."""
        sp = self.start_span(name, kind, **attributes)
        try:
            yield sp
            self.end_span(sp)
        except Exception as exc:
            self.end_span(sp, error=f"{type(exc).__name__}: {exc}")
            raise

    @property
    def current_span(self) -> Optional[Span]:
        stack = _stack()
        return stack[-1] if stack else None
