# -*- coding: utf-8 -*-
"""Add the durable ingestion transactional outbox.

Revision ID: r1_0020
Revises: r1_0019
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "r1_0020"
down_revision: Union[str, None] = "r1_0019"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "ingestion_jobs",
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "ingestion_jobs",
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"),
    )
    op.add_column("ingestion_jobs", sa.Column("lease_token", sa.String(64)))
    op.add_column(
        "ingestion_jobs", sa.Column("lease_expires_at", sa.DateTime(timezone=True))
    )
    op.add_column(
        "ingestion_jobs", sa.Column("heartbeat_at", sa.DateTime(timezone=True))
    )
    op.add_column(
        "ingestion_jobs", sa.Column("next_attempt_at", sa.DateTime(timezone=True))
    )
    op.add_column("ingestion_jobs", sa.Column("failure_kind", sa.String(20)))
    op.add_column("ingestion_jobs", sa.Column("retryable", sa.Boolean()))
    op.add_column(
        "ingestion_jobs", sa.Column("dead_lettered_at", sa.DateTime(timezone=True))
    )
    op.execute(
        "UPDATE ingestion_jobs SET completed_at=COALESCE(completed_at,enqueued_at) "
        "WHERE status IN ('succeeded','failed','cancelled')"
    )
    op.create_check_constraint(
        "ck_ingestion_jobs_status",
        "ingestion_jobs",
        "status IN ('dispatch_pending','queued','running','retry_wait',"
        "'succeeded','failed','cancelled','dead_letter')",
    )
    op.create_check_constraint(
        "ck_ingestion_jobs_attempt_bounds",
        "ingestion_jobs",
        "attempt_count >= 0 AND max_attempts >= 1 AND attempt_count <= max_attempts",
    )
    op.create_check_constraint(
        "ck_ingestion_jobs_lease_state",
        "ingestion_jobs",
        "(status='running' AND lease_token IS NOT NULL AND "
        "lease_expires_at IS NOT NULL AND heartbeat_at IS NOT NULL) OR "
        "(status<>'running' AND lease_token IS NULL AND lease_expires_at IS NULL)",
    )
    op.create_table(
        "ingestion_outbox",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "ingestion_job_id",
            sa.Integer(),
            sa.ForeignKey("ingestion_jobs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("arq_job_id", sa.String(length=100), nullable=False, unique=True),
        sa.Column(
            "task_name",
            sa.String(length=100),
            nullable=False,
            server_default=sa.text("'ingest_document'"),
        ),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "status",
            sa.String(length=20),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column(
            "attempt_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "available_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("lease_token", sa.String(length=64), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'dispatching', 'published')",
            name="ck_ingestion_outbox_status",
        ),
        sa.CheckConstraint(
            "(status = 'pending' AND lease_token IS NULL "
            "AND lease_expires_at IS NULL AND published_at IS NULL) OR "
            "(status = 'dispatching' AND lease_token IS NOT NULL "
            "AND lease_expires_at IS NOT NULL AND published_at IS NULL) OR "
            "(status = 'published' AND lease_token IS NULL "
            "AND lease_expires_at IS NULL AND published_at IS NOT NULL)",
            name="ck_ingestion_outbox_state_fields",
        ),
        sa.UniqueConstraint(
            "ingestion_job_id",
            "attempt_number",
            name="uq_ingestion_outbox_job_attempt",
        ),
    )
    op.create_index(
        "ix_ingestion_outbox_dispatch",
        "ingestion_outbox",
        ["status", "available_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_ingestion_outbox_dispatch", table_name="ingestion_outbox")
    op.drop_table("ingestion_outbox")
    op.drop_constraint(
        "ck_ingestion_jobs_lease_state", "ingestion_jobs", type_="check"
    )
    op.drop_constraint(
        "ck_ingestion_jobs_attempt_bounds", "ingestion_jobs", type_="check"
    )
    op.drop_constraint("ck_ingestion_jobs_status", "ingestion_jobs", type_="check")
    op.drop_column("ingestion_jobs", "dead_lettered_at")
    op.drop_column("ingestion_jobs", "retryable")
    op.drop_column("ingestion_jobs", "failure_kind")
    op.drop_column("ingestion_jobs", "next_attempt_at")
    op.drop_column("ingestion_jobs", "heartbeat_at")
    op.drop_column("ingestion_jobs", "lease_expires_at")
    op.drop_column("ingestion_jobs", "lease_token")
    op.drop_column("ingestion_jobs", "max_attempts")
    op.drop_column("ingestion_jobs", "attempt_count")
