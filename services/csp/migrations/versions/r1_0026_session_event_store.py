"""Create the durable Gate 5 SessionEventStore ledger.

Revision ID: r1_0026
Revises: r1_0025
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "r1_0026"
down_revision: Union[str, None] = "r1_0025"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_JSON = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "session_event_runs",
        sa.Column("run_id", sa.String(length=255), nullable=False),
        sa.Column("task_id", sa.String(length=255), nullable=False),
        sa.Column("trace_id", sa.String(length=255), nullable=False),
        sa.Column("agent_id", sa.String(length=255), nullable=False),
        sa.Column("session_id", sa.String(length=255), nullable=False),
        sa.Column("next_cursor", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_source_sequence", sa.Integer(), nullable=True),
        sa.Column("terminal_event_id", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("run_id"),
    )
    op.create_index("ix_session_event_runs_task_id", "session_event_runs", ["task_id"])
    op.create_index("ix_session_event_runs_trace_id", "session_event_runs", ["trace_id"])
    op.create_index("ix_session_event_runs_agent_id", "session_event_runs", ["agent_id"])
    op.create_index("ix_session_event_runs_session_id", "session_event_runs", ["session_id"])

    op.create_table(
        "session_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.String(length=255), nullable=False),
        sa.Column("task_id", sa.String(length=255), nullable=False),
        sa.Column("trace_id", sa.String(length=255), nullable=False),
        sa.Column("agent_id", sa.String(length=255), nullable=False),
        sa.Column("session_id", sa.String(length=255), nullable=False),
        sa.Column("event_id", sa.String(length=255), nullable=False),
        sa.Column("cursor", sa.Integer(), nullable=False),
        sa.Column("source_sequence", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("is_terminal", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("payload_sha256", sa.String(length=64), nullable=False),
        sa.Column("payload", _JSON, nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["session_event_runs.run_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "event_id", name="uq_session_events_run_event_id"),
        sa.UniqueConstraint("run_id", "cursor", name="uq_session_events_run_cursor"),
    )
    for name, column in (
        ("ix_session_events_run_id", "run_id"),
        ("ix_session_events_task_id", "task_id"),
        ("ix_session_events_trace_id", "trace_id"),
        ("ix_session_events_agent_id", "agent_id"),
        ("ix_session_events_session_id", "session_id"),
    ):
        op.create_index(name, "session_events", [column])
    op.create_index(
        "ix_session_events_binding_cursor",
        "session_events",
        ["task_id", "trace_id", "agent_id", "session_id", "run_id", "cursor"],
    )


def downgrade() -> None:
    op.drop_index("ix_session_events_binding_cursor", table_name="session_events")
    for name in (
        "ix_session_events_session_id",
        "ix_session_events_agent_id",
        "ix_session_events_trace_id",
        "ix_session_events_task_id",
        "ix_session_events_run_id",
    ):
        op.drop_index(name, table_name="session_events")
    op.drop_table("session_events")
    for name in (
        "ix_session_event_runs_session_id",
        "ix_session_event_runs_agent_id",
        "ix_session_event_runs_trace_id",
        "ix_session_event_runs_task_id",
    ):
        op.drop_index(name, table_name="session_event_runs")
    op.drop_table("session_event_runs")
