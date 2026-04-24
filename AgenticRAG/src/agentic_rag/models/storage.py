"""Storage schema models for the AgenticRAG framework.

All records are keyed with the three-layer key:
  user_id + project_id + session_id
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum
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


class ChunkType(str, Enum):
    """Kind of node in the hierarchical document tree."""

    DOCUMENT = "document"   # root node for a document (level 0)
    HEADING = "heading"     # heading / sub-heading (level >= 1)
    CONTENT = "content"     # paragraph-ish leaf
    IMAGE = "image"         # leaf representing a VLM-captioned image
    TABLE = "table"         # leaf representing a table (future use)


class DocumentChunk(BaseModel):
    """A node in the hierarchical document tree.

    Hierarchy:
      - ``parent_chunk_id`` links this chunk to its parent in the same
        document (None for the document-root chunk).
      - ``chunk_level`` is the depth from the document root (0 = root,
        1 = top heading, 2 = sub-heading, 3 = paragraph/image, ...).
      - ``chunk_type`` tells consumers how to render or search the node.
      - ``heading_path`` is a flat list of ancestor heading titles, built
        at chunk time so retrieval can cite without a recursive walk.
      - Only leaves (``CONTENT`` and ``IMAGE``) carry an ``embedding``
        and participate in vector search. Parents are stored for
        citation / context expansion only.
    """

    chunk_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    document_id: str
    user_id: str
    project_id: str

    parent_chunk_id: Optional[str] = None
    chunk_level: int = 0
    chunk_type: ChunkType = ChunkType.CONTENT
    heading_path: list[str] = Field(default_factory=list)

    content: str
    embedding: Optional[list[float]] = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=datetime.utcnow)


class Citation(BaseModel):
    """A retrieval result with structured provenance and confidence.

    Returned by the retrieval layer instead of a bare ``DocumentChunk``
    so downstream agents get both the matching leaf content *and*
    enough metadata to cite it.

    ``confidence`` is the cosine similarity between query and chunk,
    clamped to [0, 1]. Callers SHOULD display it (e.g. "87%") and MAY
    filter by it.
    """

    chunk_id: str
    document_id: str
    document_title: str = ""
    source_path: str = ""
    format: str = ""

    chunk_type: ChunkType = ChunkType.CONTENT
    chunk_level: int = 0
    heading_path: list[str] = Field(default_factory=list)
    page: Optional[int] = None

    content: str
    parent_chunk_id: Optional[str] = None
    parent_content: str = ""

    confidence: float = 0.0
    metadata: dict[str, Any] = Field(default_factory=dict)

    def cite(self) -> str:
        """Short human-readable citation like 'Title > H1 > H2 (p.3)'."""
        trail = " > ".join([self.document_title, *self.heading_path]).strip(" >")
        if self.page is not None:
            trail = f"{trail} (p.{self.page})" if trail else f"p.{self.page}"
        return trail or self.document_id


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
