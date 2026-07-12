# -*- coding: utf-8 -*-
"""Gate 2 immutable SourceSnapshot and Citation guards.

Revision ID: r1_0015
Revises: r1_0014
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "r1_0015"
down_revision: Union[str, None] = "r1_0014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_check_constraint(
        "ck_source_snapshots_content_hash_sha256",
        "source_snapshots",
        "content_hash IS NULL OR content_hash ~ '^[0-9a-f]{64}$'",
    )
    op.create_unique_constraint(
        "uq_citations_snapshot_chunk_used_by",
        "citations",
        ["source_snapshot_id", "chunk_id", "used_by"],
    )
    op.create_check_constraint(
        "ck_citations_used_by_gate2",
        "citations",
        "used_by IN ('answer', 'artifact', 'agent_tool')",
    )
    op.create_check_constraint(
        "ck_citations_page_positive",
        "citations",
        "page IS NULL OR page > 0",
    )
    op.create_check_constraint(
        "ck_citations_score_finite",
        "citations",
        "score IS NULL OR (score <> 'NaN'::double precision AND "
        "score <> 'Infinity'::double precision AND "
        "score <> '-Infinity'::double precision)",
    )
    op.execute(
        sa.text(
            r"""
            CREATE OR REPLACE FUNCTION anila_gate2_guard_sealed_snapshot()
            RETURNS TRIGGER
            LANGUAGE plpgsql
            AS $$
            BEGIN
              IF OLD.content_hash IS NOT NULL AND (
                   NEW.task_id IS DISTINCT FROM OLD.task_id
                OR NEW.origin IS DISTINCT FROM OLD.origin
                OR NEW.source_scope IS DISTINCT FROM OLD.source_scope
                OR NEW.collection_ids IS DISTINCT FROM OLD.collection_ids
                OR NEW.document_ids IS DISTINCT FROM OLD.document_ids
                OR NEW.chunk_ids IS DISTINCT FROM OLD.chunk_ids
                OR NEW.document_versions IS DISTINCT FROM OLD.document_versions
                OR NEW.retrieval_queries IS DISTINCT FROM OLD.retrieval_queries
                OR NEW.content_hash IS DISTINCT FROM OLD.content_hash
                OR NEW.payload_ref IS DISTINCT FROM OLD.payload_ref
                OR NEW.classification_level IS DISTINCT FROM OLD.classification_level
                OR NEW.classification_latched_at IS DISTINCT FROM OLD.classification_latched_at
                OR NEW.classification_source IS DISTINCT FROM OLD.classification_source
                OR NEW.classification_event_id IS DISTINCT FROM OLD.classification_event_id
                OR NEW.created_at IS DISTINCT FROM OLD.created_at
              ) THEN
                RAISE EXCEPTION 'sealed SourceSnapshot % is immutable', OLD.id
                  USING ERRCODE = '55000';
              END IF;
              IF NEW.content_hash IS NOT NULL THEN
                IF NEW.origin = 'none' THEN
                  RAISE EXCEPTION 'sealed SourceSnapshot cannot declare origin=none'
                    USING ERRCODE = '23514';
                END IF;
                IF jsonb_typeof(NEW.collection_ids::jsonb) <> 'array'
                   OR jsonb_typeof(NEW.document_ids::jsonb) <> 'array'
                   OR jsonb_typeof(NEW.chunk_ids::jsonb) <> 'array'
                   OR jsonb_typeof(NEW.retrieval_queries::jsonb) <> 'array' THEN
                  RAISE EXCEPTION 'sealed SourceSnapshot evidence lists must be arrays'
                    USING ERRCODE = '23514';
                END IF;
                IF NEW.document_versions IS NULL OR
                   jsonb_typeof(NEW.document_versions::jsonb) <> 'object' THEN
                  RAISE EXCEPTION 'sealed SourceSnapshot requires document_versions'
                    USING ERRCODE = '23514';
                END IF;
                IF NEW.payload_ref IS NULL OR btrim(NEW.payload_ref) = '' THEN
                  RAISE EXCEPTION 'sealed SourceSnapshot requires payload_ref'
                    USING ERRCODE = '23514';
                END IF;
              END IF;
              RETURN NEW;
            END;
            $$;

            CREATE OR REPLACE FUNCTION anila_gate2_guard_citation_snapshot_membership()
            RETURNS TRIGGER
            LANGUAGE plpgsql
            AS $$
            DECLARE
              snapshot_row source_snapshots%ROWTYPE;
            BEGIN
              SELECT * INTO snapshot_row
                FROM source_snapshots
               WHERE id = NEW.source_snapshot_id
                 FOR KEY SHARE;
              IF NOT FOUND OR snapshot_row.content_hash IS NULL THEN
                RAISE EXCEPTION 'Citation requires a sealed SourceSnapshot'
                  USING ERRCODE = '23514';
              END IF;
              IF NOT EXISTS (
                SELECT 1
                  FROM jsonb_array_elements_text(snapshot_row.chunk_ids::jsonb) AS item(value)
                 WHERE item.value = NEW.chunk_id
              ) THEN
                RAISE EXCEPTION 'Citation chunk % is absent from SourceSnapshot %',
                  NEW.chunk_id, NEW.source_snapshot_id
                  USING ERRCODE = '23514';
              END IF;
              IF NEW.document_id IS NOT NULL AND NOT EXISTS (
                SELECT 1
                  FROM jsonb_array_elements_text(snapshot_row.document_ids::jsonb) AS item(value)
                 WHERE item.value::BIGINT = NEW.document_id
              ) THEN
                RAISE EXCEPTION 'Citation document % is absent from SourceSnapshot %',
                  NEW.document_id, NEW.source_snapshot_id
                  USING ERRCODE = '23514';
              END IF;
              IF anila_gate2_classification_rank(NEW.classification_level) >
                 anila_gate2_classification_rank(snapshot_row.classification_level) THEN
                RAISE EXCEPTION 'Citation classification exceeds SourceSnapshot'
                  USING ERRCODE = '23514';
              END IF;
              RETURN NEW;
            END;
            $$;

            CREATE OR REPLACE FUNCTION anila_gate2_guard_citation_update()
            RETURNS TRIGGER
            LANGUAGE plpgsql
            AS $$
            BEGIN
              RAISE EXCEPTION 'persisted Citation rows are immutable'
                USING ERRCODE = '55000';
            END;
            $$;

            CREATE TRIGGER trg_gate2_source_snapshot_seal
              BEFORE INSERT OR UPDATE ON source_snapshots
              FOR EACH ROW EXECUTE FUNCTION anila_gate2_guard_sealed_snapshot();
            CREATE TRIGGER trg_gate2_citation_membership
              BEFORE INSERT ON citations
              FOR EACH ROW EXECUTE FUNCTION anila_gate2_guard_citation_snapshot_membership();
            CREATE TRIGGER trg_gate2_citation_immutable
              BEFORE UPDATE ON citations
              FOR EACH ROW EXECUTE FUNCTION anila_gate2_guard_citation_update();
            """
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            """
            DROP TRIGGER IF EXISTS trg_gate2_citation_immutable ON citations;
            DROP TRIGGER IF EXISTS trg_gate2_citation_membership ON citations;
            DROP TRIGGER IF EXISTS trg_gate2_source_snapshot_seal ON source_snapshots;
            DROP FUNCTION IF EXISTS anila_gate2_guard_citation_update();
            DROP FUNCTION IF EXISTS anila_gate2_guard_citation_snapshot_membership();
            DROP FUNCTION IF EXISTS anila_gate2_guard_sealed_snapshot();
            """
        )
    )
    op.drop_constraint(
        "ck_citations_score_finite", "citations", type_="check"
    )
    op.drop_constraint(
        "ck_citations_page_positive", "citations", type_="check"
    )
    op.drop_constraint(
        "ck_citations_used_by_gate2", "citations", type_="check"
    )
    op.drop_constraint(
        "uq_citations_snapshot_chunk_used_by", "citations", type_="unique"
    )
    op.drop_constraint(
        "ck_source_snapshots_content_hash_sha256",
        "source_snapshots",
        type_="check",
    )
