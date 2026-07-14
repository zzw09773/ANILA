"""Gate 4 synchronous trust boundary for agent-originated timeline events.

The downstream agent is not an authority for governance identifiers.  This
module accepts only the frozen ``anila.step`` wire event, validates it against
``anila-contracts``, rebinds every identity/classification field from the CSP
dispatch context, and applies bounded per-event/per-run rate limits.  It is
deliberately stateless beyond one live stream; replay and idempotency belong to
Gate 5's durable StreamBridge.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
import uuid

from pydantic import ValidationError

from anila_contracts import Classification, StepEvent
from anila_contracts.events import STEP_EVENT_SSE_NAME, StepKind, StepStatus

logger = logging.getLogger("anila.stream_bridge.audit")

TIMELINE_EVENT_NAME = STEP_EVENT_SSE_NAME
MAX_EVENT_BYTES = 32 * 1024
MAX_EVENTS_PER_SECOND = 40
MAX_EVENTS_PER_RUN = 500
MAX_SAFE_SUMMARY_CHARS = 500

_SECRET_PATTERN = re.compile(
    r"(?i)(?:"
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----|"
    r"\b(?:sk|csk|bsk)-[A-Za-z0-9_-]{12,}|"
    r"\b(?:api[_-]?key|access[_-]?token|authorization|password)\s*[:=]\s*\S+"
    r")"
)


@dataclass(frozen=True, slots=True)
class BridgeContext:
    task_id: str
    trace_id: str
    agent_id: str
    session_id: str
    run_id: str
    classification: Classification


class StreamValidator:
    """Validate and bind events for one live agent stream."""

    def __init__(
        self,
        context: BridgeContext,
        *,
        max_event_bytes: int = MAX_EVENT_BYTES,
        max_events_per_second: int = MAX_EVENTS_PER_SECOND,
        max_events_per_run: int = MAX_EVENTS_PER_RUN,
        clock=time.monotonic,
    ) -> None:
        self.context = context
        self.max_event_bytes = max_event_bytes
        self.max_events_per_second = max_events_per_second
        self.max_events_per_run = max_events_per_run
        self._clock = clock
        self._accepted = 0
        self._recent: deque[float] = deque()

    def _drop(self, reason: str, *, event_name: str | None) -> None:
        # Never log the rejected payload: it may be the secret we are blocking.
        logger.warning(
            "stream_event_dropped reason=%s event=%s task=%s agent=%s run=%s",
            reason,
            event_name or "message",
            self.context.task_id,
            self.context.agent_id,
            self.context.run_id,
        )

    def _reserve_budget(self, event_name: str) -> bool:
        if self._accepted >= self.max_events_per_run:
            self._drop("run_budget", event_name=event_name)
            return False
        now = self._clock()
        while self._recent and now - self._recent[0] >= 1.0:
            self._recent.popleft()
        if len(self._recent) >= self.max_events_per_second:
            self._drop("rate_budget", event_name=event_name)
            return False
        self._recent.append(now)
        self._accepted += 1
        return True

    def validate(self, event_name: str | None, data: str | None) -> str | None:
        """Return a canonical SSE block or ``None`` when the event is rejected."""
        if event_name != TIMELINE_EVENT_NAME:
            self._drop("event_name", event_name=event_name)
            return None
        if data is None:
            self._drop("missing_data", event_name=event_name)
            return None
        if len(data.encode("utf-8")) > self.max_event_bytes:
            self._drop("event_size", event_name=event_name)
            return None
        if not self._reserve_budget(event_name):
            return None
        try:
            raw = json.loads(data)
            event = StepEvent.model_validate(raw)
        except (json.JSONDecodeError, ValidationError, TypeError):
            self._drop("schema", event_name=event_name)
            return None

        for summary in (event.safe_input_summary, event.safe_output_summary):
            if summary is None:
                continue
            if len(summary) > MAX_SAFE_SUMMARY_CHARS:
                self._drop("summary_size", event_name=event_name)
                return None
            if _SECRET_PATTERN.search(summary):
                self._drop("summary_secret", event_name=event_name)
                return None

        trusted = self.context
        rebound = event.model_copy(
            update={
                "task_id": trusted.task_id,
                "trace_id": trusted.trace_id,
                "agent_id": trusted.agent_id,
                "session_id": trusted.session_id,
                "run_id": trusted.run_id,
                "classification": trusted.classification,
            }
        )
        return (
            f"event: {TIMELINE_EVENT_NAME}\n"
            "data: "
            + rebound.model_dump_json()
            + "\n\n"
        )


def cancelled_terminal_frame(context: BridgeContext) -> str:
    """Create CSP-authored single terminal event after downstream cancellation."""
    event = StepEvent(
        event_id=uuid.uuid4().hex,
        sequence=2_147_483_647,
        cursor="cancelled",
        trace_id=context.trace_id,
        task_id=context.task_id,
        session_id=context.session_id,
        invocation_id=context.run_id,
        run_id=context.run_id,
        step_id=f"agent:{context.agent_id}",
        kind=StepKind.AGENT,
        status=StepStatus.CANCELLED,
        safe_output_summary="執行已取消",
        agent_id=context.agent_id,
        completed_at=datetime.now(timezone.utc),
        classification=context.classification,
    )
    return f"event: {TIMELINE_EVENT_NAME}\ndata: {event.model_dump_json()}\n\n"


__all__ = [
    "BridgeContext",
    "MAX_EVENT_BYTES",
    "MAX_EVENTS_PER_RUN",
    "MAX_EVENTS_PER_SECOND",
    "StreamValidator",
    "TIMELINE_EVENT_NAME",
    "cancelled_terminal_frame",
]
