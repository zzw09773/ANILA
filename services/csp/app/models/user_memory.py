"""User-scoped memory ORM models.

Two tables, both per-user, both wiped on user-delete:

* ``UserFact`` — small structured key/value facts (name, role,
  long-term preferences). Read on every chat completion to inject a
  short "user background" block into the system prompt.
* ``ConversationMemoryChunk`` — per-message embedding for cross-
  conversation semantic recall. Written async after each turn,
  retrieved synchronously before the next.

The ``halfvec(4000)`` embedding column has no first-class SQLAlchemy
type in stock pgvector-python; we declare it as a generic ``Text``
here so the ORM can READ rows back, and use raw SQL for writes /
similarity queries (mirrors how ingestion-worker handles
``document_chunks.embedding`` via asyncpg).

See migration ``0030_add_user_memory.py`` for column-level rationale.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship

from app.database import Base


class UserFact(Base):
    """A single piece of long-term, structured knowledge about a user.

    Examples (illustrative; never seed these as defaults):
      key="<identity_attr>", value="<concrete_value>"
      key="<role_or_position>", value="<concrete_value>"
      key="<preference.<topic>>", value="<concrete_value>"

    Fact extraction is performed by the platform LLM after each turn
    (see ``memory_service.persist_turn``) and the result is upserted
    on ``(user_id, key)`` — newer extractions overwrite older ones.
    """

    __tablename__ = "user_facts"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    key = Column(String(120), nullable=False)
    value = Column(Text, nullable=False)
    source_conversation_id = Column(
        Integer,
        ForeignKey("conversations.id", ondelete="SET NULL"),
        nullable=True,
    )
    # message_id is a pointer into the conversations.messages JSON blob;
    # not a FK because messages aren't a first-class table.
    source_message_id = Column(Integer, nullable=True)
    confidence = Column(Float, nullable=False, default=1.0)
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    user = relationship("User", foreign_keys=[user_id])

    __table_args__ = (
        UniqueConstraint("user_id", "key", name="uq_user_facts_user_key"),
    )


class ConversationMemoryChunk(Base):
    """One embedded message slice for cross-conversation RAG recall.

    Written by ``memory_service.persist_turn`` once per role per turn
    (one row for the user message, one row for the assistant message).
    Retrieved by ``memory_service.retrieve_relevant_chunks`` via raw
    SQL because halfvec similarity ops aren't expressible through the
    SQLAlchemy expression language.

    ``is_encrypted`` is set TRUE when the originating conversation's
    target agent had ``requires_encryption=true``. Retrieval surfaces
    this flag; the caller is responsible for latching the consuming
    conversation into encrypted state when it's True (P3 wiring).
    """

    __tablename__ = "conversation_memory_chunks"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    conversation_id = Column(
        Integer,
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
    )
    message_id = Column(Integer, nullable=True)
    role = Column(String(20), nullable=False)
    content = Column(Text, nullable=False)
    # Stored as halfvec(4000) at the SQL layer; SQLAlchemy can't bind
    # halfvec literals so writes go through raw SQL. Reads via ORM see
    # the str representation, which is fine for the UI's "what's
    # remembered" panel — RAG queries use raw SQL anyway.
    embedding = Column(Text, nullable=False)
    is_encrypted = Column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    user = relationship("User", foreign_keys=[user_id])
    conversation = relationship("Conversation", foreign_keys=[conversation_id])
