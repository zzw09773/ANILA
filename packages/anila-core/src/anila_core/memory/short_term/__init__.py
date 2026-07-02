"""Short-term memory — within-conversation working state.

A ``Session`` holds the message stream + pending interrupts of a
single chat. It does NOT persist across conversations; that's
``long_term/``'s job.

Two ship-in-the-box adapters:

* :class:`MemorySession` — in-process dict, dev / tests only.
* :class:`SqliteSession` — aiosqlite-backed, single-process default.

Multi-process / HA deployments implement the :class:`Session`
Protocol against their own store (Postgres, Redis, …).
"""
from .protocol import (
    InterruptRecord,
    Session,
    new_interrupt_id,
    new_session_id,
)
from .in_memory import MemorySession
from .sqlite import SqliteSession, close_all_connections

__all__ = [
    "InterruptRecord",
    "MemorySession",
    "Session",
    "SqliteSession",
    "close_all_connections",
    "new_interrupt_id",
    "new_session_id",
]
