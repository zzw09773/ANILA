"""Backwards-compat shim — canonical location is ``short_term.protocol``.

Kept so that existing call sites importing
``from anila_core.memory.session import Session`` (router_server,
query_engine, handoff models, ...) keep working through the
restructure. New code should import from
``anila_core.memory.short_term`` directly.
"""
from .short_term.protocol import (  # noqa: F401
    InterruptRecord,
    Session,
    new_interrupt_id,
    new_session_id,
)

__all__ = [
    "InterruptRecord",
    "Session",
    "new_interrupt_id",
    "new_session_id",
]
