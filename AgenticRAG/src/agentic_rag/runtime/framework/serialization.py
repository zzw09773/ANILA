"""``RunSerializer`` — JSON checkpoint / resume for ``RunState``.

Drives the "pod restart mid-run, resume cleanly" use case:

    snapshot = RunSerializer.dump(state)             # serialise
    Path("state.json").write_text(snapshot)          # checkpoint
    # … pod restarts …
    state = RunSerializer.load(Path("state.json").read_text())
    # Resume: machine.step(state) until terminal

Lossless within the framework's own data model. Things outside the
model (the agent / provider objects, middleware closures, callable
handler references) are deliberately NOT serialised — those have to
be reconstructed by the caller before resuming. The serialiser stores
``agent_name`` so the caller knows which Agent registry entry to wire
up.

What this serialises:
- All ``RunState`` scalar fields
- ``RunPhase`` (string)
- ``Message`` history (recursive content / tool_calls)
- ``RunItem`` audit trail (subclass type tag + per-class fields)
- ``Usage`` accumulator
- ``PendingToolCall`` queue
- Datetime fields (ISO 8601)

What this does NOT serialise:
- ``Agent`` objects (caller rebuilds + passes to StateMachine)
- ``Provider`` instances (env-driven; rebuilt on resume)
- ``Middleware`` chain (configuration, not state)
- ``ActionResult.handoff_target`` Agent reference inside an audit
  item — we keep the agent NAME instead so deserialise has no
  forward references.

Schema version: every dump carries ``"_schema": 1`` so future
breaking changes can fail loudly. Bump on incompatible field
additions; use the version field to drive migration when it grows
beyond two.
"""

from __future__ import annotations

import json
from dataclasses import fields, is_dataclass
from datetime import datetime
from typing import Any, Mapping

from agentic_rag.runtime.framework.exceptions import UserError
from agentic_rag.runtime.framework.items import (
    ContentPart,
    ErrorItem,
    HandoffItem,
    ImageURLContent,
    Message,
    MessageOutputItem,
    RefusalContent,
    Role,
    RunItem,
    TextContent,
    ToolCall,
    ToolCallItem,
    ToolResult,
    ToolResultItem,
)
from agentic_rag.runtime.framework.state import (
    PendingToolCall,
    RunPhase,
    RunState,
)
from agentic_rag.runtime.framework.usage import (
    deserialize_usage,
    serialize_usage,
)


_SCHEMA_VERSION = 1


# ── Public API ────────────────────────────────────────────────────────


class RunSerializer:
    """Stateless namespace for serialise / deserialise.

    Class methods rather than module functions so callers can subclass
    to add custom RunItem subtypes (Sprint 5+ when bg_task / mcp items
    arrive).
    """

    @staticmethod
    def dump(state: RunState) -> str:
        """Render ``state`` as a JSON string."""
        return json.dumps(RunSerializer.to_dict(state), ensure_ascii=False)

    @staticmethod
    def load(payload: str | Mapping[str, Any]) -> RunState:
        """Rebuild ``RunState`` from a JSON string or pre-parsed dict."""
        if isinstance(payload, str):
            data = json.loads(payload)
        elif isinstance(payload, Mapping):
            data = dict(payload)
        else:
            raise UserError(
                f"RunSerializer.load expected str or Mapping, got {type(payload).__name__}"
            )
        version = data.get("_schema")
        if version != _SCHEMA_VERSION:
            raise UserError(
                f"RunSerializer schema version mismatch: got {version!r}, "
                f"runtime understands {_SCHEMA_VERSION!r}"
            )
        return RunSerializer.from_dict(data)

    # ── dict-level (useful for tests + custom storage) ───────────────

    @staticmethod
    def to_dict(state: RunState) -> dict[str, Any]:
        return {
            "_schema": _SCHEMA_VERSION,
            "run_id": state.run_id,
            "agent_name": state.agent_name,
            "model": state.model,
            "parent_run_id": state.parent_run_id,
            "group_id": state.group_id,
            "trace_metadata": dict(state.trace_metadata),
            "phase": state.phase.value,
            "turns_completed": state.turns_completed,
            "max_turns": state.max_turns,
            "pending_tool_calls": [
                {"call": _tool_call_to_dict(p.call), "index": p.index}
                for p in state.pending_tool_calls
            ],
            "handoff_target_name": state.handoff_target_name,
            "reflection_count": state.reflection_count,
            "max_reflections": state.max_reflections,
            "history": [_message_to_dict(m) for m in state.history],
            "items": [_item_to_dict(i) for i in state.items],
            "usage": serialize_usage(state.usage),
            "deadline_at": state.deadline_at,
            "created_at": state.created_at.isoformat(),
            "updated_at": state.updated_at.isoformat(),
            "final_output": state.final_output,
            "parsed_output": _safe_serialise(state.parsed_output),
            "error_type": state.error_type,
            "error_message": state.error_message,
        }

    @staticmethod
    def from_dict(data: Mapping[str, Any]) -> RunState:
        return RunState(
            run_id=str(data["run_id"]),
            agent_name=str(data["agent_name"]),
            model=str(data["model"]),
            parent_run_id=data.get("parent_run_id"),
            group_id=data.get("group_id"),
            trace_metadata=dict(data.get("trace_metadata") or {}),
            phase=RunPhase(data.get("phase", RunPhase.PLANNING.value)),
            turns_completed=int(data.get("turns_completed") or 0),
            max_turns=int(data.get("max_turns") or 10),
            pending_tool_calls=tuple(
                PendingToolCall(
                    call=_tool_call_from_dict(p["call"]),
                    index=int(p["index"]),
                )
                for p in (data.get("pending_tool_calls") or [])
            ),
            handoff_target_name=data.get("handoff_target_name"),
            reflection_count=int(data.get("reflection_count") or 0),
            max_reflections=int(data.get("max_reflections") or 1),
            history=tuple(
                _message_from_dict(m) for m in (data.get("history") or [])
            ),
            items=tuple(
                _item_from_dict(i) for i in (data.get("items") or [])
            ),
            usage=deserialize_usage(data.get("usage") or {}),
            deadline_at=data.get("deadline_at"),
            created_at=_datetime_from_iso(data.get("created_at")),
            updated_at=_datetime_from_iso(data.get("updated_at")),
            final_output=data.get("final_output"),
            parsed_output=data.get("parsed_output"),
            error_type=data.get("error_type"),
            error_message=data.get("error_message"),
        )


# ── Message ↔ dict ──────────────────────────────────────────────────


def _message_to_dict(message: Message) -> dict[str, Any]:
    return {
        "role": message.role.value,
        "content": _content_to_dict(message.content),
        "name": message.name,
        "tool_calls": [_tool_call_to_dict(tc) for tc in message.tool_calls],
        "tool_call_id": message.tool_call_id,
    }


def _message_from_dict(data: Mapping[str, Any]) -> Message:
    return Message(
        role=Role(data["role"]),
        content=_content_from_dict(data.get("content", "")),
        name=data.get("name"),
        tool_calls=tuple(
            _tool_call_from_dict(tc) for tc in (data.get("tool_calls") or [])
        ),
        tool_call_id=data.get("tool_call_id"),
    )


def _content_to_dict(content: str | tuple[ContentPart, ...]) -> Any:
    if isinstance(content, str):
        return content
    return [_part_to_dict(p) for p in content]


def _content_from_dict(data: Any) -> Any:
    if isinstance(data, str):
        return data
    if isinstance(data, list):
        return tuple(_part_from_dict(p) for p in data)
    return ""


def _part_to_dict(part: ContentPart) -> dict[str, Any]:
    if isinstance(part, TextContent):
        return {"type": "text", "text": part.text}
    if isinstance(part, ImageURLContent):
        return {"type": "image_url", "url": part.url, "detail": part.detail}
    if isinstance(part, RefusalContent):
        return {"type": "refusal", "refusal": part.refusal}
    raise UserError(f"Cannot serialise content part: {part!r}")


def _part_from_dict(data: Mapping[str, Any]) -> ContentPart:
    type_ = data.get("type")
    if type_ == "text":
        return TextContent(text=str(data.get("text", "")))
    if type_ == "image_url":
        return ImageURLContent(
            url=str(data.get("url", "")),
            detail=data.get("detail", "auto"),
        )
    if type_ == "refusal":
        return RefusalContent(refusal=str(data.get("refusal", "")))
    raise UserError(f"Unknown content part type: {type_!r}")


def _tool_call_to_dict(tc: ToolCall) -> dict[str, Any]:
    return {
        "id": tc.id,
        "name": tc.name,
        "arguments": tc.arguments,
        "type": tc.type,
    }


def _tool_call_from_dict(data: Mapping[str, Any]) -> ToolCall:
    return ToolCall(
        id=str(data.get("id", "")),
        name=str(data.get("name", "")),
        arguments=str(data.get("arguments", "")),
        type=str(data.get("type", "function")),  # type: ignore[arg-type]
    )


def _tool_result_to_dict(result: ToolResult) -> dict[str, Any]:
    return {
        "call_id": result.call_id,
        "name": result.name,
        "output": result.output,
        "error": result.error,
    }


def _tool_result_from_dict(data: Mapping[str, Any]) -> ToolResult:
    return ToolResult(
        call_id=str(data.get("call_id", "")),
        name=str(data.get("name", "")),
        output=data.get("output"),
        error=data.get("error"),
    )


# ── RunItem ↔ dict ──────────────────────────────────────────────────


def _item_to_dict(item: RunItem) -> dict[str, Any]:
    common = {
        "_kind": type(item).__name__,
        "item_id": item.item_id,
        "created_at": item.created_at.isoformat(),
    }
    if isinstance(item, MessageOutputItem):
        return {
            **common,
            "message": _message_to_dict(item.message),
            "usage": serialize_usage(item.usage),
        }
    if isinstance(item, ToolCallItem):
        return {**common, "call": _tool_call_to_dict(item.call)}
    if isinstance(item, ToolResultItem):
        return {
            **common,
            "result": _tool_result_to_dict(item.result),
            "elapsed_seconds": item.elapsed_seconds,
        }
    if isinstance(item, HandoffItem):
        return {
            **common,
            "from_agent": item.from_agent,
            "to_agent": item.to_agent,
            "reason": item.reason,
        }
    if isinstance(item, ErrorItem):
        return {**common, "error_type": item.error_type, "message": item.message}
    raise UserError(f"Cannot serialise RunItem subclass: {type(item).__name__}")


def _item_from_dict(data: Mapping[str, Any]) -> RunItem:
    kind = data.get("_kind")
    common = {
        "item_id": data.get("item_id", ""),
        "created_at": _datetime_from_iso(data.get("created_at")),
    }
    if kind == "MessageOutputItem":
        return MessageOutputItem(
            message=_message_from_dict(data["message"]),
            usage=deserialize_usage(data.get("usage") or {}),
            **common,
        )
    if kind == "ToolCallItem":
        return ToolCallItem(call=_tool_call_from_dict(data["call"]), **common)
    if kind == "ToolResultItem":
        return ToolResultItem(
            result=_tool_result_from_dict(data["result"]),
            elapsed_seconds=float(data.get("elapsed_seconds") or 0.0),
            **common,
        )
    if kind == "HandoffItem":
        return HandoffItem(
            from_agent=str(data.get("from_agent", "")),
            to_agent=str(data.get("to_agent", "")),
            reason=data.get("reason"),
            **common,
        )
    if kind == "ErrorItem":
        return ErrorItem(
            error_type=str(data.get("error_type", "")),
            message=str(data.get("message", "")),
            **common,
        )
    raise UserError(f"Unknown RunItem kind: {kind!r}")


# ── Helpers ────────────────────────────────────────────────────────────


def _datetime_from_iso(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            pass
    return datetime.now()


def _safe_serialise(value: Any) -> Any:
    """Best-effort JSON-friendly rendering of ``parsed_output``.

    The runtime sets ``parsed_output`` to whatever the structured-output
    validator produced — a Pydantic model, a dataclass, a dict, etc.
    For checkpoint storage we render to JSON-compatible primitives so a
    dict-loading path produces the same shape.
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, tuple)):
        return [_safe_serialise(v) for v in value]
    if isinstance(value, dict):
        return {k: _safe_serialise(v) for k, v in value.items()}
    # Pydantic v2
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            return model_dump()
        except Exception:  # noqa: BLE001
            pass
    # Dataclass
    if is_dataclass(value):
        return {f.name: _safe_serialise(getattr(value, f.name)) for f in fields(value)}
    return str(value)


__all__ = ["RunSerializer"]
