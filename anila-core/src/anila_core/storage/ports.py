"""Storage and retrieval port definitions (Protocol classes).

These Protocols define the interface that infrastructure adapters must
implement. The core engine depends only on these interfaces, not on any
specific database or vector store.

Storage key structure:
  All operations include user_id + project_id + session_id where relevant.
"""

from __future__ import annotations

from typing import Optional, Protocol, runtime_checkable

from ..models.memory import MemoryFile, MemoryHeader
from ..models.storage import DocumentChunk, RetrievalTrace, Session, StoredMessage


@runtime_checkable
class SessionStore(Protocol):
    """Persistent session record storage."""

    async def get(self, session_id: str) -> Optional[Session]:
        """Return a session by ID, or None if not found."""
        ...

    async def set(self, session: Session) -> None:
        """Upsert a session record."""
        ...

    async def delete(self, session_id: str) -> None:
        """Delete a session record."""
        ...

    async def list_by_project(
        self, user_id: str, project_id: str
    ) -> list[Session]:
        """Return all sessions for a user+project."""
        ...


@runtime_checkable
class MessageStore(Protocol):
    """Conversation message storage."""

    async def append(self, message: StoredMessage) -> None:
        """Append a message to a session's history."""
        ...

    async def get(self, message_id: str) -> Optional[StoredMessage]:
        """Return a message by ID."""
        ...

    async def list_by_session(
        self,
        session_id: str,
        limit: int = 100,
        offset: int = 0,
    ) -> list[StoredMessage]:
        """Return messages for a session, paginated."""
        ...

    async def delete_session_messages(self, session_id: str) -> None:
        """Delete all messages for a session."""
        ...


@runtime_checkable
class DocumentStore(Protocol):
    """Document chunk storage with optional embeddings."""

    async def store(self, chunk: DocumentChunk) -> None:
        """Store a document chunk."""
        ...

    async def retrieve(self, chunk_id: str) -> Optional[DocumentChunk]:
        """Return a document chunk by ID."""
        ...

    async def list_by_document(
        self,
        document_id: str,
        user_id: Optional[str] = None,
        project_id: Optional[str] = None,
    ) -> list[DocumentChunk]:
        """Return all chunks for a document, scoped to user_id + project_id when provided."""
        ...

    async def delete_document(
        self,
        document_id: str,
        user_id: Optional[str] = None,
        project_id: Optional[str] = None,
    ) -> None:
        """Delete all chunks for a document.

        When *user_id* and *project_id* are provided, only chunks matching
        the scope are deleted — preventing cross-tenant deletions.
        """
        ...


@runtime_checkable
class RetrievalTraceStore(Protocol):
    """Audit log for retrieval operations."""

    async def log(self, trace: RetrievalTrace) -> None:
        """Log a retrieval operation."""
        ...

    async def list_by_session(
        self, session_id: str, limit: int = 50
    ) -> list[RetrievalTrace]:
        """Return recent retrieval traces for a session."""
        ...


@runtime_checkable
class MemoryStore(Protocol):
    """Memory file storage keyed by scope."""

    async def read(self, file_path: str) -> Optional[MemoryFile]:
        """Read a memory file by absolute path."""
        ...

    async def write(self, file_path: str, memory: MemoryFile) -> None:
        """Write a memory file."""
        ...

    async def list_headers(
        self,
        user_id: str,
        project_id: str,
        scope: str = "project",
    ) -> list[MemoryHeader]:
        """List memory headers for a given scope."""
        ...

    async def delete(self, file_path: str) -> None:
        """Delete a memory file."""
        ...


@runtime_checkable
class EmbeddingProvider(Protocol):
    """Text embedding provider for semantic search."""

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Return embedding vectors for a list of texts."""
        ...

    @property
    def dimension(self) -> int:
        """Return the embedding dimension."""
        ...


@runtime_checkable
class RetrievalProvider(Protocol):
    """Semantic retrieval provider (e.g. pgvector)."""

    async def search(
        self,
        query_embedding: list[float],
        user_id: str,
        project_id: str,
        top_k: int = 5,
        min_score: float = 0.0,
    ) -> list[DocumentChunk]:
        """Return the top-k most similar document chunks."""
        ...

    async def index(self, chunk: DocumentChunk) -> None:
        """Add a document chunk to the index."""
        ...

    async def delete_document(
        self,
        document_id: str,
        user_id: Optional[str] = None,
        project_id: Optional[str] = None,
    ) -> None:
        """Remove all chunks for a document from the index, scoped to tenant when provided."""
        ...
