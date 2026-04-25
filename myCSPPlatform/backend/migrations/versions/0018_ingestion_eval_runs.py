"""Add ``ingestion_eval_runs`` for the Chunking Evaluator (§3.1, §6).

Per docs/ingestion-platform-design.md §6: dev uploads sample documents
+ a list of (query, expected_doc_id) pairs, picks N strategies, and
the evaluator scores each strategy by Hit@1 / Hit@5 / MRR. The eval
run row carries the input set + the computed results JSONB so the
results page can render a comparison table without re-running.

Schema follows the design doc shape but stays minimal — Sprint-3 first
cut excludes ``judge_llm_config`` because the LLM-as-judge path is
deferred to a follow-up. The column is added now anyway as nullable
JSONB so adding judge later doesn't need a schema migration.

Revision ID: 0018
Revises: 0017
Create Date: 2026-04-25
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB


revision: str = "0018"
down_revision: Union[str, None] = "0017"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "ingestion_eval_runs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "collection_id",
            sa.Integer(),
            sa.ForeignKey("ingestion_collections.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=200), nullable=False),
        # Array-of-int document ids; we use Postgres array rather than
        # JSONB so simple ANY($1::int[]) lookups are index-friendly if
        # we ever want to filter eval rows by doc.
        sa.Column(
            "sample_document_ids",
            sa.dialects.postgresql.ARRAY(sa.Integer()),
            nullable=False,
        ),
        sa.Column("strategies_tried", JSONB(), nullable=False),
        sa.Column("queries", JSONB(), nullable=False),
        sa.Column("judge_llm_config", JSONB(), nullable=True),
        sa.Column("arq_job_id", sa.String(length=100), nullable=True, unique=True),
        sa.Column(
            "status",
            sa.String(length=20),
            nullable=False,
            server_default=sa.text("'queued'"),
        ),
        sa.Column("results", JSONB(), nullable=True),
        sa.Column("recommended_strategy", sa.String(length=64), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
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
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
    )
    op.create_index(
        "ix_eval_runs_collection_id",
        "ingestion_eval_runs",
        ["collection_id"],
    )
    op.execute("ALTER TABLE ingestion_eval_runs OWNER TO csp_app")


def downgrade() -> None:
    op.drop_index("ix_eval_runs_collection_id", table_name="ingestion_eval_runs")
    op.drop_table("ingestion_eval_runs")
