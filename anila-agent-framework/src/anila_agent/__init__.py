"""anila-agent-framework — provider-agnostic agent runtime primitives.

v0.1.0-alpha skeleton. Public surface stabilises in v0.1.0 GA after
Sprint 5 lands lifecycle / guardrails / handoffs / sessions / tracing
(see ``docs/agenticrag-phase1-plan.md`` upstream).

What's here right now:

- ``anila_agent.exceptions`` — generic exception hierarchy
- ``anila_agent.usage`` — provider-agnostic token / request usage
- ``anila_agent.providers.protocol`` — LLMProvider Protocol

What's coming (Sprint 1 stage B onwards):

- ``anila_agent.agent`` — Agent class
- ``anila_agent.items`` — Message / event types
- ``anila_agent.tool`` — ToolDefinition / ToolRegistry
- ``anila_agent.runner`` — Runner / AgentRunner
- ``anila_agent.providers.openai_compat`` — Chat Completions provider

Provenance: files adapted from openai-agents-python (MIT) carry a header
note at the top of the file.
"""

__version__ = "0.1.0a1"

from anila_agent.exceptions import (
    AgentsException,
    MaxTurnsExceeded,
    ModelBehaviorError,
    ModelRefusalError,
    ToolTimeoutError,
    UserError,
)
from anila_agent.usage import (
    InputTokensDetails,
    OutputTokensDetails,
    RequestUsage,
    Usage,
)

__all__ = [
    "AgentsException",
    "InputTokensDetails",
    "MaxTurnsExceeded",
    "ModelBehaviorError",
    "ModelRefusalError",
    "OutputTokensDetails",
    "RequestUsage",
    "ToolTimeoutError",
    "Usage",
    "UserError",
    "__version__",
]
