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
    Boolean,
    CheckConstraint,
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
    text,
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
    are platform-shared resources owned by the user who created them;
    any agent backend can configure ``RAG_COLLECTION_ID`` to point at
    one. The platform stops caring which agent uses which collection.

    Engine-level isolation moved with it: the RLS policy on
    ``document_chunks`` is now keyed on ``anila.collection_id`` GUC
    instead of ``anila.agent_id``. Agent backends issue
    ``SET LOCAL anila.collection_id = N`` before retrieval queries.

    Counter columns (``document_count`` / ``chunk_count`` /
    ``bytes_stored``) are denormalized for fast list-page rendering.
    The worker updates them inside the same transaction as the chunk
    insert; nothing else writes them.
    """

    __tablename__ = "ingestion_collections"
    __table_args__ = (
        CheckConstraint(
            "embedding_fingerprint ~ '^sha256:[0-9a-f]{64}$'",
            name="ck_ingestion_collections_embedding_fingerprint",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "lifecycle_state IN ('active','archived','erase_due','erased')",
            name="ck_ingestion_collections_lifecycle_state",
        ),
        CheckConstraint(
            "(lifecycle_state = 'active' AND archived_at IS NULL AND erased_at IS NULL) OR "
            "(lifecycle_state = 'archived' AND archived_at IS NOT NULL "
            "AND erase_due_at IS NOT NULL AND erased_at IS NULL) OR "
            "(lifecycle_state = 'erase_due' AND erase_due_at IS NOT NULL "
            "AND erased_at IS NULL) OR "
            "(lifecycle_state = 'erased' AND erased_at IS NOT NULL)",
            name="ck_ingestion_collections_lifecycle_timestamps",
        ),
        CheckConstraint(
            "(legal_hold = true AND legal_hold_reason IS NOT NULL "
            "AND length(legal_hold_reason) > 0) OR "
            "(legal_hold = false AND legal_hold_reason IS NULL)",
            name="ck_ingestion_collections_legal_hold_reason",
        ),
        CheckConstraint(
            "document_count >= 0 AND chunk_count >= 0 AND bytes_stored >= 0 "
            "AND image_count >= 0 AND artifact_count >= 0",
            name="ck_ingestion_collections_counters_nonnegative",
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(200), nullable=False)
    description = Column(Text, nullable=True)
    chunking_config = Column(JSONValue, nullable=False)
    embedding_model = Column(String(200), nullable=False)
    embedding_fingerprint = Column(CHAR(71), nullable=False)
    embedding_dim = Column(Integer, nullable=False)
    status = Column(String(20), nullable=False, default="active")
    document_count = Column(Integer, nullable=False, default=0)
    chunk_count = Column(Integer, nullable=False, default=0)
    bytes_stored = Column(BigInteger, nullable=False, default=0)
    image_count = Column(Integer, nullable=False, default=0, server_default="0")
    artifact_count = Column(Integer, nullable=False, default=0, server_default="0")
    lifecycle_state = Column(String(20), nullable=False, default="active",
                             server_default="active", index=True)
    archive_due_at = Column(DateTime(timezone=True), nullable=True)
    archived_at = Column(DateTime(timezone=True), nullable=True)
    erase_due_at = Column(DateTime(timezone=True), nullable=True, index=True)
    erased_at = Column(DateTime(timezone=True), nullable=True)
    legal_hold = Column(Boolean, nullable=False, default=False,
                        server_default="false")
    legal_hold_reason = Column(String(500), nullable=True)
    # Owner (NOT NULL post-Sprint-4 — see migration 0019). Same person
    # who created the collection; ON DELETE RESTRICT would lock user
    # deletes, so we stay with default and rely on app-layer reassign
    # if a user is offboarded.
    created_by = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=False
    )
    # ── 五級分類共通欄位(doc 08 §5,Slice 3a;backfill floor=無機密,
    # 最終等級以人工分類盤點為準,doc 08 §15)────────────────────────────
    classification_level = Column(
        String(20), nullable=False, default="無機密", server_default="無機密"
    )
    classification_latched_at = Column(DateTime, nullable=True)
    classification_source = Column(String(50), nullable=True)
    classification_event_id = Column(
        Integer,
        ForeignKey("classification_events.id", ondelete="SET NULL"),
        nullable=True,
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
        # Composite UNIQUE so document_relations can hang a composite FK on
        # (collection_id, id) — that FK is what enforces same-collection edges
        # at the DB layer (migration 0039, codex #1). Mirrors the migration so
        # the SQLite create_all test path produces a valid FK target.
        UniqueConstraint(
            "collection_id", "id", name="uq_ingestion_documents_collection_id_id"
        ),
        ForeignKeyConstraint(
            ["active_generation_id", "id", "collection_id"],
            [
                "ingestion_document_generations.id",
                "ingestion_document_generations.document_id",
                "ingestion_document_generations.collection_id",
            ],
            ondelete="RESTRICT",
            name="fk_ingestion_document_active_generation",
            use_alter=True,
        ),
        CheckConstraint(
            "availability_status IN ('unavailable','available')",
            name="ck_ingestion_documents_availability",
        ),
        CheckConstraint(
            "processing_stage IN ('pending','parsing','chunking','embedding',"
            "'staging','complete','failed')",
            name="ck_ingestion_documents_processing_stage",
        ),
        CheckConstraint(
            "lifecycle_state IN ('active','archived','erase_due','erased')",
            name="ck_ingestion_documents_lifecycle_state",
        ),
        CheckConstraint(
            "(lifecycle_state = 'active' AND archived_at IS NULL AND erased_at IS NULL) OR "
            "(lifecycle_state = 'archived' AND archived_at IS NOT NULL "
            "AND erase_due_at IS NOT NULL AND erased_at IS NULL) OR "
            "(lifecycle_state = 'erase_due' AND erase_due_at IS NOT NULL "
            "AND erased_at IS NULL) OR "
            "(lifecycle_state = 'erased' AND erased_at IS NOT NULL)",
            name="ck_ingestion_documents_lifecycle_timestamps",
        ),
        CheckConstraint(
            "(legal_hold = true AND legal_hold_reason IS NOT NULL "
            "AND length(legal_hold_reason) > 0) OR "
            "(legal_hold = false AND legal_hold_reason IS NULL)",
            name="ck_ingestion_documents_legal_hold_reason",
        ),
        CheckConstraint(
            "chunk_count >= 0 AND (bytes IS NULL OR bytes >= 0)",
            name="ck_ingestion_documents_counters_nonnegative",
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
    availability_status = Column(
        String(20), nullable=False, default="unavailable",
        server_default="unavailable",
    )
    processing_stage = Column(
        String(20), nullable=False, default="pending", server_default="pending"
    )
    active_generation_id = Column(BigInteger, nullable=True)
    lifecycle_state = Column(String(20), nullable=False, default="active",
                             server_default="active", index=True)
    archive_due_at = Column(DateTime(timezone=True), nullable=True)
    archived_at = Column(DateTime(timezone=True), nullable=True)
    erase_due_at = Column(DateTime(timezone=True), nullable=True, index=True)
    erased_at = Column(DateTime(timezone=True), nullable=True)
    legal_hold = Column(Boolean, nullable=False, default=False,
                        server_default="false")
    legal_hold_reason = Column(String(500), nullable=True)
    uploaded_by = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    # ── 五級分類共通欄位(doc 08 §5,Slice 3a)────────────────────────────
    classification_level = Column(
        String(20), nullable=False, default="無機密", server_default="無機密"
    )
    classification_latched_at = Column(DateTime, nullable=True)
    classification_source = Column(String(50), nullable=True)
    classification_event_id = Column(
        Integer,
        ForeignKey("classification_events.id", ondelete="SET NULL"),
        nullable=True,
    )
    uploaded_at = Column(
        DateTime, nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    indexed_at = Column(DateTime, nullable=True)

    collection = relationship("IngestionCollection", back_populates="documents")


class IngestionDocumentGeneration(Base):
    """Immutable, model-bound generation activated atomically per document."""

    __tablename__ = "ingestion_document_generations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["collection_id", "document_id"],
            ["ingestion_documents.collection_id", "ingestion_documents.id"],
            ondelete="CASCADE",
            name="fk_ingestion_generation_document_collection",
        ),
        UniqueConstraint(
            "document_id", "generation_number",
            name="uq_ingestion_generation_document_number",
        ),
        UniqueConstraint(
            "id", "document_id", "collection_id",
            name="uq_ingestion_generation_identity_scope",
        ),
        CheckConstraint(
            "status IN ('staging','active','retired','failed')",
            name="ck_ingestion_generation_status",
        ),
        CheckConstraint(
            "embedding_dim > 0 AND chunk_count >= 0",
            name="ck_ingestion_generation_counts",
        ),
        CheckConstraint(
            "embedding_fingerprint ~ '^sha256:[0-9a-f]{64}$'",
            name="ck_ingestion_generation_fingerprint",
        ).ddl_if(dialect="postgresql"),
        Index(
            "uq_ingestion_generation_one_active", "document_id",
            unique=True, postgresql_where=text("status = 'active'"),
        ),
        Index(
            "ix_ingestion_generation_collection_status", "collection_id", "status"
        ),
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    document_id = Column(Integer, nullable=False)
    collection_id = Column(Integer, nullable=False)
    generation_number = Column(Integer, nullable=False)
    source_ingestion_job_id = Column(
        Integer,
        ForeignKey(
            "ingestion_jobs.id", ondelete="RESTRICT", use_alter=True,
            name="fk_ingestion_generation_source_job",
        ),
        nullable=True, unique=True,
    )
    status = Column(String(20), nullable=False)
    embedding_model = Column(String(200), nullable=False)
    embedding_fingerprint = Column(CHAR(71), nullable=False)
    embedding_dim = Column(Integer, nullable=False)
    chunk_count = Column(Integer, nullable=False, default=0)
    created_at = Column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    activated_at = Column(DateTime(timezone=True), nullable=True)
    retired_at = Column(DateTime(timezone=True), nullable=True)
    failed_at = Column(DateTime(timezone=True), nullable=True)


class SimilarityRecomputeRequest(Base):
    """Durable collection-level debounce/lease row owned by I7."""

    __tablename__ = "similarity_recompute_requests"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','running')", name="ck_similarity_recompute_status"
        ),
        CheckConstraint(
            "request_seq >= 1 AND (claimed_seq IS NULL OR claimed_seq >= 1)",
            name="ck_similarity_recompute_sequences",
        ),
        CheckConstraint(
            "(status='pending' AND claimed_seq IS NULL AND lease_token IS NULL "
            "AND lease_expires_at IS NULL) OR "
            "(status='running' AND claimed_seq IS NOT NULL AND lease_token IS NOT NULL "
            "AND lease_expires_at IS NOT NULL)",
            name="ck_similarity_recompute_lease_state",
        ),
        Index("ix_similarity_recompute_dispatch", "status", "not_before"),
    )

    collection_id = Column(
        Integer, ForeignKey("ingestion_collections.id", ondelete="CASCADE"),
        primary_key=True,
    )
    status = Column(String(20), nullable=False)
    request_seq = Column(BigInteger, nullable=False)
    claimed_seq = Column(BigInteger, nullable=True)
    requested_at = Column(DateTime(timezone=True), nullable=False)
    not_before = Column(DateTime(timezone=True), nullable=False)
    lease_token = Column(String(64), nullable=True)
    lease_expires_at = Column(DateTime(timezone=True), nullable=True)
    last_error = Column(Text, nullable=True)
    updated_at = Column(DateTime(timezone=True), nullable=False)


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
    created_at = Column(
        DateTime, nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)


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
    last_used_at = Column(DateTime, nullable=True)
    created_at = Column(
        DateTime, nullable=False, default=lambda: datetime.now(timezone.utc)
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
    __table_args__ = (
        CheckConstraint(
            "status IN ('dispatch_pending','queued','running','retry_wait',"
            "'succeeded','failed','cancelled','dead_letter')",
            name="ck_ingestion_jobs_status",
        ),
        CheckConstraint(
            "attempt_count >= 0 AND max_attempts >= 1 "
            "AND attempt_count <= max_attempts",
            name="ck_ingestion_jobs_attempt_bounds",
        ),
        CheckConstraint(
            "(status='running' AND lease_token IS NOT NULL "
            "AND lease_expires_at IS NOT NULL AND heartbeat_at IS NOT NULL) OR "
            "(status<>'running' AND lease_token IS NULL "
            "AND lease_expires_at IS NULL)",
            name="ck_ingestion_jobs_lease_state",
        ),
    )

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
    attempt_count = Column(Integer, nullable=False, default=0)
    max_attempts = Column(Integer, nullable=False, default=3)
    lease_token = Column(String(64), nullable=True)
    lease_expires_at = Column(DateTime(timezone=True), nullable=True)
    heartbeat_at = Column(DateTime(timezone=True), nullable=True)
    next_attempt_at = Column(DateTime(timezone=True), nullable=True)
    failure_kind = Column(String(20), nullable=True)
    retryable = Column(Boolean, nullable=True)
    dead_lettered_at = Column(DateTime(timezone=True), nullable=True)


class IngestionOutbox(Base):
    """Durable intent to publish an ingestion job to Arq.

    The document, ``IngestionJob``, and this row are committed together.  A
    background relay owns the Redis side effect afterwards, so a Redis outage
    can never turn an accepted upload into a document with no durable work
    intent.  ``arq_job_id`` is deterministic and unique, making relay replay
    safe after a crash between enqueue and the published acknowledgement.
    """

    __tablename__ = "ingestion_outbox"
    __table_args__ = (
        UniqueConstraint(
            "ingestion_job_id",
            "attempt_number",
            name="uq_ingestion_outbox_job_attempt",
        ),
        CheckConstraint(
            "status IN ('pending', 'dispatching', 'published')",
            name="ck_ingestion_outbox_status",
        ),
        CheckConstraint(
            "(status = 'pending' AND lease_token IS NULL "
            "AND lease_expires_at IS NULL AND published_at IS NULL) OR "
            "(status = 'dispatching' AND lease_token IS NOT NULL "
            "AND lease_expires_at IS NOT NULL AND published_at IS NULL) OR "
            "(status = 'published' AND lease_token IS NULL "
            "AND lease_expires_at IS NULL AND published_at IS NOT NULL)",
            name="ck_ingestion_outbox_state_fields",
        ),
        Index("ix_ingestion_outbox_dispatch", "status", "available_at"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    ingestion_job_id = Column(
        Integer,
        ForeignKey("ingestion_jobs.id", ondelete="CASCADE"),
        nullable=False,
    )
    attempt_number = Column(Integer, nullable=False)
    arq_job_id = Column(String(100), nullable=False, unique=True)
    task_name = Column(String(100), nullable=False, default="ingest_document")
    payload = Column(JSONValue, nullable=False)
    status = Column(String(20), nullable=False, default="pending")
    attempt_count = Column(Integer, nullable=False, default=0)
    available_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    lease_token = Column(String(64), nullable=True)
    lease_expires_at = Column(DateTime(timezone=True), nullable=True)
    last_error = Column(Text, nullable=True)
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    published_at = Column(DateTime(timezone=True), nullable=True)


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
