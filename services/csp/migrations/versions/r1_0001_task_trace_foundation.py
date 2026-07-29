# -*- coding: utf-8 -*-
"""Slice 2a — Task / Trace / Policy 六表基礎(redesign r1 系列首發)。

依 docs/anila-redesign-docs/01(Task 主脊椎、SourceSnapshot 三規則)、
03 §5(PolicyDecision append-only + 可查詢索引)、05 §6 / 09(TraceSpan)。

- tasks / task_runs / source_snapshots / citations / policy_decisions /
  trace_spans 六表;enum 欄位一律開放 String(封閉 enum 在 Pydantic 契約
  層把關),JSON 走 with_variant(JSONB on PG、JSON on SQLite)——
  可攜 DDL,不用 PG 原生 enum / extension。
- 每表 classification_level 預設 '無機密'(四級繁中字串)。
- tasks.source_snapshot_id / policy_decision_id 不掛 FK:對向表都有
  task_id FK 指回 tasks,雙向掛會循環相依;service 層維護(Slice 2b)。
- downgrade 依 FK 反序卸表(先子後母)。

Revision ID: r1_0001
Revises: 0046
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "r1_0001"
down_revision: Union[str, None] = "0046"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# JSONB on Postgres, JSON on SQLite — 同 0029 / app/models 的 JSONValue 模式。
json_type = sa.JSON().with_variant(JSONB, "postgresql")

_UNCLASSIFIED = "無機密"


def _classification_column() -> sa.Column:
    return sa.Column(
        "classification_level",
        sa.String(length=20),
        nullable=False,
        server_default=_UNCLASSIFIED,
    )


def _created_at_column() -> sa.Column:
    return sa.Column(
        "created_at",
        sa.DateTime(),
        nullable=False,
        server_default=sa.text("now()"),
    )


def upgrade() -> None:
    # ── tasks(主脊椎;FK: users / departments / conversations)──────────
    op.create_table(
        "tasks",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("title", sa.String(length=255), nullable=False,
                  server_default="新任務"),
        sa.Column("task_type", sa.String(length=32), nullable=False),
        sa.Column("requester_user_id", sa.Integer(), nullable=False),
        sa.Column("department_id", sa.Integer(), nullable=True),
        sa.Column("conversation_id", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False,
                  server_default="draft"),
        sa.Column("source_scope", sa.String(length=32), nullable=False,
                  server_default="none"),
        sa.Column("selected_collection_ids", json_type, nullable=False,
                  server_default=sa.text("'[]'")),
        sa.Column("selected_service_id", sa.String(length=100), nullable=True),
        sa.Column("requested_output_type", sa.String(length=32),
                  nullable=True),
        sa.Column("source_snapshot_id", sa.Integer(), nullable=True),
        sa.Column("policy_decision_id", sa.Integer(), nullable=True),
        sa.Column("legacy_runtime_call", sa.Boolean(), nullable=False,
                  server_default="false"),
        _classification_column(),
        sa.Column("trace_id", sa.String(length=64), nullable=False),
        _created_at_column(),
        sa.Column("updated_at", sa.DateTime(), nullable=False,
                  server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(
            ["requester_user_id"], ["users.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["department_id"], ["departments.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"], ["conversations.id"], ondelete="SET NULL"
        ),
    )
    op.create_index("ix_tasks_requester_user_id", "tasks",
                    ["requester_user_id"])
    op.create_index("ix_tasks_status", "tasks", ["status"])
    op.create_index("ix_tasks_trace_id", "tasks", ["trace_id"], unique=True)

    # ── task_runs(FK: tasks / token_usage)────────────────────────────
    op.create_table(
        "task_runs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("task_id", sa.Integer(), nullable=False),
        sa.Column("run_sequence", sa.Integer(), nullable=False,
                  server_default="1"),
        sa.Column("dispatch_target", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False,
                  server_default="queued"),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("usage_record_id", sa.Integer(), nullable=True),
        sa.Column("error", json_type, nullable=True),
        _classification_column(),
        _created_at_column(),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"],
                                ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["usage_record_id"], ["token_usage.id"],
                                ondelete="SET NULL"),
        sa.UniqueConstraint("task_id", "run_sequence",
                            name="uq_task_runs_task_sequence"),
    )
    op.create_index("ix_task_runs_task_id", "task_runs", ["task_id"])

    # ── source_snapshots(FK: tasks)───────────────────────────────────
    op.create_table(
        "source_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("task_id", sa.Integer(), nullable=False),
        sa.Column("origin", sa.String(length=32), nullable=False,
                  server_default="none"),
        sa.Column("source_scope", sa.String(length=32), nullable=False,
                  server_default="none"),
        sa.Column("collection_ids", json_type, nullable=False,
                  server_default=sa.text("'[]'")),
        sa.Column("document_ids", json_type, nullable=False,
                  server_default=sa.text("'[]'")),
        sa.Column("chunk_ids", json_type, nullable=False,
                  server_default=sa.text("'[]'")),
        sa.Column("document_versions", json_type, nullable=True),
        sa.Column("retrieval_queries", json_type, nullable=False,
                  server_default=sa.text("'[]'")),
        sa.Column("content_hash", sa.String(length=64), nullable=True),
        sa.Column("payload_ref", sa.String(length=1000), nullable=True),
        _classification_column(),
        _created_at_column(),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"],
                                ondelete="CASCADE"),
    )
    op.create_index("ix_source_snapshots_task_id", "source_snapshots",
                    ["task_id"])

    # ── citations(FK: source_snapshots;不掛 live document FK,規則 2)─
    op.create_table(
        "citations",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("source_snapshot_id", sa.Integer(), nullable=False),
        sa.Column("document_id", sa.Integer(), nullable=True),
        sa.Column("chunk_id", sa.String(length=128), nullable=False),
        sa.Column("quote_preview", sa.Text(), nullable=True),
        sa.Column("page", sa.Integer(), nullable=True),
        sa.Column("score", sa.Float(), nullable=True),
        sa.Column("span_start", sa.Integer(), nullable=True),
        sa.Column("span_end", sa.Integer(), nullable=True),
        sa.Column("used_by", sa.String(length=20), nullable=False,
                  server_default="answer"),
        _classification_column(),
        _created_at_column(),
        sa.ForeignKeyConstraint(["source_snapshot_id"],
                                ["source_snapshots.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_citations_source_snapshot_id", "citations",
                    ["source_snapshot_id"])

    # ── policy_decisions(append-only;FK: tasks SET NULL 保留裁決史)──
    op.create_table(
        "policy_decisions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("task_id", sa.Integer(), nullable=True),
        sa.Column("actor_type", sa.String(length=16), nullable=False),
        sa.Column("actor_id", sa.Integer(), nullable=True),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("resource_type", sa.String(length=50), nullable=False),
        sa.Column("resource_id", sa.String(length=100), nullable=True),
        sa.Column("decision", sa.String(length=20), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("matched_policy_ids", json_type, nullable=False,
                  server_default=sa.text("'[]'")),
        sa.Column("policy_version", sa.String(length=32), nullable=True),
        sa.Column("metadata_json", json_type, nullable=True),
        _classification_column(),
        _created_at_column(),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"],
                                ondelete="SET NULL"),
    )
    op.create_index("ix_policy_decisions_action_created_at",
                    "policy_decisions", ["action", "created_at"])
    op.create_index("ix_policy_decisions_task_id", "policy_decisions",
                    ["task_id"])

    # ── trace_spans(FK: tasks SET NULL 保留觀測史)─────────────────────
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
        sa.Column("status", sa.String(length=20), nullable=False,
                  server_default="ok"),
        sa.Column("attributes", json_type, nullable=True),
        sa.Column("producer", sa.String(length=20), nullable=False),
        _classification_column(),
        _created_at_column(),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"],
                                ondelete="SET NULL"),
    )
    op.create_index("ix_trace_spans_trace_id", "trace_spans", ["trace_id"])
    op.create_index("ix_trace_spans_task_id", "trace_spans", ["task_id"])
    op.create_index("uq_trace_spans_trace_span", "trace_spans",
                    ["trace_id", "span_id"], unique=True)


def downgrade() -> None:
    # FK 反序:先卸掛在 tasks / source_snapshots 上的子表,最後卸 tasks。
    op.drop_table("trace_spans")
    op.drop_table("policy_decisions")
    op.drop_table("citations")
    op.drop_table("source_snapshots")
    op.drop_table("task_runs")
    op.drop_table("tasks")
