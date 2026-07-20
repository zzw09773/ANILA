"""Compatibility access to ANILA's independent wire contracts.

New code may import directly from :mod:`anila_contracts`. This module keeps
the official anila-core consumer surface explicit without copying schemas
into the heavy runtime package.
"""

from anila_contracts import (
    AgentError,
    Classification,
    InvocationCommand,
    SafeSummary,
    SourceSnapshot,
    StepEvent,
    TaskContext,
    TraceContext,
)

__all__ = [
    "AgentError",
    "Classification",
    "InvocationCommand",
    "SafeSummary",
    "SourceSnapshot",
    "StepEvent",
    "TaskContext",
    "TraceContext",
]
