"""Tool guardrails — pre-call input validation + post-call output filter.

Mirrors openai-agents `tool_guardrails.py` shape, distilled to the parts
ANILA actually needs:

- :class:`InputGuardrail` Protocol — inspect the tool input dict
  before the tool body runs. May reject (turn the call into an error
  ``ToolResult``) or modify (substitute a sanitised input).
- :class:`OutputGuardrail` Protocol — inspect the tool result content
  after the body returns. Same reject / modify semantics, applied to
  the model-visible output.
- Built-ins:
  - :class:`RegexBlockInput` / :class:`RegexBlockOutput` —
    regex match → reject **or** redact.
  - :class:`MaxLengthOutput` — soft-cap string output with a
    truncation marker.

Distinct from the **permission policy** (Sprint 11): permission gates
*whether* a tool runs at all (ALLOW / DENY / ASK). Guardrails gate
*what data* flows in / out of an allowed call. Both layers compose.

Wiring: attach to :class:`ToolDefinition.input_guardrails` /
``output_guardrails`` (Sprint 12 PR 5 fields). The
:class:`ToolRegistry.execute` invokes them before / after the tool body.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Optional, Protocol, runtime_checkable


@dataclass(frozen=True)
class GuardrailResult:
    """Result of a single guardrail evaluation.

    Three shapes (one of):

    - ``passed=True`` + no modification → the input/output flows through
      unchanged.
    - ``passed=True`` + ``modified_value=…`` → the input/output is
      substituted with ``modified_value`` (e.g. redacted text).
    - ``passed=False`` → the call is rejected with ``reason`` surfaced
      as the model-visible error.
    """

    passed: bool
    modified_value: Optional[Any] = None
    reason: Optional[str] = None

    @classmethod
    def ok(cls) -> "GuardrailResult":
        return cls(passed=True)

    @classmethod
    def modified(cls, value: Any) -> "GuardrailResult":
        return cls(passed=True, modified_value=value)

    @classmethod
    def reject(cls, reason: str) -> "GuardrailResult":
        return cls(passed=False, reason=reason)


@runtime_checkable
class InputGuardrail(Protocol):
    """Pre-call check on the raw input dict."""

    name: str

    def check(
        self, *, tool_name: str, tool_input: dict[str, Any]
    ) -> GuardrailResult:
        ...


@runtime_checkable
class OutputGuardrail(Protocol):
    """Post-call check on the tool result content (string or dict-list)."""

    name: str

    def check(self, *, tool_name: str, output: Any) -> GuardrailResult:
        ...


# ---------------------------------------------------------------------------
# Built-ins
# ---------------------------------------------------------------------------


class RegexBlockInput:
    """Regex over the JSON-serialised input. Reject or redact on match.

    Use case: block API keys / passwords / SSN-style strings from being
    sent to a tool that would log or forward them. ``mode='reject'``
    aborts the call with a clear reason; ``mode='redact'`` replaces
    matches in any string-valued field with ``replacement``.
    """

    def __init__(
        self,
        *,
        pattern: str,
        mode: str = "reject",
        replacement: str = "[REDACTED]",
        flags: int = 0,
        name: Optional[str] = None,
    ) -> None:
        if mode not in {"reject", "redact"}:
            raise ValueError(
                f"RegexBlockInput.mode must be reject|redact, got {mode!r}"
            )
        self._regex = re.compile(pattern, flags)
        self._mode = mode
        self._replacement = replacement
        self.name = name or f"regex_block_input({pattern})"

    def check(
        self, *, tool_name: str, tool_input: dict[str, Any]
    ) -> GuardrailResult:
        matches_found = False

        def _scrub(value: Any) -> Any:
            nonlocal matches_found
            if isinstance(value, str):
                if self._regex.search(value):
                    matches_found = True
                    if self._mode == "redact":
                        return self._regex.sub(self._replacement, value)
                return value
            if isinstance(value, list):
                return [_scrub(v) for v in value]
            if isinstance(value, dict):
                return {k: _scrub(v) for k, v in value.items()}
            return value

        scrubbed = _scrub(tool_input)
        if not matches_found:
            return GuardrailResult.ok()
        if self._mode == "reject":
            return GuardrailResult.reject(
                f"input rejected by guardrail {self.name!r}: "
                f"matched /{self._regex.pattern}/"
            )
        return GuardrailResult.modified(scrubbed)


class RegexBlockOutput:
    """Regex over a string output. Reject or redact on match.

    Mirror of :class:`RegexBlockInput` for the post-call side. Operates
    only on string outputs — dict / list outputs are passed through
    unchanged.
    """

    def __init__(
        self,
        *,
        pattern: str,
        mode: str = "redact",
        replacement: str = "[REDACTED]",
        flags: int = 0,
        name: Optional[str] = None,
    ) -> None:
        if mode not in {"reject", "redact"}:
            raise ValueError(
                f"RegexBlockOutput.mode must be reject|redact, got {mode!r}"
            )
        self._regex = re.compile(pattern, flags)
        self._mode = mode
        self._replacement = replacement
        self.name = name or f"regex_block_output({pattern})"

    def check(self, *, tool_name: str, output: Any) -> GuardrailResult:
        if not isinstance(output, str):
            return GuardrailResult.ok()
        if not self._regex.search(output):
            return GuardrailResult.ok()
        if self._mode == "reject":
            return GuardrailResult.reject(
                f"output rejected by guardrail {self.name!r}: "
                f"matched /{self._regex.pattern}/"
            )
        return GuardrailResult.modified(
            self._regex.sub(self._replacement, output)
        )


class MaxLengthOutput:
    """Soft-cap string output with a truncation marker.

    Only acts on string outputs. Dict / list outputs are passed
    through unchanged (use a regex / shape guardrail for those).
    """

    def __init__(
        self,
        *,
        max_chars: int,
        marker: str = "\n[…truncated by guardrail]",
        name: Optional[str] = None,
    ) -> None:
        if max_chars <= 0:
            raise ValueError("MaxLengthOutput.max_chars must be positive")
        self._max_chars = max_chars
        self._marker = marker
        self.name = name or f"max_length_output({max_chars})"

    def check(self, *, tool_name: str, output: Any) -> GuardrailResult:
        if not isinstance(output, str):
            return GuardrailResult.ok()
        if len(output) <= self._max_chars:
            return GuardrailResult.ok()
        return GuardrailResult.modified(
            output[: self._max_chars] + self._marker
        )


# ---------------------------------------------------------------------------
# Apply helpers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GuardrailChainResult:
    """Aggregate result after running a chain of guardrails.

    ``passed=False`` means at least one guard rejected — caller turns
    the call into a :class:`ToolResult(is_error=True)` with the
    ``reason``. ``modified_value`` is the *final* value after any
    redactions; ``None`` if no guard modified.
    """

    passed: bool
    modified_value: Optional[Any] = None
    reason: Optional[str] = None
    rejected_by: Optional[str] = None


def apply_input_guardrails(
    guards: list[InputGuardrail],
    *,
    tool_name: str,
    tool_input: dict[str, Any],
) -> GuardrailChainResult:
    """Run input guardrails in registration order. First reject wins.

    Modifications compose — each guard sees the previous guard's
    output. ``tool_input`` is not mutated; modifications return a
    fresh dict.
    """
    current = tool_input
    modified = False
    for guard in guards:
        result = guard.check(tool_name=tool_name, tool_input=current)
        if not result.passed:
            return GuardrailChainResult(
                passed=False,
                reason=result.reason,
                rejected_by=guard.name,
            )
        if result.modified_value is not None:
            current = result.modified_value
            modified = True
    return GuardrailChainResult(
        passed=True,
        modified_value=current if modified else None,
    )


def apply_output_guardrails(
    guards: list[OutputGuardrail],
    *,
    tool_name: str,
    output: Any,
) -> GuardrailChainResult:
    """Run output guardrails in registration order. First reject wins.

    Same composition rules as :func:`apply_input_guardrails`.
    """
    current = output
    modified = False
    for guard in guards:
        result = guard.check(tool_name=tool_name, output=current)
        if not result.passed:
            return GuardrailChainResult(
                passed=False,
                reason=result.reason,
                rejected_by=guard.name,
            )
        if result.modified_value is not None:
            current = result.modified_value
            modified = True
    return GuardrailChainResult(
        passed=True,
        modified_value=current if modified else None,
    )


__all__ = [
    "GuardrailResult",
    "GuardrailChainResult",
    "InputGuardrail",
    "OutputGuardrail",
    "RegexBlockInput",
    "RegexBlockOutput",
    "MaxLengthOutput",
    "apply_input_guardrails",
    "apply_output_guardrails",
]
