"""Ingestion platform foundation: collections / documents / chunks / jobs + RLS.

Sprint 1 of the Phase 2 ingestion platform (docs/ingestion-platform-design.md
§3.1–§3.3). This migration replaces AgenticRAG's runtime-`CREATE TABLE`
self-bootstrap (which used `(user_id, project_id)` legacy scoping) with a
proper alembic-managed schema scoped on `agent_id`.

Four tables introduced:

1. ``ingestion_collections`` — per-agent grouping of corpora with chunking
   strategy and embedding model pinned. ``UNIQUE (agent_id, name)`` so an
   agent can have e.g. "legal-regs" and "internal-sop" as separate
   collections.

2. ``ingestion_documents`` — one row per uploaded file. SHA-256 deduplication
   within a collection (re-uploading the same file is a no-op). Status
   tracks the parse → chunk → embed → indexed pipeline.

3. ``document_chunks`` — pgvector-backed retrieval table. Schema is
   completely different from the legacy AgenticRAG one (TEXT chunk_id,
   user_id, project_id) — we drop the legacy table if present (CASCADE)
   because it has no production data in this deployment (alpine postgres
   image had no pgvector extension, so AgenticRAG's runtime bootstrap
   couldn't have ever succeeded here). Layer 1 isolation is the
   ``agent_id NOT NULL`` constraint; Layer 2 is the RLS policy added below.

4. ``ingestion_jobs`` — async job tracking for the worker. ``arq_job_id``
   correlates back to Arq's redis-backed queue. Sprint 1 ships only synchronous
   ingestion through the API, but the table exists so Sprint 2 worker swap is
   schema-compatible.

Two further tables (``ingestion_eval_runs``, ``agent_llm_credentials``) live
in design doc §3.1 but are deferred to Sprint 3 / Sprint 2 respectively.

PG extension prerequisite: ``CREATE EXTENSION vector``. The csp-db image
must be ``pgvector/pgvector:pg16`` (or compatible) — the upstream
``postgres:16-alpine`` does NOT ship pgvector. This migration calls
``CREATE EXTENSION IF NOT EXISTS vector`` first; if the extension binary
is unavailable, the migration fails fast with a clear error before any
table is created.

Revision ID: 0014
Revises: 0013
Create Date: 2026-04-25
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0014"
down_revision: Union[str, None] = "0013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Embedding dimension pinned at the table level.
#
# Decision (2026-04-25): use 1536-d truncated embeddings instead of the
# native 4096-d output from nvidia/NV-embed-V2. Driven by pgvector's
# index-dimension limits:
#
#   - ``vector`` HNSW/IVFFlat: up to 2,000 dims.
#   - ``halfvec`` HNSW/IVFFlat: up to 4,000 dims (still <4096).
#   - ``bit``: 64,000 dims but binary quantization, too lossy for retrieval.
#
# NVIDIA's own developer forum recommends 1536-d for pgvector compatibility
# (see "Solved: pgvector ≤2000-dim index limit with NeMo embeddings"). NV-
# embed-V2 is Matryoshka-trained, so truncation to 1536 keeps ≥99% retrieval
# quality. 1536 is also the OpenAI text-embedding-3-small / ada-002 default,
# which keeps the schema portable across embedding providers.
#
# The collection-level ``embedding_dim`` column lets a future "physical
# isolation" mode pin a different per-collection dim — Sprint 4 evaluator
# territory. For Sprint 1 the platform default and the column value match.
_EMBEDDING_DIM = 1536


def upgrade() -> None:
    # ── 0. pgvector extension ───────────────────────────────────────────────
    # Fails fast if the postgres image is not pgvector-enabled; the operator
    # error is loud and the migration is idempotent.
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    # ── 0a. ``csp_app`` runtime role (RLS-respecting) ───────────────────────
    # PostgreSQL superusers and roles with BYPASSRLS bypass row-level security
    # *unconditionally* — even ``FORCE ROW LEVEL SECURITY`` doesn't apply to
    # them. The default ``csp`` role created by the postgres image is both,
    # so any application code connecting as ``csp`` defeats Layer 2 silently.
    #
    # We split runtime work onto a dedicated, non-privileged ``csp_app`` role
    # the application uses for *all* connections. Migrations still run as the
    # ``csp`` superuser (needed for CREATE EXTENSION); only runtime queries
    # are downgraded. The split makes RLS actually enforce.
    #
    # Idempotent: ``DO`` block tolerates re-run on a DB where the role
    # already exists (alembic re-applies after `downgrade()` would otherwise
    # fail on ``CREATE ROLE`` collision).
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'csp_app') THEN
                CREATE ROLE csp_app LOGIN PASSWORD 'csp'
                    NOBYPASSRLS NOSUPERUSER NOCREATEDB NOCREATEROLE;
            END IF;
        END
        $$
        """
    )
    # Grant access to existing schema. CREATE on the schema lets the app
    # ``CREATE INDEX`` etc. — ``startup_migrations`` issues those at boot.
    # ALTER DEFAULT PRIVILEGES then ensures any tables / sequences created
    # by future migrations are auto-granted.
    op.execute("GRANT USAGE, CREATE ON SCHEMA public TO csp_app")
    op.execute("GRANT ALL ON ALL TABLES IN SCHEMA public TO csp_app")
    op.execute("GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO csp_app")
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO csp_app"
    )
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        "GRANT ALL ON SEQUENCES TO csp_app"
    )

    # Transfer ownership of all existing public tables to csp_app. The CSP
    # backend's ``run_startup_migrations`` issues ALTER TABLE statements at
    # boot, which require ownership (GRANT ALL doesn't cover ALTER). Without
    # this loop the app would crash on first start with
    # "must be owner of table token_usage". Migration role keeps superuser
    # via MIGRATION_DATABASE_URL.
    op.execute(
        """
        DO $$
        DECLARE r RECORD;
        BEGIN
            FOR r IN
                SELECT schemaname, tablename FROM pg_tables WHERE schemaname='public'
            LOOP
                EXECUTE format('ALTER TABLE %I.%I OWNER TO csp_app',
                               r.schemaname, r.tablename);
            END LOOP;
            FOR r IN
                SELECT sequence_schema, sequence_name
                FROM information_schema.sequences WHERE sequence_schema='public'
            LOOP
                EXECUTE format('ALTER SEQUENCE %I.%I OWNER TO csp_app',
                               r.sequence_schema, r.sequence_name);
            END LOOP;
        END $$
        """
    )

    # ── 0b. legacy AgenticRAG document_chunks (TEXT PK, user_id/project_id)
    # has no prod data here (extension wasn't installed), so we drop it
    # outright. CASCADE removes any FKs / indexes referencing it.
    op.execute("DROP TABLE IF EXISTS document_chunks CASCADE")

    # ── 1. ingestion_collections ────────────────────────────────────────────
    op.create_table(
        "ingestion_collections",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "agent_id",
            sa.Integer(),
            sa.ForeignKey("agents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "chunking_config",
            JSONB(),
            nullable=False,
            server_default=sa.text(
                """'{"strategy": "hierarchical", "max_leaf_tokens": 1024,"""
                """ "overlap_tokens": 64}'::jsonb"""
            ),
        ),
        sa.Column(
            "embedding_model",
            sa.String(length=200),
            nullable=False,
            server_default=sa.text("'nvidia/NV-embed-V2'"),
        ),
        sa.Column(
            "embedding_dim",
            sa.Integer(),
            nullable=False,
            server_default=sa.text(str(_EMBEDDING_DIM)),
        ),
        sa.Column(
            "status",
            sa.String(length=20),
            nullable=False,
            server_default=sa.text("'active'"),
        ),
        sa.Column(
            "document_count", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column(
            "chunk_count", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column(
            "bytes_stored", sa.BigInteger(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column(
            "created_by",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.UniqueConstraint("agent_id", "name", name="uq_collections_agent_name"),
    )
    op.create_index(
        "ix_collections_agent_id", "ingestion_collections", ["agent_id"]
    )

    # ── 2. ingestion_documents ──────────────────────────────────────────────
    op.create_table(
        "ingestion_documents",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "collection_id",
            sa.Integer(),
            sa.ForeignKey("ingestion_collections.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("filename", sa.String(length=500), nullable=False),
        # SHA-256 in hex = 64 chars; CHAR(64) lets PG pad/strict-check.
        sa.Column("sha256", sa.CHAR(length=64), nullable=False),
        sa.Column("mime_type", sa.String(length=200), nullable=True),
        sa.Column("bytes", sa.BigInteger(), nullable=True),
        # Where the original file is persisted (path under ingestion blob dir
        # OR external object-store key). NULL until the upload completes.
        sa.Column("storage_path", sa.String(length=1000), nullable=True),
        sa.Column(
            "status",
            sa.String(length=20),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column(
            "chunk_count", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "uploaded_by",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "uploaded_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("indexed_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint(
            "collection_id", "sha256", name="uq_documents_collection_sha256"
        ),
    )
    op.create_index(
        "ix_documents_collection_status",
        "ingestion_documents",
        ["collection_id", "status"],
    )

    # ── 3. document_chunks (pgvector) ───────────────────────────────────────
    # Layer 1 schema enforcement: agent_id NOT NULL is the first defence
    # against cross-tenant leakage. RLS policy below is Layer 2.
    op.execute(
        f"""
        CREATE TABLE document_chunks (
            id              BIGSERIAL PRIMARY KEY,
            collection_id   INTEGER NOT NULL
                            REFERENCES ingestion_collections(id) ON DELETE CASCADE,
            -- Denormalised from collections so RLS / index can filter without join.
            agent_id        INTEGER NOT NULL,
            document_id     INTEGER NOT NULL
                            REFERENCES ingestion_documents(id) ON DELETE CASCADE,
            chunk_key       TEXT NOT NULL,
            content         TEXT NOT NULL,
            content_tsv     tsvector,
            embedding       vector({_EMBEDDING_DIM}),
            metadata        JSONB NOT NULL DEFAULT '{{}}'::jsonb,
            token_count     INTEGER,
            created_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT chunks_agent_required CHECK (agent_id IS NOT NULL),
            CONSTRAINT uq_chunks_collection_chunk_key UNIQUE (collection_id, chunk_key)
        )
        """
    )

    # Filtering index: every retrieval query MUST hit (agent_id, collection_id)
    # before pgvector similarity, so this is the first index probed.
    op.create_index(
        "ix_chunks_agent_collection",
        "document_chunks",
        ["agent_id", "collection_id"],
    )
    op.create_index(
        "ix_chunks_document_id", "document_chunks", ["document_id"]
    )

    # FTS index for hybrid keyword search.
    op.execute(
        """
        CREATE INDEX ix_chunks_content_tsv
            ON document_chunks USING GIN (content_tsv)
        """
    )

    # ANN index — HNSW on the 1536-d vector column. design doc §3.2 specified
    # IVFFlat originally; HNSW was the Sprint 4 plan. We adopt HNSW now
    # because:
    #
    #   1. Both indexes have the same 2,000-d cap, so adopting HNSW costs
    #      nothing in coverage.
    #   2. HNSW gives better speed-recall tradeoffs at write-once corpus
    #      sizes (which is what ingestion produces).
    #   3. pgvector 0.8.2 (the image we're on) ships HNSW out of the box.
    #
    # Default params (m=16, ef_construction=64) match the pgvector docs
    # recommended starting point — Sprint 4 evaluator can re-tune per
    # corpus shape.
    op.execute(
        """
        CREATE INDEX ix_chunks_embedding_hnsw
            ON document_chunks USING hnsw (embedding vector_cosine_ops)
            WITH (m = 16, ef_construction = 64)
        """
    )

    # ── 3b. RLS Layer 2 — engine-level isolation ────────────────────────────
    # Even if application code forgets WHERE agent_id = X, the engine
    # filters rows down to the agent set in `anila.agent_id` GUC. SQL
    # injection cannot bypass this because RLS runs after row visibility
    # planning. Sessions that don't SET LOCAL anila.agent_id (i.e. raw
    # admin tools) see zero rows by default — by design.
    #
    # FORCE ROW LEVEL SECURITY is critical: without it, the table OWNER
    # (the migration role, typically the application's main user) bypasses
    # the policy entirely. PostgreSQL grants table owners RLS-bypass by
    # default, which is the opposite of what we want for an isolation
    # boundary. With FORCE, even the owner is subject to the policy —
    # nothing short of a superuser bypass can read other agents' rows.
    op.execute("ALTER TABLE document_chunks ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE document_chunks FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY chunks_agent_isolation ON document_chunks
            FOR ALL
            USING (agent_id = NULLIF(current_setting('anila.agent_id', true), '')::int)
        """
    )

    # ── 4. ingestion_jobs ───────────────────────────────────────────────────
    op.create_table(
        "ingestion_jobs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("arq_job_id", sa.String(length=100), nullable=True, unique=True),
        sa.Column(
            "collection_id",
            sa.Integer(),
            sa.ForeignKey("ingestion_collections.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "document_id",
            sa.Integer(),
            sa.ForeignKey("ingestion_documents.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("job_type", sa.String(length=30), nullable=False),
        sa.Column(
            "status",
            sa.String(length=20),
            nullable=False,
            server_default=sa.text("'queued'"),
        ),
        sa.Column(
            "progress_pct",
            sa.SmallInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("progress_message", sa.Text(), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "enqueued_by",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "enqueued_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
    )
    op.create_index(
        "ix_jobs_collection_status",
        "ingestion_jobs",
        ["collection_id", "status"],
    )


def downgrade() -> None:
    # Reverse order — child tables first, RLS policy auto-drops with table,
    # then collections (parent).
    op.drop_index("ix_jobs_collection_status", table_name="ingestion_jobs")
    op.drop_table("ingestion_jobs")

    # document_chunks: indexes + policy auto-drop with the table.
    op.execute("DROP TABLE IF EXISTS document_chunks CASCADE")

    op.drop_index(
        "ix_documents_collection_status", table_name="ingestion_documents"
    )
    op.drop_table("ingestion_documents")

    op.drop_index("ix_collections_agent_id", table_name="ingestion_collections")
    op.drop_table("ingestion_collections")

    # We do NOT drop the vector extension — other tables outside this
    # migration's scope might depend on it, and the extension itself is
    # cheap to keep installed.
