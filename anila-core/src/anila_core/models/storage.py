"""Storage schema models for the ANILA Core agent runtime.

All records are keyed with the three-layer key:
  user_id + project_id + session_id
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field


class Session(BaseModel):
    """Persistent session record."""

    session_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    user_id: str
    project_id: str
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    agent_type: str = "default"
    model: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class StoredMessage(BaseModel):
    """A message persisted to the message store."""

    message_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    session_id: str
    user_id: str
    project_id: str
    role: str
    content: Any
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    tool_call_id: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    token_count: int = 0
    metadata: dict[str, Any] = Field(default_factory=dict)


class DocumentChunk(BaseModel):
    """A chunk of a document stored for retrieval."""

    chunk_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    document_id: str
    user_id: str
    project_id: str
    content: str
    embedding: Optional[list[float]] = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=datetime.utcnow)


class RetrievalTrace(BaseModel):
    """Audit record of a retrieval operation."""

    trace_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    session_id: str
    user_id: str
    project_id: str
    query: str
    retrieved_chunk_ids: list[str] = Field(default_factory=list)
    scores: list[float] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    latency_ms: float = 0.0
