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


class RunCancelled(AgentsException):
    """Run was cancelled before it could finish.

    ``reason`` distinguishes the cause for downstream handling:
      - ``"signal"``  — caller set a ``cancel_signal`` Event
      - ``"deadline"`` — ``deadline_seconds`` elapsed
      - ``"task"``    — surrounding asyncio.Task was cancelled

    Partial state is captured in the ``run_id`` / ``turns_completed``
    attributes so callers can correlate the cancellation against
    audit-trail items emitted by middleware before the abort.
    """

    reason: str
    run_id: str
    turns_completed: int

    def __init__(self, reason: str, run_id: str, turns_completed: int) -> None:
        self.reason = reason
        self.run_id = run_id
        self.turns_completed = turns_completed
        super().__init__(
            f"Run {run_id} cancelled after {turns_completed} turn(s) "
            f"(reason={reason})"
        )


class OutputValidationError(ModelBehaviorError):
    """The LLM's final output failed structured-output validation.

    Raised when ``Agent.output_type`` is set and the assistant's text
    cannot be parsed / validated against the type. Subclasses
    ``ModelBehaviorError`` so existing callers that only catch the
    parent class still see it; new callers can catch this specifically
    to drive structured-output retries.
    """

    payload: str
    validator_message: str

    def __init__(self, payload: str, validator_message: str) -> None:
        self.payload = payload
        self.validator_message = validator_message
        preview = payload[:200] + ("…" if len(payload) > 200 else "")
        super().__init__(
            f"Output failed validation: {validator_message}. Payload preview: {preview!r}"
        )


# Reserved for Sprint 3 (guardrails). Kept here so import paths stabilise:
# ``from agentic_rag.runtime.framework.exceptions import InputGuardrailTripwireTriggered`` will
# work once the type lands.
#
# class InputGuardrailTripwireTriggered(AgentsException): ...
# class OutputGuardrailTripwireTriggered(AgentsException): ...
# class ToolInputGuardrailTripwireTriggered(AgentsException): ...
# class ToolOutputGuardrailTripwireTriggered(AgentsException): ...
#
# Reserved for Sprint 8 (MCP):
# class MCPToolCancellationError(AgentsException): ...
