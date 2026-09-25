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
    # 使用者在記憶頁改過的事實。整理對話時不得用逐字稿裡的舊值蓋掉。
    user_edited = Column(
        Boolean, nullable=False, default=False, server_default="false"
    )
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
    # P4.8: which model produced this vector + its native width. Retrieval
    # filters to the current platform designation; other rows stay for
    # pending recompute.
    embedding_source_model = Column(String(200), nullable=True)
    embedding_native_dim = Column(Integer, nullable=True)
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


class ConversationSummary(Base):
    """一個對話一則摘要。給需要時的 RECALL 搜尋，也給使用者自己刪。

    摘要只描述使用者要什麼、得出什麼結論，不存助理原文。
    ``covered_message_id`` 是這則摘要已經涵蓋的最後一則訊息；
    之後又有合格回合，閒置或開新對話時才重寫。
    """

    __tablename__ = "conversation_summaries"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    conversation_id = Column(
        Integer,
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
    )
    summary = Column(Text, nullable=False)
    covered_message_id = Column(Integer, nullable=True)
    embedding = Column(Text, nullable=True)
    embedding_source_model = Column(String(200), nullable=True)
    embedding_native_dim = Column(Integer, nullable=True)
    is_encrypted = Column(
        Boolean, nullable=False, default=False, server_default="false"
    )
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
    conversation = relationship("Conversation", foreign_keys=[conversation_id])

    __table_args__ = (
        UniqueConstraint(
            "conversation_id", name="uq_conversation_summaries_conversation"
        ),
    )


class MemoryTombstone(Base):
    """使用者刪掉的摘要或事實。閒置整理不得從涵蓋範圍內把內容做回來。

    ``kind`` 是 ``summary`` 或 ``fact``。摘要墓碑記來源對話與已涵蓋的訊息；
    事實墓碑再記 key。``covered_message_id`` 以內的原文不再產生同一筆記憶。
    """

    __tablename__ = "memory_tombstones"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    conversation_id = Column(
        Integer,
        ForeignKey("conversations.id", ondelete="SET NULL"),
        nullable=True,
    )
    kind = Column(String(20), nullable=False)
    fact_key = Column(String(120), nullable=True)
    covered_message_id = Column(Integer, nullable=True)
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    user = relationship("User", foreign_keys=[user_id])
    conversation = relationship("Conversation", foreign_keys=[conversation_id])


class MemoryRefreshLease(Base):
    """一個對話同時只許一個 worker 整理。

    ``claimed_until`` 是 unix 秒。過期後別的 worker 才能認領。
    ``claim_token`` 留到下一輪認領，避免舊結果在租約讓出後把新摘要蓋掉。
    """

    __tablename__ = "memory_refresh_leases"

    conversation_id = Column(
        Integer,
        ForeignKey("conversations.id", ondelete="CASCADE"),
        primary_key=True,
    )
    claim_token = Column(String(64), nullable=False)
    claimed_until = Column(Integer, nullable=False)
