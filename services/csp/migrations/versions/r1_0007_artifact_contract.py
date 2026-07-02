# -*- coding: utf-8 -*-
"""Slice 8a — Artifact contract (doc 02 ArtifactJob, doc 01 Artifact/Version/
Export, doc 08 §5).

Four tables backing the Studio artifact contract (doc 02 §8 blocker: Studio
five job pipelines keep state in process memory → violates the failure model
"restart 不丟 job"). Studio reports over HTTP with its service token (doc 10
§12: studio 不直讀 CSP DB), so the durable state lives here:

- ``artifacts``          : doc 01 Artifact + doc 08 §5 four common
  classification columns (binding rule: source_task_id OR source_snapshot_id).
- ``artifact_versions``  : doc 01 ArtifactVersion (version increments;
  per-version effective classification record).
- ``export_records``     : doc 01 ExportRecord + doc 08 §5 four common
  classification columns (only classification-policy-passed exports land).
- ``artifact_jobs``      : doc 02 ArtifactJob schema verbatim (job_id string
  PK = studio uuid; four-value status; NOT a doc 08 §5 classification
  resource, so no classification columns).

FK cycle avoidance (mirrors ``tasks.source_snapshot_id``): ``artifacts.job_id``
is a plain string reference (no FK) while ``artifact_jobs.artifact_id`` holds
the FK back to ``artifacts`` — a two-way FK would form a cycle. So the create
order is artifacts → artifact_versions → export_records → artifact_jobs.

Revision ID: r1_0007
Revises: r1_0006
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "r1_0007"
down_revision: Union[str, None] = "r1_0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Postgres → JSONB, everything else → JSON (SQLite offline runs).
_JSON = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
_EMPTY_LIST = sa.text("'[]'")
_UNCLASSIFIED = "無機密"


def upgrade() -> None:
    # ── artifacts (doc 01 + doc 08 §5 four common classification columns) ─────
    op.create_table(
        "artifacts",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("artifact_type", sa.String(length=32), nullable=False),
        sa.Column(
            "title", sa.String(length=500), nullable=False,
            server_default="未命名產出",
        ),
        sa.Column(
            "status", sa.String(length=20), nullable=False,
            server_default="completed",
        ),
        sa.Column(
            "owner_user_id", sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
        ),
        sa.Column(
            "source_task_id", sa.Integer(),
            sa.ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True,
        ),
        sa.Column(
            "source_snapshot_id", sa.Integer(),
            sa.ForeignKey("source_snapshots.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("job_id", sa.String(length=64), nullable=True),
        sa.Column(
            "current_version", sa.Integer(), nullable=False, server_default="1"
        ),
        sa.Column("trace_id", sa.String(length=64), nullable=True),
        sa.Column("metadata_json", _JSON, nullable=True),
        sa.Column(
            "classification_level", sa.String(length=20), nullable=False,
            server_default=_UNCLASSIFIED,
        ),
        sa.Column("classification_latched_at", sa.DateTime(), nullable=True),
        sa.Column("classification_source", sa.String(length=50), nullable=True),
        sa.Column(
            "classification_event_id", sa.Integer(),
            sa.ForeignKey("classification_events.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_artifacts_source_task_id", "artifacts", ["source_task_id"])
    op.create_index("ix_artifacts_artifact_type", "artifacts", ["artifact_type"])
    op.create_index("ix_artifacts_owner_user_id", "artifacts", ["owner_user_id"])
    op.create_index("ix_artifacts_job_id", "artifacts", ["job_id"])
    op.create_index("ix_artifacts_trace_id", "artifacts", ["trace_id"])

    # ── artifact_versions ────────────────────────────────────────────────────
    op.create_table(
        "artifact_versions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "artifact_id", sa.Integer(),
            sa.ForeignKey("artifacts.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("storage_ref", sa.String(length=1000), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=True),
        sa.Column("file_refs", _JSON, nullable=False, server_default=_EMPTY_LIST),
        sa.Column("citation_map", _JSON, nullable=True),
        sa.Column("generated_by_model_id", sa.Integer(), nullable=True),
        sa.Column("generated_by_agent_id", sa.Integer(), nullable=True),
        sa.Column(
            "generated_by_studio_job_id", sa.String(length=64), nullable=True
        ),
        sa.Column(
            "classification_level", sa.String(length=20), nullable=False,
            server_default=_UNCLASSIFIED,
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint(
            "artifact_id", "version",
            name="uq_artifact_versions_artifact_version",
        ),
    )
    op.create_index(
        "ix_artifact_versions_artifact_id", "artifact_versions", ["artifact_id"]
    )

    # ── export_records (doc 01 + doc 08 §5 four common classification cols) ───
    op.create_table(
        "export_records",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "artifact_id", sa.Integer(),
            sa.ForeignKey("artifacts.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column(
            "artifact_version_id", sa.Integer(),
            sa.ForeignKey("artifact_versions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "exporter_user_id", sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
        ),
        sa.Column("exporter_employee_id", sa.String(length=32), nullable=True),
        sa.Column("target_space", sa.String(length=100), nullable=True),
        sa.Column(
            "target_classification_floor", sa.String(length=20), nullable=True
        ),
        sa.Column("export_format", sa.String(length=32), nullable=True),
        sa.Column(
            "policy_decision_id", sa.Integer(),
            sa.ForeignKey("policy_decisions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "decision", sa.String(length=20), nullable=False,
            server_default="allow",
        ),
        sa.Column("trace_id", sa.String(length=64), nullable=True),
        sa.Column(
            "classification_level", sa.String(length=20), nullable=False,
            server_default=_UNCLASSIFIED,
        ),
        sa.Column("classification_latched_at", sa.DateTime(), nullable=True),
        sa.Column("classification_source", sa.String(length=50), nullable=True),
        sa.Column(
            "classification_event_id", sa.Integer(),
            sa.ForeignKey("classification_events.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index(
        "ix_export_records_artifact_id", "export_records", ["artifact_id"]
    )

    # ── artifact_jobs (doc 02 ArtifactJob verbatim; created last for the FK) ──
    op.create_table(
        "artifact_jobs",
        sa.Column("job_id", sa.String(length=64), primary_key=True),
        sa.Column(
            "owner_user_id", sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
        ),
        sa.Column("requester_employee_id", sa.String(length=32), nullable=True),
        sa.Column("collection_id", sa.Integer(), nullable=True),
        sa.Column(
            "task_id", sa.Integer(),
            sa.ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True,
        ),
        sa.Column(
            "source_snapshot_id", sa.Integer(),
            sa.ForeignKey("source_snapshots.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("artifact_type", sa.String(length=32), nullable=False),
        sa.Column(
            "status", sa.String(length=20), nullable=False,
            server_default="queued",
        ),
        sa.Column("progress", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("result_metadata", _JSON, nullable=True),
        sa.Column(
            "artifact_files", _JSON, nullable=False, server_default=_EMPTY_LIST
        ),
        sa.Column("error", _JSON, nullable=True),
        sa.Column("params_digest", sa.String(length=64), nullable=True),
        sa.Column("trace_id", sa.String(length=64), nullable=True),
        sa.Column(
            "artifact_id", sa.Integer(),
            sa.ForeignKey("artifacts.id", ondelete="SET NULL"), nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_artifact_jobs_status", "artifact_jobs", ["status"])
    op.create_index("ix_artifact_jobs_task_id", "artifact_jobs", ["task_id"])
    op.create_index(
        "ix_artifact_jobs_artifact_type", "artifact_jobs", ["artifact_type"]
    )
    op.create_index("ix_artifact_jobs_trace_id", "artifact_jobs", ["trace_id"])


def downgrade() -> None:
    op.drop_index("ix_artifact_jobs_trace_id", table_name="artifact_jobs")
    op.drop_index("ix_artifact_jobs_artifact_type", table_name="artifact_jobs")
    op.drop_index("ix_artifact_jobs_task_id", table_name="artifact_jobs")
    op.drop_index("ix_artifact_jobs_status", table_name="artifact_jobs")
    op.drop_table("artifact_jobs")

    op.drop_index("ix_export_records_artifact_id", table_name="export_records")
    op.drop_table("export_records")

    op.drop_index(
        "ix_artifact_versions_artifact_id", table_name="artifact_versions"
    )
    op.drop_table("artifact_versions")

    op.drop_index("ix_artifacts_trace_id", table_name="artifacts")
    op.drop_index("ix_artifacts_job_id", table_name="artifacts")
    op.drop_index("ix_artifacts_owner_user_id", table_name="artifacts")
    op.drop_index("ix_artifacts_artifact_type", table_name="artifacts")
    op.drop_index("ix_artifacts_source_task_id", table_name="artifacts")
    op.drop_table("artifacts")
