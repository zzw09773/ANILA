# -*- coding: utf-8 -*-
"""Add immutable ingestion generations and active-generation pointers.

Revision ID: r1_0021
Revises: r1_0020
"""

from __future__ import annotations

import os
import re
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "r1_0021"
down_revision: Union[str, None] = "r1_0020"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_FP_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def upgrade() -> None:
    op.add_column(
        "ingestion_collections",
        sa.Column("embedding_fingerprint", sa.CHAR(length=71), nullable=True),
    )
    bind = op.get_bind()
    existing = bind.execute(
        sa.text("SELECT count(*) FROM ingestion_collections")
    ).scalar_one()
    if existing:
        fingerprint = os.getenv("EMBEDDING_MODEL_FINGERPRINT", "").strip().lower()
        if not _FP_RE.fullmatch(fingerprint):
            raise RuntimeError(
                "EMBEDDING_MODEL_FINGERPRINT (sha256:<64 lowercase hex>) is required "
                "to migrate existing ingestion collections; model names are not "
                "accepted as weight fingerprints"
            )
        bind.execute(
            sa.text(
                "UPDATE ingestion_collections SET embedding_fingerprint=:fingerprint"
            ),
            {"fingerprint": fingerprint},
        )
    op.alter_column(
        "ingestion_collections", "embedding_fingerprint", nullable=False
    )
    op.create_check_constraint(
        "ck_ingestion_collections_embedding_fingerprint",
        "ingestion_collections",
        "embedding_fingerprint ~ '^sha256:[0-9a-f]{64}$'",
    )

    op.add_column(
        "ingestion_documents",
        sa.Column(
            "availability_status",
            sa.String(length=20),
            nullable=False,
            server_default="unavailable",
        ),
    )
    op.add_column(
        "ingestion_documents",
        sa.Column(
            "processing_stage",
            sa.String(length=20),
            nullable=False,
            server_default="pending",
        ),
    )
    op.add_column(
        "ingestion_documents",
        sa.Column("active_generation_id", sa.BigInteger(), nullable=True),
    )
    op.create_check_constraint(
        "ck_ingestion_documents_availability",
        "ingestion_documents",
        "availability_status IN ('unavailable','available')",
    )
    op.create_check_constraint(
        "ck_ingestion_documents_processing_stage",
        "ingestion_documents",
        "processing_stage IN ('pending','parsing','chunking','embedding',"
        "'staging','complete','failed')",
    )

    op.create_table(
        "ingestion_document_generations",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("document_id", sa.Integer(), nullable=False),
        sa.Column("collection_id", sa.Integer(), nullable=False),
        sa.Column("generation_number", sa.Integer(), nullable=False),
        sa.Column(
            "source_ingestion_job_id",
            sa.Integer(),
            sa.ForeignKey("ingestion_jobs.id", ondelete="RESTRICT"),
            nullable=True,
            unique=True,
        ),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("embedding_model", sa.String(length=200), nullable=False),
        sa.Column("embedding_fingerprint", sa.CHAR(length=71), nullable=False),
        sa.Column("embedding_dim", sa.Integer(), nullable=False),
        sa.Column("chunk_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["collection_id", "document_id"],
            ["ingestion_documents.collection_id", "ingestion_documents.id"],
            ondelete="CASCADE",
            name="fk_ingestion_generation_document_collection",
        ),
        sa.UniqueConstraint(
            "document_id",
            "generation_number",
            name="uq_ingestion_generation_document_number",
        ),
        sa.UniqueConstraint(
            "id",
            "document_id",
            "collection_id",
            name="uq_ingestion_generation_identity_scope",
        ),
        sa.CheckConstraint(
            "status IN ('staging','active','retired','failed')",
            name="ck_ingestion_generation_status",
        ),
        sa.CheckConstraint(
            "embedding_fingerprint ~ '^sha256:[0-9a-f]{64}$'",
            name="ck_ingestion_generation_fingerprint",
        ),
        sa.CheckConstraint(
            "embedding_dim > 0 AND chunk_count >= 0",
            name="ck_ingestion_generation_counts",
        ),
    )
    op.create_index(
        "uq_ingestion_generation_one_active",
        "ingestion_document_generations",
        ["document_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )
    op.create_index(
        "ix_ingestion_generation_collection_status",
        "ingestion_document_generations",
        ["collection_id", "status"],
    )

    op.add_column(
        "document_chunks",
        sa.Column("generation_id", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "document_chunks",
        sa.Column(
            "is_active_generation",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )

    # Every already-indexed document, every document with chunks, and every
    # indexed zero-chunk document receives a legacy generation.
    op.execute(
        """
        INSERT INTO ingestion_document_generations
          (document_id,collection_id,generation_number,status,embedding_model,
           embedding_fingerprint,embedding_dim,chunk_count,created_at,activated_at)
        SELECT d.id,d.collection_id,1,'active',c.embedding_model,
               c.embedding_fingerprint,c.embedding_dim,count(ch.id),now(),now()
          FROM ingestion_documents d
          JOIN ingestion_collections c ON c.id=d.collection_id
          LEFT JOIN document_chunks ch ON ch.document_id=d.id
         WHERE d.status='indexed' OR EXISTS
               (SELECT 1 FROM document_chunks x WHERE x.document_id=d.id)
         GROUP BY d.id,d.collection_id,c.embedding_model,
                  c.embedding_fingerprint,c.embedding_dim
        """
    )
    op.execute(
        """
        UPDATE document_chunks ch SET generation_id=g.id,is_active_generation=true
          FROM ingestion_document_generations g
         WHERE g.document_id=ch.document_id AND g.status='active'
        """
    )
    op.execute(
        """
        UPDATE ingestion_documents d
           SET active_generation_id=g.id,availability_status='available',
               processing_stage='complete',status='indexed',chunk_count=g.chunk_count
          FROM ingestion_document_generations g
         WHERE g.document_id=d.id AND g.status='active'
        """
    )
    orphan_chunks = bind.execute(
        sa.text("SELECT count(*) FROM document_chunks WHERE generation_id IS NULL")
    ).scalar_one()
    if orphan_chunks:
        raise RuntimeError("document_chunks backfill left rows without a generation")
    op.alter_column("document_chunks", "generation_id", nullable=False)
    op.create_foreign_key(
        "fk_document_chunks_generation",
        "document_chunks",
        "ingestion_document_generations",
        ["generation_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.drop_constraint(
        "uq_chunks_doc_chunk_key", "document_chunks", type_="unique"
    )
    op.create_unique_constraint(
        "uq_chunks_generation_chunk_key",
        "document_chunks",
        ["generation_id", "chunk_key"],
    )
    op.create_index(
        "ix_chunks_active_collection_document",
        "document_chunks",
        ["collection_id", "document_id"],
        postgresql_where=sa.text("is_active_generation = true"),
    )
    op.create_foreign_key(
        "fk_ingestion_document_active_generation",
        "ingestion_documents",
        "ingestion_document_generations",
        ["active_generation_id", "id", "collection_id"],
        ["id", "document_id", "collection_id"],
        ondelete="RESTRICT",
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION enforce_active_generation_pointer()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF NEW.active_generation_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM ingestion_document_generations g
             WHERE g.id=NEW.active_generation_id
               AND g.document_id=NEW.id
               AND g.collection_id=NEW.collection_id
               AND g.status='active'
          ) THEN
            RAISE EXCEPTION 'active_generation_id must reference an active generation'
              USING ERRCODE='23514';
          END IF;
          RETURN NEW;
        END $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_ingestion_document_active_generation
        BEFORE INSERT OR UPDATE OF active_generation_id,collection_id
        ON ingestion_documents FOR EACH ROW
        EXECUTE FUNCTION enforce_active_generation_pointer()
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION protect_referenced_active_generation()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF OLD.status='active'
             AND (TG_OP='DELETE' OR NEW.status IS DISTINCT FROM 'active')
             AND EXISTS (
               SELECT 1 FROM ingestion_documents d
                WHERE d.active_generation_id=OLD.id
             ) THEN
            RAISE EXCEPTION 'referenced active generation cannot be retired or deleted'
              USING ERRCODE='23514';
          END IF;
          IF TG_OP='DELETE' THEN RETURN OLD; END IF;
          RETURN NEW;
        END $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_protect_referenced_active_generation
        BEFORE UPDATE OF status OR DELETE ON ingestion_document_generations
        FOR EACH ROW EXECUTE FUNCTION protect_referenced_active_generation()
        """
    )

    op.create_table(
        "similarity_recompute_requests",
        sa.Column(
            "collection_id",
            sa.Integer(),
            sa.ForeignKey("ingestion_collections.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("request_seq", sa.BigInteger(), nullable=False),
        sa.Column("claimed_seq", sa.BigInteger(), nullable=True),
        sa.Column(
            "requested_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "not_before",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("lease_token", sa.String(length=64), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "status IN ('pending','running')",
            name="ck_similarity_recompute_status",
        ),
        sa.CheckConstraint(
            "request_seq >= 1 AND (claimed_seq IS NULL OR claimed_seq >= 1)",
            name="ck_similarity_recompute_sequences",
        ),
        sa.CheckConstraint(
            "(status='pending' AND claimed_seq IS NULL AND lease_token IS NULL "
            "AND lease_expires_at IS NULL) OR "
            "(status='running' AND claimed_seq IS NOT NULL AND lease_token IS NOT NULL "
            "AND lease_expires_at IS NOT NULL)",
            name="ck_similarity_recompute_lease_state",
        ),
    )
    op.create_index(
        "ix_similarity_recompute_dispatch",
        "similarity_recompute_requests",
        ["status", "not_before"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_similarity_recompute_dispatch",
        table_name="similarity_recompute_requests",
    )
    op.drop_table("similarity_recompute_requests")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_protect_referenced_active_generation "
        "ON ingestion_document_generations"
    )
    op.execute("DROP FUNCTION IF EXISTS protect_referenced_active_generation()")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_ingestion_document_active_generation "
        "ON ingestion_documents"
    )
    op.execute("DROP FUNCTION IF EXISTS enforce_active_generation_pointer()")
    op.drop_constraint(
        "fk_ingestion_document_active_generation",
        "ingestion_documents",
        type_="foreignkey",
    )
    op.drop_index(
        "ix_chunks_active_collection_document", table_name="document_chunks"
    )
    op.execute("DELETE FROM document_chunks WHERE is_active_generation=false")
    op.drop_constraint(
        "uq_chunks_generation_chunk_key", "document_chunks", type_="unique"
    )
    op.create_unique_constraint(
        "uq_chunks_doc_chunk_key",
        "document_chunks",
        ["collection_id", "document_id", "chunk_key"],
    )
    op.drop_constraint(
        "fk_document_chunks_generation", "document_chunks", type_="foreignkey"
    )
    op.drop_column("document_chunks", "is_active_generation")
    op.drop_column("document_chunks", "generation_id")
    op.drop_column("ingestion_documents", "active_generation_id")
    op.drop_constraint(
        "ck_ingestion_documents_processing_stage",
        "ingestion_documents",
        type_="check",
    )
    op.drop_constraint(
        "ck_ingestion_documents_availability",
        "ingestion_documents",
        type_="check",
    )
    op.drop_column("ingestion_documents", "processing_stage")
    op.drop_column("ingestion_documents", "availability_status")
    op.drop_index(
        "ix_ingestion_generation_collection_status",
        table_name="ingestion_document_generations",
    )
    op.drop_index(
        "uq_ingestion_generation_one_active",
        table_name="ingestion_document_generations",
    )
    op.drop_table("ingestion_document_generations")
    op.drop_constraint(
        "ck_ingestion_collections_embedding_fingerprint",
        "ingestion_collections",
        type_="check",
    )
    op.drop_column("ingestion_collections", "embedding_fingerprint")
