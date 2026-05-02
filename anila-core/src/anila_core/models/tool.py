"""Tool definition and error models."""

from __future__ import annotations

from enum import Enum
from typing import Any, Callable, Literal, Optional

from pydantic import BaseModel, Field


class ToolSafety(str, Enum):
    """Safety classification for a tool."""

    READ_ONLY = "read_only"
    DESTRUCTIVE = "destructive"
    CONCURRENCY_SAFE = "concurrency_safe"


class ToolPermission(str, Enum):
    """Per-tool permission policy (Sprint 11 PR 3).

    Distinct from :class:`ToolSafety` (which is a *capability* hint
    about concurrency / destructiveness). Permission is the *governance*
    gate that says whether the tool may run at all in the current
    context, and how:

    - ``ALLOW``  — tool runs whenever the LLM calls it (default).
    - ``DENY``   — tool is always rejected with an error result.
    - ``ASK``    — pause the run, ask the user; on approve, the tool
                   runs with the original input. Implemented by
                   returning :class:`InterruptItem(kind="tool_approval")`
                   from the registry; the resume endpoint re-executes
                   the tool with the gate bypassed.
    """

    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"


class ToolDefinition(BaseModel):
    """Definition of a tool available to agents.

    The implementation field is excluded from serialization so tool
    definitions can be safely serialized without capturing callables.
    """

    name: str
    description: str
    input_schema: dict[str, Any]
    safety: ToolSafety = ToolSafety.READ_ONLY
    permission: ToolPermission = ToolPermission.ALLOW
    implementation: Optional[Callable[..., Any]] = Field(default=None, exclude=True)

    model_config = {"arbitrary_types_allowed": True}

    def to_openai_schema(self) -> dict[str, Any]:
        """Generate OpenAI-compatible tool schema.

        Normalizes ``"integer"`` → ``"number"`` for models (e.g. gemma4)
        that only accept a restricted set of JSON Schema types.
        """
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": _normalize_schema_types(self.input_schema),
            },
        }

    def to_anthropic_schema(self) -> dict[str, Any]:
        """Generate Anthropic-compatible tool schema."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }


class ToolError(BaseModel):
    """Structured error from tool execution."""

    type: Literal[
        "retryable",
        "non_retryable",
        "input_error",
        "permission_error",
        "timeout",
    ]
    message: str
    tool_name: str

    def is_retryable(self) -> bool:
        return self.type == "retryable"


def _normalize_schema_types(schema: dict[str, Any]) -> dict[str, Any]:
    """Recursively replace ``"integer"`` with ``"number"`` in JSON Schema.

    Some models (e.g. gemma4) reject ``"integer"`` as an unknown type and
    only accept ``"string" | "number" | "boolean" | "array" | "object"``.
    """
    if not isinstance(schema, dict):
        return schema

    result: dict[str, Any] = {}
    for key, value in schema.items():
        if key == "type" and value == "integer":
            result[key] = "number"
        elif isinstance(value, dict):
            result[key] = _normalize_schema_types(value)
        elif isinstance(value, list):
            result[key] = [
                _normalize_schema_types(item) if isinstance(item, dict) else item
                for item in value
            ]
        else:
            result[key] = value
    return result
