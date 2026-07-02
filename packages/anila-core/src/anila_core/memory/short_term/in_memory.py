"""In-memory :class:`Session` adapter — for tests and dev only.

Not safe across event loops or processes; pick :class:`SqliteSession`
or roll your own adapter for production.
"""

from __future__ import annotations

from anila_core.models.message import Message
from .protocol import InterruptRecord


class MemorySession:
    """Process-local Session backed by Python lists."""

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self._items: list[Message] = []
        self._interrupts: list[InterruptRecord] = []

    # ---- conversation history ----

    async def get_items(self, limit: int | None = None) -> list[Message]:
        if limit is None:
            return list(self._items)
        if limit <= 0:
            return []
        return list(self._items[-limit:])

    async def add_items(self, items: list[Message]) -> None:
        self._items.extend(items)

    async def pop_item(self) -> Message | None:
        if not self._items:
            return None
        return self._items.pop()

    async def clear_session(self) -> None:
        self._items.clear()
        self._interrupts.clear()

    # ---- pending interrupts ----

    async def pending_interrupts(self) -> list[InterruptRecord]:
        return list(self._interrupts)

    async def push_interrupt(self, record: InterruptRecord) -> None:
        self._interrupts.append(record)

    async def pop_interrupt(self, interrupt_id: str) -> InterruptRecord | None:
        for i, r in enumerate(self._interrupts):
            if r.id == interrupt_id:
                return self._interrupts.pop(i)
        return None
