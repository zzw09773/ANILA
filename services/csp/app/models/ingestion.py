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
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    JSON,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, CHAR, JSONB
from sqlalchemy.orm import relationship

from app.database import Base


# Production runs Postgres so JSONB stays as the canonical column type
# (indexable + operator support). SQLite-backed pytest fixtures can't
# compile JSONB → fall back to plain JSON; ARRAY → JSON list (read-only
# convenience, test-time only). Same with_variant pattern as the other
# ORM modules (message.py / agent.py / handoff.py / platform_link.py).
JSONValue = JSON().with_variant(JSONB(), "postgresql")
IntListValue = JSON().with_variant(ARRAY(Integer), "postgresql")


class IngestionCollection(Base):
    """A user-owned corpus grouping (Sprint 4 first-class refactor).

    Sprint 1–3 scoped collections to ``agent_id`` (one collection per
    agent). Sprint 4 (migration 0019) drops that coupling — collections
    are owned by the user who created them; any agent backend can
    configure ``RAG_COLLECTION_ID`` to point at one.

    ``origin`` (migration r1_0029) records which product surface created
    the row (``csp`` / ``anilalm`` / NULL legacy). Same shape as
    ``conversations.origin``: inventory partitioning so CSP project
    corpora and ANILALM personal notebooks do not share a shelf. Not an
    authorization control — ownership, clearance and the classification
    latch are unchanged; retrieval / RLS still key on collection id.

    Engine-level isolation: the RLS policy on ``document_chunks`` is
    keyed on ``anila.collection_id`` GUC. Agent backends issue
    ``SET LOCAL anila.collection_id = N`` before retrieval queries.

    Counter columns (``document_count`` / ``chunk_count`` /
    ``bytes_stored``) are denormalized for fast list-page rendering.
    The worker updates them inside the same transaction as the chunk
    insert; nothing else writes them.
    """

    __tablename__ = "ingestion_collections"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(200), nullable=False)
    description = Column(Text, nullable=True)
    chunking_config = Column(JSONValue, nullable=False)
    embedding_model = Column(String(200), nullable=False)
    embedding_dim = Column(Integer, nullable=False)
    status = Column(String(20), nullable=False, default="active")
    document_count = Column(Integer, nullable=False, default=0)
    chunk_count = Column(Integer, nullable=False, default=0)
    bytes_stored = Column(BigInteger, nullable=False, default=0)
    # Owner (NOT NULL post-Sprint-4 — see migration 0019). Same person
    # who created the collection; ON DELETE RESTRICT would lock user
    # deletes, so we stay with default and rely on app-layer reassign
    # if a user is offboarded.
    created_by = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=False
    )
    # Product surface that created this row. Today's values: 'csp' /
    # 'anilalm' / NULL (pre-r1_0029 legacy — still listed for the owner
    # under every surface so existing corpora are never orphaned).
    origin = Column(String(32), nullable=True)
    # ── 四級分類共通欄位(doc 08 §5,Slice 3a;backfill floor=無機密,
    # 最終等級以人工分類盤點為準,doc 08 §15)────────────────────────────
    classification_level = Column(
        String(20), nullable=False, default="無機密", server_default="無機密"
    )
    classification_latched_at = Column(DateTime(timezone=True), nullable=True)
    classification_source = Column(String(50), nullable=True)
    classification_event_id = Column(
        Integer,
        ForeignKey("classification_events.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    updated_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
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
        # Composite UNIQUE so document_relations can hang a composite FK on
        # (collection_id, id) — that FK is what enforces same-collection edges
        # at the DB layer (migration 0039, codex #1). Mirrors the migration so
        # the SQLite create_all test path produces a valid FK target.
        UniqueConstraint(
            "collection_id", "id", name="uq_ingestion_documents_collection_id_id"
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    collection_id = Column(
        Integer,
        ForeignKey("ingestion_collections.id", ondelete="CASCADE"),
        nullable=False,
    )
    filename = Column(String(500), nullable=False)
    # Human-facing regulation name + its normalized form (NFKC, brackets/space
    # stripped). ``normalized_title`` is the join key citation resolution uses to
    # match a ``target_title`` back to a document; ``filename`` alone is useless
    # for ROC regs that are named by date / 字號 (migration 0039).
    title = Column(String(500), nullable=True)
    normalized_title = Column(String(500), nullable=True)
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
    # ── 四級分類共通欄位(doc 08 §5,Slice 3a)────────────────────────────
    classification_level = Column(
        String(20), nullable=False, default="無機密", server_default="無機密"
    )
    classification_latched_at = Column(DateTime(timezone=True), nullable=True)
    classification_source = Column(String(50), nullable=True)
    classification_event_id = Column(
        Integer,
        ForeignKey("classification_events.id", ondelete="SET NULL"),
        nullable=True,
    )
    uploaded_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    indexed_at = Column(DateTime(timezone=True), nullable=True)

    collection = relationship("IngestionCollection", back_populates="documents")


class IngestionEvalRun(Base):
    """One Chunking Evaluator run.

    The full input set (sample doc IDs + strategies + queries) lives on
    the row so the results page can render without re-fetching pieces.
    ``results`` holds the per-strategy metrics dict produced by the
    worker handler at completion time.
    """

    __tablename__ = "ingestion_eval_runs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    collection_id = Column(
        Integer,
        ForeignKey("ingestion_collections.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name = Column(String(200), nullable=False)
    sample_document_ids = Column(IntListValue, nullable=False)
    strategies_tried = Column(JSONValue, nullable=False)
    queries = Column(JSONValue, nullable=False)
    judge_llm_config = Column(JSONValue, nullable=True)
    arq_job_id = Column(String(100), nullable=True, unique=True)
    status = Column(String(20), nullable=False, default="queued")
    results = Column(JSONValue, nullable=True)
    recommended_strategy = Column(String(64), nullable=True)
    error_code = Column(String(64), nullable=True)
    error_message = Column(Text, nullable=True)
    created_by = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    started_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)


class UserLlmCredential(Base):
    """User-owned LLM credential (Sprint 4 rename: was AgentLlmCredential).

    Devs register their judge / external LLM endpoints once; the
    credential is reusable across every collection owned by that
    same user. The Chunking Evaluator's judge step looks creds up
    by ``(created_by, name)``.

    Pre-Sprint-4 the FK was ``agent_id``; migration 0019 rebases onto
    ``users.id`` (= ``created_by``) and renames the table to
    ``user_llm_credentials``.

    See ``app/services/credential_crypto.py`` for the encryption shape
    (AES-256-GCM with PBKDF2-derived master key). The ``api_key``
    plaintext never leaves the encrypt / decrypt helpers.
    """

    __tablename__ = "user_llm_credentials"
    __table_args__ = (
        UniqueConstraint("created_by", "name", name="uq_user_llm_credentials_name"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    created_by = Column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name = Column(String(200), nullable=False)
    endpoint_url = Column(String(1000), nullable=False)
    model_name = Column(String(200), nullable=False)
    api_key_encrypted = Column(LargeBinary, nullable=False)
    api_key_nonce = Column(LargeBinary, nullable=False)
    api_key_tag = Column(LargeBinary, nullable=False)
    last_used_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )


# Back-compat alias so existing imports survive one transition cycle.
# Sprint 5 will drop this. New code should import ``UserLlmCredential``.
AgentLlmCredential = UserLlmCredential


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
    enqueued_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    started_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)


class DocumentRelation(Base):
    """A directed edge between documents in the same collection.

    The shared substrate for cross-document relations (design v2 §3): both
    A (rule/manual edges from the regex citation extractor or a human) and a
    future B (LLM/GraphRAG edges) write the SAME table, distinguished by
    ``source`` (rule / manual / llm) and graded by ``confidence``. Retrieval
    expands along edges once, regardless of who authored them.

    Resolution is order-independent (design v2 §7): an edge is recorded with
    ``target_ref`` (the normalized "title [+ article]" the source text cited)
    even before the target document exists; ``dst_document_id`` is back-filled
    when a matching ``normalized_title`` is later ingested, and nulled again on
    SET NULL if that target is deleted. ``target_ref`` is the durable key.

    ``dst_chunk_id`` is reserved for Phase 1.5 (chunk-level edges); it carries
    no FK yet. No ORM ``relationship()`` is declared back to IngestionDocument
    on purpose: ``collection_id`` participates in both the src and dst composite
    FKs, which would make a relationship overlap-ambiguous — the API layer
    queries edges explicitly instead.
    """

    __tablename__ = "document_relations"
    __table_args__ = (
        # same-collection enforced at the DB layer via composite FK (codex #1)
        ForeignKeyConstraint(
            ["collection_id", "src_document_id"],
            ["ingestion_documents.collection_id", "ingestion_documents.id"],
            ondelete="CASCADE",
            name="fk_docrel_src",
        ),
        ForeignKeyConstraint(
            ["collection_id", "dst_document_id"],
            ["ingestion_documents.collection_id", "ingestion_documents.id"],
            ondelete="SET NULL",
            name="fk_docrel_dst",
        ),
        # rule / manual / llm can coexist for the same logical edge (codex #9)
        UniqueConstraint(
            "collection_id",
            "src_document_id",
            "target_ref",
            "relation_type",
            "source",
            name="uq_document_relations_edge",
        ),
        Index("ix_document_relations_src", "collection_id", "src_document_id"),
        Index("ix_document_relations_dst", "collection_id", "dst_document_id"),
        Index("ix_document_relations_target", "collection_id", "target_ref"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    collection_id = Column(Integer, nullable=False)
    src_document_id = Column(Integer, nullable=False)
    dst_document_id = Column(Integer, nullable=True)
    dst_chunk_id = Column(Integer, nullable=True)  # Phase 1.5 reserved, no FK
    target_ref = Column(String(500), nullable=False)
    relation_type = Column(String(20), nullable=False)
    confidence = Column(Float, nullable=False, server_default="1.0", default=1.0)
    source = Column(String(10), nullable=False)  # rule / manual / llm
    extractor_run_id = Column(String(40), nullable=True)
    evidence = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    created_by_user_id = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
