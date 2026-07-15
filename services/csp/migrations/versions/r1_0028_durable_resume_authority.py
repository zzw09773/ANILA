"""Persist CSP resume authority and per-cursor idempotency claims.

Revision ID: r1_0028
Revises: r1_0027
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "r1_0028"
down_revision: Union[str, None] = "r1_0027"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Keep SQLite migration tests portable while matching the ORM JSONB
    # variant used by every CSP authority model in PostgreSQL.
    json_type = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
    op.create_table(
        "resume_authorities",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column("task_id", sa.Integer(), nullable=False),
        sa.Column("caller_user_id", sa.Integer(), nullable=False),
        sa.Column("owner_id", sa.Integer(), nullable=False),
        sa.Column("source_snapshot_id", sa.Integer(), nullable=False),
        sa.Column("trace_id", sa.String(length=255), nullable=False),
        sa.Column("invocation_id", sa.String(length=255), nullable=False),
        sa.Column("session_id", sa.String(length=255), nullable=False),
        sa.Column("agent_db_id", sa.Integer(), nullable=False),
        sa.Column("agent_id", sa.String(length=255), nullable=False),
        sa.Column("classification", sa.String(length=20), nullable=False),
        sa.Column("registry_snapshot_id", sa.String(length=255), nullable=False),
        sa.Column("registry_snapshot_revision", sa.String(length=255), nullable=False),
        sa.Column("registry_snapshot_hash", sa.String(length=255), nullable=False),
        sa.Column("manifest_revision", sa.String(length=255), nullable=False),
        sa.Column("manifest_sha256", sa.String(length=255), nullable=False),
        sa.Column("grant_id", sa.String(length=255), nullable=False),
        sa.Column("route_decision_id", sa.String(length=255), nullable=False),
        sa.Column("policy_decision_id", sa.String(length=255), nullable=False),
        sa.Column("auth_session_sid", sa.String(length=64), nullable=False),
        sa.Column("model_binding", json_type, nullable=False),
        sa.Column("capabilities", json_type, nullable=False),
        sa.Column("scopes", json_type, nullable=False),
        sa.Column("grant_json", json_type, nullable=False),
        sa.Column("grant_sha256", sa.String(length=64), nullable=False),
        sa.Column("blocked_cursor", sa.Integer(), nullable=False),
        sa.Column("lifecycle", sa.String(length=20), nullable=False, server_default="blocked"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("blocked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("terminal_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("terminal_cursor", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["run_id"], ["task_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["caller_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["agent_db_id"], ["agents.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", name="uq_resume_authorities_run"),
        sa.CheckConstraint(
            "lifecycle IN ('blocked', 'resuming', 'paused', 'completed', 'failed', 'cancelled')",
            name="ck_resume_authorities_lifecycle",
        ),
    )
    op.create_index("ix_resume_authorities_run_id", "resume_authorities", ["run_id"])
    op.create_index("ix_resume_authorities_task_id", "resume_authorities", ["task_id"])
    op.create_index("ix_resume_authorities_caller_user_id", "resume_authorities", ["caller_user_id"])
    op.create_index("ix_resume_authorities_owner_id", "resume_authorities", ["owner_id"])
    op.create_index("ix_resume_authorities_source_snapshot_id", "resume_authorities", ["source_snapshot_id"])
    op.create_index("ix_resume_authorities_trace_id", "resume_authorities", ["trace_id"])
    op.create_index("ix_resume_authorities_invocation_id", "resume_authorities", ["invocation_id"])
    op.create_index("ix_resume_authorities_session_id", "resume_authorities", ["session_id"])
    op.create_index("ix_resume_authorities_agent_db_id", "resume_authorities", ["agent_db_id"])
    op.create_index("ix_resume_authorities_agent_id", "resume_authorities", ["agent_id"])
    op.create_index("ix_resume_authorities_grant_id", "resume_authorities", ["grant_id"])
    op.create_index("ix_resume_authorities_auth_session_sid", "resume_authorities", ["auth_session_sid"])
    op.create_index("ix_resume_authorities_lifecycle", "resume_authorities", ["lifecycle"])
    op.create_index(
        "uq_resume_authorities_active_session",
        "resume_authorities",
        ["session_id"],
        unique=True,
        postgresql_where=sa.text("lifecycle IN ('blocked', 'resuming', 'paused')"),
        sqlite_where=sa.text("lifecycle IN ('blocked', 'resuming', 'paused')"),
    )

    op.create_table(
        "resume_attempts",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column("blocked_cursor", sa.Integer(), nullable=False),
        sa.Column("idempotency_key_sha256", sa.String(length=64), nullable=False),
        sa.Column("request_sha256", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="claimed"),
        # The raw lease token is never stored.  ``lease_generation`` is the
        # monotonic fence; the digest lets the current worker prove ownership
        # without exposing bearer material in the database.
        sa.Column("lease_token_sha256", sa.String(length=64), nullable=False),
        sa.Column("lease_generation", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("response_cursor", sa.Integer(), nullable=True),
        sa.Column("response_status", sa.String(length=20), nullable=True),
        sa.Column("response_meta", json_type, nullable=True),
        sa.ForeignKeyConstraint(["run_id"], ["task_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "blocked_cursor", name="uq_resume_attempts_run_cursor"),
        sa.CheckConstraint(
            "status IN ('claimed', 'paused', 'completed', 'failed', 'cancelled')",
            name="ck_resume_attempts_status",
        ),
    )
    op.create_index("ix_resume_attempts_run_id", "resume_attempts", ["run_id"])
    op.create_index("ix_resume_attempts_status", "resume_attempts", ["status"])
    op.create_index("ix_resume_attempts_run_created", "resume_attempts", ["run_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_resume_attempts_run_created", table_name="resume_attempts")
    op.drop_index("ix_resume_attempts_status", table_name="resume_attempts")
    op.drop_index("ix_resume_attempts_run_id", table_name="resume_attempts")
    op.drop_table("resume_attempts")

    op.drop_index("uq_resume_authorities_active_session", table_name="resume_authorities")
    for name in (
        "ix_resume_authorities_lifecycle",
        "ix_resume_authorities_auth_session_sid",
        "ix_resume_authorities_grant_id",
        "ix_resume_authorities_agent_id",
        "ix_resume_authorities_agent_db_id",
        "ix_resume_authorities_session_id",
        "ix_resume_authorities_invocation_id",
        "ix_resume_authorities_trace_id",
        "ix_resume_authorities_source_snapshot_id",
        "ix_resume_authorities_owner_id",
        "ix_resume_authorities_caller_user_id",
        "ix_resume_authorities_task_id",
        "ix_resume_authorities_run_id",
    ):
        op.drop_index(name, table_name="resume_authorities")
    op.drop_table("resume_authorities")
