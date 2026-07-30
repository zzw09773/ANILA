# -*- coding: utf-8 -*-
"""D1 — drop the Full Trace span subsystem.

SYSTEM-MAP §7 rejects span trees / parent links / trace-id reporting.
OE-2 package D1; OE-1 already removed the approval hard-gate that stamped
``trace_test_passed_at`` via span polling.

Drops:
  - table ``trace_spans`` (and its indexes / FK)
  - columns ``agents.trace_test_passed_at``, ``agents.trace_test_report``

Keeps ``tasks.trace_id`` as a correlation id for Task rows — that is not the
span tree. Existing rows go with the table (DB disposable before go-live,
PLAN 0.4).

Revision ID: r1_0021
Revises: r1_0020
Create Date: 2026-07-30
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "r1_0021"
down_revision: Union[str, None] = "r1_0020"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_table("trace_spans")
    op.drop_column("agents", "trace_test_report")
    op.drop_column("agents", "trace_test_passed_at")


def downgrade() -> None:
    json_type = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
    op.add_column(
        "agents",
        sa.Column("trace_test_passed_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "agents",
        sa.Column("trace_test_report", json_type, nullable=True),
    )
    op.create_table(
        "trace_spans",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("trace_id", sa.String(length=64), nullable=False),
        sa.Column("span_id", sa.String(length=64), nullable=False),
        sa.Column("parent_span_id", sa.String(length=64), nullable=True),
        sa.Column("task_id", sa.Integer(), nullable=True),
        sa.Column("span_type", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("ended_at", sa.DateTime(), nullable=True),
        sa.Column(
            "status",
            sa.String(length=20),
            nullable=False,
            server_default="ok",
        ),
        sa.Column("attributes", json_type, nullable=True),
        sa.Column("producer", sa.String(length=20), nullable=False),
        sa.Column(
            "classification_level",
            sa.String(length=20),
            nullable=False,
            server_default="無機密",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="SET NULL"),
    )
    op.create_index("ix_trace_spans_trace_id", "trace_spans", ["trace_id"])
    op.create_index("ix_trace_spans_task_id", "trace_spans", ["task_id"])
    op.create_index(
        "uq_trace_spans_trace_span",
        "trace_spans",
        ["trace_id", "span_id"],
        unique=True,
    )
