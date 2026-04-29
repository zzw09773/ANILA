"""Add ``action_function*`` schema for ANILA Functions v1.

Five tables that together support the OpenWebUI-style "Action functions"
feature (assistant-message-bound dev-authored buttons):

* ``action_functions`` — main metadata row per Function
* ``action_function_versions`` — append-only code history (UPDATE/DELETE
  rejected by trigger so audit trail stays immutable)
* ``action_function_valves`` — admin-set parameters, AES-256-GCM at rest
* ``action_function_runs`` — every Action button click + Test Console run
  (audit log; 360-day retention purge runs separately)
* ``action_function_reports`` — abuse reports filed by users

Status enum has four values: ``draft``, ``enabled``, ``disabled``,
``quarantined``. Quarantine is the admin's "abuse suspected" state —
code becomes invisible to non-author developers (see spec §3.6 / §7.1).

``action_functions.latest_version_id`` is a denormalized cache of the
most recent version's ``id``. Deliberately **not** an FK to break the
otherwise circular relationship with ``action_function_versions``
(versions FK functions; if functions FK versions you can't insert
either first). Read paths LEFT JOIN versions and treat missing as
"no saved version yet".

The ``pg_advisory_xact_lock`` namespace key for this table family is
``42`` (used by ``app/services/action_function/crud.py:save_version``
to serialize concurrent saves on the same function and avoid
``unique(function_id, version_no)`` violations).

Revision ID: 0025
Revises: 0024
Create Date: 2026-04-29
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0025"
down_revision: Union[str, None] = "0024"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── 1. action_functions ─────────────────────────────────────────────
    op.create_table(
        "action_functions",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("slug", sa.String(64), nullable=False, unique=True),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("icon_data_url", sa.Text, nullable=True),
        sa.Column(
            "author_user_id",
            sa.BigInteger,
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum(
                "draft",
                "enabled",
                "disabled",
                "quarantined",
                name="action_function_status",
            ),
            nullable=False,
            server_default="draft",
        ),
        sa.Column("disabled_reason", sa.Text, nullable=True),
        # NO FK on latest_version_id — denormalized cache, avoid circular FK
        sa.Column("latest_version_id", sa.BigInteger, nullable=True),
        sa.Column(
            "forked_from_id",
            sa.BigInteger,
            sa.ForeignKey("action_functions.id"),
            nullable=True,
        ),
        sa.Column(
            "tags",
            postgresql.ARRAY(sa.Text),
            nullable=False,
            server_default="{}",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_action_functions_status", "action_functions", ["status"]
    )
    op.create_index(
        "ix_action_functions_author", "action_functions", ["author_user_id"]
    )

    # ── 2. action_function_versions (append-only) ───────────────────────
    op.create_table(
        "action_function_versions",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column(
            "function_id",
            sa.BigInteger,
            sa.ForeignKey("action_functions.id"),
            nullable=False,
        ),
        sa.Column("version_no", sa.Integer, nullable=False),
        sa.Column("code", sa.Text, nullable=False),
        sa.Column(
            "metadata_json",
            postgresql.JSONB,
            nullable=False,
            server_default="{}",
        ),
        sa.Column(
            "actions_meta_json",
            postgresql.JSONB,
            nullable=False,
            server_default="[]",
        ),
        sa.Column(
            "valves_schema_json",
            postgresql.JSONB,
            nullable=False,
            server_default="{}",
        ),
        sa.Column(
            "editor_user_id",
            sa.BigInteger,
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column("commit_message", sa.Text, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "function_id", "version_no", name="uq_function_version_no"
        ),
    )

    # ── 3. action_function_valves (encrypted) ───────────────────────────
    op.create_table(
        "action_function_valves",
        sa.Column(
            "function_id",
            sa.BigInteger,
            sa.ForeignKey("action_functions.id"),
            primary_key=True,
        ),
        sa.Column("values_encrypted", sa.LargeBinary, nullable=False),
        sa.Column("nonce", sa.LargeBinary, nullable=False),
        sa.Column(
            "key_version", sa.Integer, nullable=False, server_default="1"
        ),
        sa.Column(
            "updated_by",
            sa.BigInteger,
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )

    # ── 4. action_function_runs (audit) ─────────────────────────────────
    op.create_table(
        "action_function_runs",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column(
            "function_id",
            sa.BigInteger,
            sa.ForeignKey("action_functions.id"),
            nullable=False,
        ),
        sa.Column("version_no", sa.Integer, nullable=False),
        sa.Column("action_id", sa.Text, nullable=False),
        sa.Column(
            "triggered_by_user_id",
            sa.BigInteger,
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column(
            "context_type",
            sa.Enum(
                "chat_message",
                "test_console",
                name="action_function_run_context",
            ),
            nullable=False,
        ),
        sa.Column(
            "conversation_id",
            sa.BigInteger,
            sa.ForeignKey("conversations.id"),
            nullable=True,
        ),
        sa.Column(
            "message_id",
            sa.BigInteger,
            sa.ForeignKey("messages.id"),
            nullable=True,
        ),
        sa.Column(
            "request_payload_json",
            postgresql.JSONB,
            nullable=False,
            server_default="{}",
        ),
        sa.Column(
            "status",
            sa.Enum(
                "queued",
                "running",
                "success",
                "error",
                "timeout",
                name="action_function_run_status",
            ),
            nullable=False,
        ),
        sa.Column("error_message", sa.Text, nullable=True),
        sa.Column("duration_ms", sa.Integer, nullable=True),
        sa.Column(
            "events_json",
            postgresql.JSONB,
            nullable=False,
            server_default="[]",
        ),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_runs_function_started",
        "action_function_runs",
        ["function_id", "started_at"],
    )
    op.create_index(
        "ix_runs_user_started",
        "action_function_runs",
        ["triggered_by_user_id", "started_at"],
    )
    op.create_index(
        "ix_runs_conversation",
        "action_function_runs",
        ["conversation_id"],
    )

    # ── 5. action_function_reports ──────────────────────────────────────
    op.create_table(
        "action_function_reports",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column(
            "function_id",
            sa.BigInteger,
            sa.ForeignKey("action_functions.id"),
            nullable=False,
        ),
        sa.Column(
            "reporter_user_id",
            sa.BigInteger,
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column("reason", sa.Text, nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "open",
                "acknowledged",
                "dismissed",
                "actioned",
                name="action_function_report_status",
            ),
            nullable=False,
            server_default="open",
        ),
        sa.Column(
            "acknowledged_by",
            sa.BigInteger,
            sa.ForeignKey("users.id"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_reports_function_status",
        "action_function_reports",
        ["function_id", "status"],
    )
    op.create_index(
        "ix_reports_status_created",
        "action_function_reports",
        ["status", "created_at"],
    )

    # ── 6. Append-only trigger on action_function_versions ──────────────
    op.execute(
        """
        CREATE OR REPLACE FUNCTION action_function_versions_immutable()
        RETURNS TRIGGER AS $$
        BEGIN
            RAISE EXCEPTION
                'action_function_versions is append-only; UPDATE/DELETE not allowed';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_action_function_versions_immutable
        BEFORE UPDATE OR DELETE ON action_function_versions
        FOR EACH ROW EXECUTE FUNCTION action_function_versions_immutable();
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS trg_action_function_versions_immutable "
        "ON action_function_versions"
    )
    op.execute("DROP FUNCTION IF EXISTS action_function_versions_immutable()")
    op.drop_table("action_function_reports")
    op.drop_table("action_function_runs")
    op.drop_table("action_function_valves")
    op.drop_table("action_function_versions")
    op.drop_table("action_functions")
    # Postgres ENUM types must be dropped explicitly after their tables
    op.execute("DROP TYPE IF EXISTS action_function_status")
    op.execute("DROP TYPE IF EXISTS action_function_run_context")
    op.execute("DROP TYPE IF EXISTS action_function_run_status")
    op.execute("DROP TYPE IF EXISTS action_function_report_status")
