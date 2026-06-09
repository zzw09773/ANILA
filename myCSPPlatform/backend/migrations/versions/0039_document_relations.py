"""Document-relations Phase 1: cross-document citation edges + title columns.

See docs/ingestion/document-relations-design.md (v2, codex-reviewed).

- ingestion_documents: add `title` / `normalized_title` (resolve citation targets;
  filename alone is useless for ROC regs named by date/字號) + index +
  UNIQUE(collection_id, id) so document_relations can use a composite FK.
- document_relations: edge table (src --relation_type--> dst), shared substrate
  for A (rule/manual) and future B (llm) edges. Composite FK enforces
  same-collection at the DB layer (codex #1); UNIQUE includes `source` so
  rule/manual/llm coexist (codex #9). RLS mirrors 0037 exactly:
  ENABLE + FORCE + NULLIF(current_setting('anila.collection_id', true), '') (codex #2).
- dst_chunk_id reserved (Phase 1.5 chunk-level); no FK yet.

Idempotent (inspector-guarded) for the create_all test path.

Revision ID: 0039
Revises: 0038
Create Date: 2026-06-09
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0039"
down_revision: Union[str, None] = "0038"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_UQ_DOC = "uq_ingestion_documents_collection_id_id"


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)

    # 1) ingestion_documents: title / normalized_title + UNIQUE(collection_id, id)
    doc_cols = {c["name"] for c in insp.get_columns("ingestion_documents")}
    if "title" not in doc_cols:
        op.add_column("ingestion_documents", sa.Column("title", sa.String(500), nullable=True))
    if "normalized_title" not in doc_cols:
        op.add_column(
            "ingestion_documents", sa.Column("normalized_title", sa.String(500), nullable=True)
        )
        op.create_index(
            "ix_ingestion_documents_collection_normtitle",
            "ingestion_documents",
            ["collection_id", "normalized_title"],
        )
    uqs = {c["name"] for c in insp.get_unique_constraints("ingestion_documents")}
    if _UQ_DOC not in uqs:
        op.create_unique_constraint(_UQ_DOC, "ingestion_documents", ["collection_id", "id"])

    # 2) document_relations
    if "document_relations" not in set(insp.get_table_names()):
        op.create_table(
            "document_relations",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("collection_id", sa.Integer(), nullable=False),
            sa.Column("src_document_id", sa.Integer(), nullable=False),
            sa.Column("dst_document_id", sa.Integer(), nullable=True),
            sa.Column("dst_chunk_id", sa.Integer(), nullable=True),  # Phase 1.5 reserved
            sa.Column("target_ref", sa.String(500), nullable=False),
            sa.Column("relation_type", sa.String(20), nullable=False),
            sa.Column("confidence", sa.Float(), nullable=False, server_default="1.0"),
            sa.Column("source", sa.String(10), nullable=False),
            sa.Column("extractor_run_id", sa.String(40), nullable=True),
            sa.Column("evidence", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("created_by_user_id", sa.Integer(), nullable=True),
            # same-collection enforced at DB layer via composite FK (codex #1)
            sa.ForeignKeyConstraint(
                ["collection_id", "src_document_id"],
                ["ingestion_documents.collection_id", "ingestion_documents.id"],
                ondelete="CASCADE",
                name="fk_docrel_src",
            ),
            sa.ForeignKeyConstraint(
                ["collection_id", "dst_document_id"],
                ["ingestion_documents.collection_id", "ingestion_documents.id"],
                ondelete="SET NULL",
                name="fk_docrel_dst",
            ),
            sa.ForeignKeyConstraint(
                ["created_by_user_id"], ["users.id"], ondelete="SET NULL"
            ),
            # rule/manual/llm coexist for the same edge (codex #9)
            sa.UniqueConstraint(
                "collection_id", "src_document_id", "target_ref", "relation_type", "source",
                name="uq_document_relations_edge",
            ),
        )
        op.create_index(
            "ix_document_relations_src", "document_relations",
            ["collection_id", "src_document_id"],
        )
        op.create_index(
            "ix_document_relations_dst", "document_relations",
            ["collection_id", "dst_document_id"],
        )
        op.create_index(
            "ix_document_relations_target", "document_relations",
            ["collection_id", "target_ref"],
        )

    # 3) RLS — mirror 0037 (ENABLE + FORCE + NULLIF current_setting). Postgres only;
    #    SQLite (test create_all) ignores these, which is fine.
    if bind.dialect.name == "postgresql":
        op.execute("ALTER TABLE document_relations ENABLE ROW LEVEL SECURITY")
        op.execute("ALTER TABLE document_relations FORCE ROW LEVEL SECURITY")
        op.execute(
            """
            CREATE POLICY document_relations_collection_isolation ON document_relations
                FOR ALL
                USING (
                    collection_id = NULLIF(
                        current_setting('anila.collection_id', true),
                        ''
                    )::int
                )
            """
        )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "document_relations" in set(insp.get_table_names()):
        if bind.dialect.name == "postgresql":
            op.execute(
                "DROP POLICY IF EXISTS document_relations_collection_isolation "
                "ON document_relations"
            )
            op.execute("ALTER TABLE document_relations NO FORCE ROW LEVEL SECURITY")
            op.execute("ALTER TABLE document_relations DISABLE ROW LEVEL SECURITY")
        op.drop_table("document_relations")

    uqs = {c["name"] for c in insp.get_unique_constraints("ingestion_documents")}
    if _UQ_DOC in uqs:
        op.drop_constraint(_UQ_DOC, "ingestion_documents", type_="unique")
    doc_cols = {c["name"] for c in insp.get_columns("ingestion_documents")}
    if "normalized_title" in doc_cols:
        op.drop_index(
            "ix_ingestion_documents_collection_normtitle", table_name="ingestion_documents"
        )
        op.drop_column("ingestion_documents", "normalized_title")
    if "title" in doc_cols:
        op.drop_column("ingestion_documents", "title")
