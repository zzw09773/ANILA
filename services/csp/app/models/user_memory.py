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
    CheckConstraint,
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


# SQLite autoincrements only for the literal INTEGER PRIMARY KEY spelling;
# production PostgreSQL still receives BIGINT/BIGSERIAL through this variant.
MemoryBigInt = BigInteger().with_variant(Integer, "sqlite")


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

    id = Column(MemoryBigInt, primary_key=True, autoincrement=True)
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
    # Gate 2 G3: memory is classification-bearing data.  These fields are
    # required on every row; the migration backfills legacy rows from their
    # source conversation and uses TOP SECRET for an unverifiable source.
    classification_level = Column(String(20), nullable=False)
    classification_source = Column(String(80), nullable=False)
    source_task_id = Column(
        Integer,
        ForeignKey("tasks.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    source_snapshot_id = Column(
        Integer,
        ForeignKey("source_snapshots.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
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
        CheckConstraint(
            "classification_level IN ('無機密', '營業秘密', '機密', '極機密', '絕對機密')",
            name="ck_user_facts_classification_level",
        ),
        CheckConstraint(
            "length(trim(classification_source)) > 0",
            name="ck_user_facts_classification_source",
        ),
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
    conversation when it's True (P3 wiring).

    ⚠ 命名警告(W1-3):``is_encrypted`` 與 ``requires_encryption`` 都**不代表
    任何加密**。這一列的內容以明文存在 Postgres 裡(平台無 at-rest 加密:
    ``pgcrypto|LUKS|dm-crypt|TDE`` 全 repo grep=0)。旗標的真實語意是「來源
    對話已被單向鎖定密等」,消費端因此必須跟著鎖定。欄位名為相容性保留
    (改名是 schema 事務,掛 C5 legacy ledger),但**人看得到的字一律用
    「密等鎖定(latch)」**,不得寫成加密。
    """

    __tablename__ = "conversation_memory_chunks"
    __table_args__ = (
        CheckConstraint(
            "classification_level IN ('無機密', '營業秘密', '機密', '極機密', '絕對機密')",
            name="ck_conversation_memory_chunks_classification_level",
        ),
        CheckConstraint(
            "length(trim(classification_source)) > 0",
            name="ck_conversation_memory_chunks_classification_source",
        ),
    )

    id = Column(MemoryBigInt, primary_key=True, autoincrement=True)
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
    classification_level = Column(String(20), nullable=False)
    classification_source = Column(String(80), nullable=False)
    source_task_id = Column(
        Integer,
        ForeignKey("tasks.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    source_snapshot_id = Column(
        Integer,
        ForeignKey("source_snapshots.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    user = relationship("User", foreign_keys=[user_id])
    conversation = relationship("Conversation", foreign_keys=[conversation_id])


class UserFactRequiredCompartment(Base):
    """Compartment requirements inherited by a structured fact."""

    __tablename__ = "user_fact_required_compartments"

    fact_id = Column(
        MemoryBigInt,
        ForeignKey("user_facts.id", ondelete="CASCADE"),
        primary_key=True,
    )
    compartment_id = Column(
        Integer,
        ForeignKey("security_compartments.id", ondelete="RESTRICT"),
        primary_key=True,
    )


class MemoryChunkRequiredCompartment(Base):
    """Compartment requirements inherited by a recalled message chunk."""

    __tablename__ = "memory_chunk_required_compartments"

    chunk_id = Column(
        MemoryBigInt,
        ForeignKey("conversation_memory_chunks.id", ondelete="CASCADE"),
        primary_key=True,
    )
    compartment_id = Column(
        Integer,
        ForeignKey("security_compartments.id", ondelete="RESTRICT"),
        primary_key=True,
    )


class UserFactSourceCollection(Base):
    """Collection provenance whose NTK must remain valid for a fact."""

    __tablename__ = "user_fact_source_collections"

    fact_id = Column(
        MemoryBigInt,
        ForeignKey("user_facts.id", ondelete="CASCADE"),
        primary_key=True,
    )
    collection_id = Column(
        Integer,
        ForeignKey("ingestion_collections.id", ondelete="RESTRICT"),
        primary_key=True,
    )


class MemoryChunkSourceCollection(Base):
    """Collection provenance whose NTK must remain valid for a chunk."""

    __tablename__ = "memory_chunk_source_collections"

    chunk_id = Column(
        MemoryBigInt,
        ForeignKey("conversation_memory_chunks.id", ondelete="CASCADE"),
        primary_key=True,
    )
    collection_id = Column(
        Integer,
        ForeignKey("ingestion_collections.id", ondelete="RESTRICT"),
        primary_key=True,
    )
