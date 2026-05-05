"""Event bus — a thin pub/sub for decoupling the runner from observers (CLI, tests, telemetry).

This is intentionally separate from openai-agents tracing. Tracing is for spans/observability;
the event bus is for in-process listeners that need to react synchronously (renderer, prompt UI).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

EventListener = Callable[["Event"], None]


@dataclass(frozen=True)
class Event:
    """A typed in-process event.

    `kind` examples: turn_started, llm_started, llm_ended, tool_started, tool_ended,
    hook_fired, message, turn_ended, error.
    """

    kind: str
    payload: dict[str, Any] = field(default_factory=dict)


class EventBus:
    """Subscribe by kind or to all events. Listeners are called in registration order.

    Listener exceptions are caught and logged so a misbehaving observer cannot break the run.
    """

    def __init__(self) -> None:
        self._by_kind: dict[str, list[EventListener]] = defaultdict(list)
        self._all: list[EventListener] = []

    def on(self, kind: str, listener: EventListener) -> None:
        self._by_kind[kind].append(listener)

    def on_any(self, listener: EventListener) -> None:
        self._all.append(listener)

    def emit(self, kind: str, **payload: Any) -> None:
        event = Event(kind=kind, payload=payload)
        for listener in (*self._by_kind.get(kind, ()), *self._all):
            try:
                listener(event)
            except Exception:  # noqa: BLE001 - observer must not break the run
                from anila_agent.utils.logging import get_logger

                get_logger(__name__).exception("event listener for %s raised", kind)

    def listeners_for(self, kind: str) -> Iterable[EventListener]:
        return tuple(self._by_kind.get(kind, ()))
