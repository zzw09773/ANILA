"""Generic exception hierarchy for the agent framework.

Originally exceptions.py from openai-agents-python (MIT); adapted here
to drop forward refs to types we haven't ported yet (RunErrorDetails,
guardrail / MCP-specific exceptions). Those exceptions return when their
respective subsystems land in later sprints.

Design rule: every exception escaping the runtime should be an
``AgentsException`` subclass. Anything else gets wrapped at the run loop
boundary so consumers can ``except AgentsException`` and not chase
provider-specific errors.
"""

from __future__ import annotations


class AgentsException(Exception):
    """Base class for every exception raised by the agent framework."""


class MaxTurnsExceeded(AgentsException):
    """Raised when a run hits its max-turn cap without producing a final output."""

    message: str

    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


class ModelBehaviorError(AgentsException):
    """The LLM did something unexpected.

    Examples: called a tool that doesn't exist, returned malformed JSON
    in a structured-output response, returned a finish_reason the
    runtime doesn't know how to interpret.
    """

    message: str

    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


class ModelRefusalError(AgentsException):
    """The model refused to produce the requested output."""

    refusal: str
    """The refusal text the provider returned."""

    def __init__(self, refusal: str):
        self.refusal = refusal
        super().__init__(f"Model refused to produce output: {refusal}")


class UserError(AgentsException):
    """Raised when the caller misuses the SDK.

    e.g. missing required field, invalid configuration, calling an API
    out of order. These are programmer errors, not runtime conditions.
    """

    message: str

    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


class ToolTimeoutError(AgentsException):
    """A function tool exceeded its configured timeout."""

    tool_name: str
    timeout_seconds: float

    def __init__(self, tool_name: str, timeout_seconds: float):
        self.tool_name = tool_name
        self.timeout_seconds = timeout_seconds
        super().__init__(
            f"Tool '{tool_name}' timed out after {timeout_seconds:g} seconds."
        )


# Reserved for Sprint 3 (guardrails). Kept here so import paths stabilise:
# ``from anila_agent.exceptions import InputGuardrailTripwireTriggered`` will
# work once the type lands.
#
# class InputGuardrailTripwireTriggered(AgentsException): ...
# class OutputGuardrailTripwireTriggered(AgentsException): ...
# class ToolInputGuardrailTripwireTriggered(AgentsException): ...
# class ToolOutputGuardrailTripwireTriggered(AgentsException): ...
#
# Reserved for Sprint 8 (MCP):
# class MCPToolCancellationError(AgentsException): ...
