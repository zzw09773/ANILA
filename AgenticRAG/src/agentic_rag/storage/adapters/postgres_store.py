"""PostgreSQL adapters for Session, Message, and RetrievalTrace storage.

Implements:
  - PgSessionStore      → SessionStore Protocol
  - PgMessageStore      → MessageStore Protocol
  - PgRetrievalTraceStore → RetrievalTraceStore Protocol

All tables are created via initialize_schema().
Uses CREATE TABLE IF NOT EXISTS for idempotent migration.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from ...models.storage import RetrievalTrace, Session, StoredMessage

# Phase 0 (2026-05-02): reclaimed local PgPool — AgenticRAG must
# not import platform-internal anila-core packages.
from .pg_pool import PgPool

logger = logging.getLogger(__name__)

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id  TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL,
    project_id  TEXT NOT NULL,
    agent_type  TEXT DEFAULT 'default',
    model       TEXT,
    metadata    JSONB DEFAULT '{}',
    created_at  TIMESTAMPTZ DEFAULT now(),
    updated_at  TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_sessions_project
    ON sessions(user_id, project_id);

CREATE TABLE IF NOT EXISTS messages (
    message_id   TEXT PRIMARY KEY,
    session_id   TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    user_id      TEXT NOT NULL,
    project_id   TEXT NOT NULL,
    role         TEXT NOT NULL,
    content      JSONB NOT NULL,
    tool_calls   JSONB DEFAULT '[]',
    tool_call_id TEXT,
    token_count  INTEGER DEFAULT 0,
    metadata     JSONB DEFAULT '{}',
    created_at   TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_messages_session
    ON messages(session_id, created_at);

CREATE TABLE IF NOT EXISTS retrieval_traces (
    trace_id            TEXT PRIMARY KEY,
    session_id          TEXT NOT NULL,
    user_id             TEXT NOT NULL,
    project_id          TEXT NOT NULL,
    query               TEXT NOT NULL,
    retrieved_chunk_ids TEXT[] DEFAULT '{}',
    scores              REAL[] DEFAULT '{}',
    latency_ms          REAL DEFAULT 0,
    created_at          TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_traces_session
    ON retrieval_traces(session_id, created_at);
"""


async def initialize_schema(pool: PgPool) -> None:
    """Create all tables if they do not exist."""
    async with pool.acquire() as conn:
        await conn.execute(_SCHEMA_SQL)
    logger.info("PostgreSQL schema initialised")


# ------------------------------------------------------------------
# Session Store
# ------------------------------------------------------------------

class PgSessionStore:
    """SessionStore backed by PostgreSQL."""

    def __init__(self, pool: PgPool) -> None:
        self._pool = pool

    async def get(self, session_id: str) -> Optional[Session]:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM sessions WHERE session_id = $1", session_id
            )
        return _row_to_session(row) if row else None

    async def set(self, session: Session) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO sessions
                    (session_id, user_id, project_id, agent_type,
                     model, metadata, created_at, updated_at)
                VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $8)
                ON CONFLICT (session_id) DO UPDATE SET
                    agent_type = EXCLUDED.agent_type,
                    model      = EXCLUDED.model,
                    metadata   = EXCLUDED.metadata,
                    updated_at = EXCLUDED.updated_at
                """,
                session.session_id,
                session.user_id,
                session.project_id,
                session.agent_type,
                session.model,
                json.dumps(session.metadata),
                session.created_at,
                session.updated_at,
            )

    async def delete(self, session_id: str) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM sessions WHERE session_id = $1", session_id
            )

    async def list_by_project(self, user_id: str, project_id: str) -> list[Session]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT * FROM sessions
                WHERE user_id = $1 AND project_id = $2
                ORDER BY updated_at DESC
                """,
                user_id,
                project_id,
            )
        return [_row_to_session(r) for r in rows]


# ------------------------------------------------------------------
# Message Store
# ------------------------------------------------------------------

class PgMessageStore:
    """MessageStore backed by PostgreSQL."""

    def __init__(self, pool: PgPool) -> None:
        self._pool = pool

    async def append(self, message: StoredMessage) -> None:
        content = message.content
        if not isinstance(content, str):
            content = json.dumps(content)
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO messages
                    (message_id, session_id, user_id, project_id,
                     role, content, tool_calls, tool_call_id,
                     token_count, metadata, created_at)
                VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7::jsonb,
                        $8, $9, $10::jsonb, $11)
                ON CONFLICT (message_id) DO NOTHING
                """,
                message.message_id,
                message.session_id,
                message.user_id,
                message.project_id,
                message.role,
                content,
                json.dumps(message.tool_calls),
                message.tool_call_id,
                message.token_count,
                json.dumps(message.metadata),
                message.created_at,
            )

    async def get(self, message_id: str) -> Optional[StoredMessage]:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM messages WHERE message_id = $1", message_id
            )
        return _row_to_message(row) if row else None

    async def list_by_session(
        self,
        session_id: str,
        limit: int = 100,
        offset: int = 0,
    ) -> list[StoredMessage]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT * FROM messages
                WHERE session_id = $1
                ORDER BY created_at
                LIMIT $2 OFFSET $3
                """,
                session_id,
                limit,
                offset,
            )
        return [_row_to_message(r) for r in rows]

    async def delete_session_messages(self, session_id: str) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM messages WHERE session_id = $1", session_id
            )


# ------------------------------------------------------------------
# Retrieval Trace Store
# ------------------------------------------------------------------

class PgRetrievalTraceStore:
    """RetrievalTraceStore backed by PostgreSQL."""

    def __init__(self, pool: PgPool) -> None:
        self._pool = pool

    async def log(self, trace: RetrievalTrace) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO retrieval_traces
                    (trace_id, session_id, user_id, project_id,
                     query, retrieved_chunk_ids, scores, latency_ms, created_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                ON CONFLICT (trace_id) DO NOTHING
                """,
                trace.trace_id,
                trace.session_id,
                trace.user_id,
                trace.project_id,
                trace.query,
                trace.retrieved_chunk_ids,
                trace.scores,
                trace.latency_ms,
                trace.created_at,
            )

    async def list_by_session(
        self, session_id: str, limit: int = 50
    ) -> list[RetrievalTrace]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT * FROM retrieval_traces
                WHERE session_id = $1
                ORDER BY created_at DESC
                LIMIT $2
                """,
                session_id,
                limit,
            )
        return [_row_to_trace(r) for r in rows]


# ------------------------------------------------------------------
# Row → Model helpers
# ------------------------------------------------------------------

def _row_to_session(row: Any) -> Session:
    metadata = row["metadata"] or {}
    if isinstance(metadata, str):
        metadata = json.loads(metadata)
    return Session(
        session_id=row["session_id"],
        user_id=row["user_id"],
        project_id=row["project_id"],
        agent_type=row["agent_type"] or "default",
        model=row["model"],
        metadata=metadata,
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_message(row: Any) -> StoredMessage:
    content = row["content"]
    if isinstance(content, str):
        try:
            content = json.loads(content)
        except json.JSONDecodeError:
            pass

    tool_calls = row["tool_calls"] or []
    if isinstance(tool_calls, str):
        tool_calls = json.loads(tool_calls)

    metadata = row["metadata"] or {}
    if isinstance(metadata, str):
        metadata = json.loads(metadata)

    return StoredMessage(
        message_id=row["message_id"],
        session_id=row["session_id"],
        user_id=row["user_id"],
        project_id=row["project_id"],
        role=row["role"],
        content=content,
        tool_calls=tool_calls,
        tool_call_id=row["tool_call_id"],
        token_count=row["token_count"] or 0,
        metadata=metadata,
        created_at=row["created_at"],
    )


def _row_to_trace(row: Any) -> RetrievalTrace:
    return RetrievalTrace(
        trace_id=row["trace_id"],
        session_id=row["session_id"],
        user_id=row["user_id"],
        project_id=row["project_id"],
        query=row["query"],
        retrieved_chunk_ids=list(row["retrieved_chunk_ids"] or []),
        scores=list(row["scores"] or []),
        latency_ms=float(row["latency_ms"] or 0.0),
        created_at=row["created_at"],
    )
