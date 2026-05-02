"""Span data model — one unit of work in the trace tree."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class SpanKind(str, Enum):
    """Categorical bucket for a span. Mirrors OTel SpanKind plus the
    ANILA-specific kinds we emit by default."""

    RUN = "run"
    AGENT = "agent"
    LLM = "llm"
    TOOL = "tool"
    HANDOFF = "handoff"
    INTERRUPT = "interrupt"
    INTERNAL = "internal"


class SpanStatus(str, Enum):
    UNSET = "unset"
    OK = "ok"
    ERROR = "error"


def _new_span_id() -> str:
    return f"sp-{uuid.uuid4().hex[:16]}"


def _new_trace_id() -> str:
    return f"tr-{uuid.uuid4().hex}"


@dataclass
class Span:
    """One timed unit of work.

    Spans are mutable while open (status / attributes / events can be
    set), then frozen when ``end()`` is called and handed to the
    SpanProcessor.

    Use :meth:`Tracer.start_span` to construct — manual construction
    works too for tests but doesn't auto-link parents.
    """

    name: str
    kind: SpanKind = SpanKind.INTERNAL
    span_id: str = field(default_factory=_new_span_id)
    trace_id: str = field(default_factory=_new_trace_id)
    parent_id: Optional[str] = None

    start_time: float = field(default_factory=time.time)
    end_time: Optional[float] = None
    status: SpanStatus = SpanStatus.UNSET
    error: Optional[str] = None

    attributes: dict[str, Any] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)

    def add_event(self, name: str, **attributes: Any) -> None:
        """Attach a timestamped point-in-time event to this span."""
        self.events.append(
            {
                "name": name,
                "ts": time.time(),
                "attributes": dict(attributes),
            }
        )

    def set_attribute(self, key: str, value: Any) -> None:
        self.attributes[key] = value

    def set_status(self, status: SpanStatus, error: Optional[str] = None) -> None:
        self.status = status
        if error is not None:
            self.error = error

    def end(self, *, status: Optional[SpanStatus] = None) -> None:
        """Close the span. Idempotent."""
        if self.end_time is not None:
            return
        self.end_time = time.time()
        if status is not None:
            self.status = status
        elif self.status == SpanStatus.UNSET:
            self.status = SpanStatus.OK

    @property
    def duration_ms(self) -> Optional[float]:
        if self.end_time is None:
            return None
        return (self.end_time - self.start_time) * 1000.0

    def to_dict(self) -> dict[str, Any]:
        """JSON-friendly serialisation (no children — tree is built by Tracer)."""
        return {
            "span_id": self.span_id,
            "trace_id": self.trace_id,
            "parent_id": self.parent_id,
            "name": self.name,
            "kind": self.kind.value,
            "status": self.status.value,
            "error": self.error,
            "start_ts": self.start_time,
            "end_ts": self.end_time,
            "duration_ms": self.duration_ms,
            "attributes": dict(self.attributes),
            "events": list(self.events),
        }
