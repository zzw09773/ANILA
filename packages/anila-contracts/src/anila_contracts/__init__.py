"""ANILA framework-neutral cross-service wire contracts.

The top-level API contains the Gate 1 foundations, Gate 2 governance
envelopes, and the Gate 5 v2 routing/policy/manifest/grant envelopes.
Implementation enums and schema-version constants remain in their defining
modules rather than becoming separate wire contracts.
"""

from .agents import AgentManifest
from .classification import Classification
from .contexts import TaskContext, TraceContext
from .errors import AgentError
from .events import StepEvent
from .grants import ExecutionGrant
from .invocations import InvocationCommand
from .policy import PolicyGateResult
from .routing import RouteDecision
from .sources import SourceSnapshot
from .summaries import SafeSummary

__version__ = "2.0.0"

__all__ = [
    "AgentError",
    "AgentManifest",
    "Classification",
    "ExecutionGrant",
    "InvocationCommand",
    "PolicyGateResult",
    "RouteDecision",
    "SafeSummary",
    "SourceSnapshot",
    "StepEvent",
    "TaskContext",
    "TraceContext",
]
