"""SQLAlchemy ORM models for the ingestion platform.

Mirrors the schema introduced by migration 0014 (``ingestion_collections``,
``ingestion_documents``, ``ingestion_jobs``). The pgvector-backed
``document_chunks`` is intentionally NOT modelled here — that table is
written / read exclusively through ``anila_core.storage.adapters.
AgentScopedPgVectorStore`` so RLS scoping (``SET LOCAL anila.agent_id``)
is impossible to bypass. The CSP backend talks to chunks via the
ingestion-worker SDK, never via SQLAlchemy.

All three tables here are agent-scoped via FK chains rooted at
``ingestion_collections.agent_id``. The CSP API layer enforces the agent
scope in code; the chunks table backs that with RLS at the DB engine.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import CHAR, JSONB
from sqlalchemy.orm import relationship

from app.database import Base


class IngestionCollection(Base):
    """A per-agent grouping of corpora.

    One agent can own multiple collections (e.g. "legal-regs" + "internal-sop")
    — the ``UNIQUE (agent_id, name)`` constraint enforces unique names within
    a single agent's namespace, but allows the same name across agents.

    Counter columns (``document_count`` / ``chunk_count`` / ``bytes_stored``)
    are denormalized for fast list-page rendering. The worker updates them
    inside the same transaction as the chunk insert; nothing else writes them.
    """

    __tablename__ = "ingestion_collections"
    __table_args__ = (
        UniqueConstraint("agent_id", "name", name="uq_collections_agent_name"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    agent_id = Column(
        Integer,
        ForeignKey("agents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name = Column(String(200), nullable=False)
    description = Column(Text, nullable=True)
    chunking_config = Column(JSONB, nullable=False)
    embedding_model = Column(String(200), nullable=False)
    embedding_dim = Column(Integer, nullable=False)
    status = Column(String(20), nullable=False, default="active")
    document_count = Column(Integer, nullable=False, default=0)
    chunk_count = Column(Integer, nullable=False, default=0)
    bytes_stored = Column(BigInteger, nullable=False, default=0)
    created_by = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at = Column(
        DateTime, nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    updated_at = Column(
        DateTime, nullable=False, default=lambda: datetime.now(timezone.utc)
    )

    documents = relationship(
        "IngestionDocument",
        back_populates="collection",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class IngestionDocument(Base):
    """One uploaded file within a collection.

    SHA-256 deduplicates within a single collection — re-uploading the same
    content is a no-op (the API surfaces "already indexed" rather than
    creating a duplicate row). Status tracks the parse → chunk → embed →
    indexed pipeline; ``error_message`` carries the structured
    ``IngestionError.user_message`` when status='failed'.

    ``storage_path`` records where the original blob lives (worker-local
    disk for Sprint 1, future object-store key for Sprint 4+).
    """

    __tablename__ = "ingestion_documents"
    __table_args__ = (
        UniqueConstraint(
            "collection_id", "sha256", name="uq_documents_collection_sha256"
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    collection_id = Column(
        Integer,
        ForeignKey("ingestion_collections.id", ondelete="CASCADE"),
        nullable=False,
    )
    filename = Column(String(500), nullable=False)
    sha256 = Column(CHAR(64), nullable=False)
    mime_type = Column(String(200), nullable=True)
    bytes = Column(BigInteger, nullable=True)
    storage_path = Column(String(1000), nullable=True)
    status = Column(String(20), nullable=False, default="pending")
    chunk_count = Column(Integer, nullable=False, default=0)
    error_message = Column(Text, nullable=True)
    uploaded_by = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    uploaded_at = Column(
        DateTime, nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    indexed_at = Column(DateTime, nullable=True)

    collection = relationship("IngestionCollection", back_populates="documents")


class AgentLlmCredential(Base):
    """Dev-supplied LLM credential for the Chunking Evaluator's judge.

    See ``app/services/credential_crypto.py`` for the encryption shape
    (AES-256-GCM with PBKDF2-derived master key).

    The ``api_key`` plaintext is NEVER read from this table directly by
    application code outside the evaluator handler — every consumer
    goes through ``decrypt_credential(...)`` which keeps the decrypted
    string only as long as the outbound HTTP call needs it.
    """

    __tablename__ = "agent_llm_credentials"
    __table_args__ = (
        UniqueConstraint("agent_id", "name", name="uq_agent_llm_credentials_name"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    agent_id = Column(
        Integer,
        ForeignKey("agents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name = Column(String(200), nullable=False)
    endpoint_url = Column(String(1000), nullable=False)
    model_name = Column(String(200), nullable=False)
    api_key_encrypted = Column(LargeBinary, nullable=False)
    api_key_nonce = Column(LargeBinary, nullable=False)
    api_key_tag = Column(LargeBinary, nullable=False)
    last_used_at = Column(DateTime, nullable=True)
    created_by = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at = Column(
        DateTime, nullable=False, default=lambda: datetime.now(timezone.utc)
    )


class IngestionJob(Base):
    """Async job tracking for the ingestion-worker.

    Sprint 1 scope leaves ``job_type='ingest'`` only; later sprints add
    'reindex' / 'evaluate' / 'apply_strategy'. ``arq_job_id`` correlates
    back to Redis-side queue state for status polling and cancellation.
    """

    __tablename__ = "ingestion_jobs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    arq_job_id = Column(String(100), nullable=True, unique=True)
    collection_id = Column(
        Integer,
        ForeignKey("ingestion_collections.id", ondelete="CASCADE"),
        nullable=False,
    )
    document_id = Column(
        Integer,
        ForeignKey("ingestion_documents.id", ondelete="CASCADE"),
        nullable=True,
    )
    job_type = Column(String(30), nullable=False)
    status = Column(String(20), nullable=False, default="queued")
    progress_pct = Column(SmallInteger, nullable=False, default=0)
    progress_message = Column(Text, nullable=True)
    error_code = Column(String(64), nullable=True)
    error_message = Column(Text, nullable=True)
    enqueued_by = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    enqueued_at = Column(
        DateTime, nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
