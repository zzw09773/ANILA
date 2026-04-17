"""SSE event schema for the ANILA Core API.

All events streamed to the client follow the ServerEvent envelope.
Events are delivered as Server-Sent Events (SSE) with:
  event: <event_type>
  data: <json_payload>
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel


class EventType(str, Enum):
    """All event types that can be streamed from the server."""

    # Model output
    MESSAGE_DELTA = "message_delta"
    REASONING_DELTA = "reasoning_delta"

    # Tool lifecycle
    TOOL_CALL_STARTED = "tool_call_started"
    TOOL_CALL_FINISHED = "tool_call_finished"

    # Multi-agent coordination
    TASK_NOTIFICATION = "task_notification"
    AGENT_SUMMARY = "agent_summary"

    # Token tracking
    USAGE_UPDATE = "usage_update"

    # Memory lifecycle
    MEMORY_SAVED = "memory_saved"

    # Compact lifecycle
    COMPACT_TRIGGERED = "compact_triggered"

    # Session management
    AWAY_SUMMARY = "away_summary"

    # Terminal events
    STREAM_DONE = "stream_done"
    ERROR = "error"


class ServerEvent(BaseModel):
    """Envelope for all SSE events."""

    type: EventType
    session_id: str = ""
    payload: Any = None

    def to_sse(self) -> str:
        """Format as a raw SSE string for the response body."""
        import json
        data = json.dumps(
            {"type": self.type.value, "session_id": self.session_id, "payload": self.payload},
            ensure_ascii=False,
        )
        return f"event: {self.type.value}\ndata: {data}\n\n"


class MessageDeltaPayload(BaseModel):
    text: str
    turn_index: int = 0


class ToolCallStartedPayload(BaseModel):
    tool_call_id: str
    tool_name: str
    input: Any = None


class ToolCallFinishedPayload(BaseModel):
    tool_call_id: str
    tool_name: str
    is_error: bool = False
    output_preview: str = ""


class UsageUpdatePayload(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    turn_count: int = 0


class MemorySavedPayload(BaseModel):
    paths: list[str]
    count: int = 0


class CompactTriggeredPayload(BaseModel):
    tokens_before: int
    tokens_after: Optional[int] = None
    strategy: str = "auto"


class AwaySummaryPayload(BaseModel):
    summary: str
    session_id: str


class AgentSummaryPayload(BaseModel):
    task_id: str
    summary: str
    status: str


class TaskNotificationPayload(BaseModel):
    task_id: str
    status: str
    summary: str
    result: str = ""


class ErrorPayload(BaseModel):
    message: str
    code: Optional[str] = None
