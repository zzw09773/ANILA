"""ANILA framework-neutral cross-service wire contracts.

The top-level API contains the Gate 1 v0 foundations and the Gate 2 v1
governance envelopes. Implementation enums and schema-version constants
remain in their defining modules rather than becoming separate contracts.
"""

from .classification import Classification
from .contexts import TaskContext, TraceContext
from .errors import AgentError
from .events import StepEvent
from .invocations import InvocationCommand
from .sources import SourceSnapshot
from .summaries import SafeSummary

__version__ = "1.0.0"

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
