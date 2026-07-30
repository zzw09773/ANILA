# -*- coding: utf-8 -*-
"""Convert remaining TIMESTAMP WITHOUT TIME ZONE columns to timestamptz.

Revision ID: r1_0023
Revises: r1_0022
Create Date: 2026-07-31

X.3 — every timestamp in the UI is eight hours out.

Assumption about existing rows
------------------------------
Every naive value is a **UTC wall clock**. Writers are
``datetime.now(timezone.utc)`` (and ``server_default CURRENT_TIMESTAMP``
under session TZ=UTC). There are zero bare ``datetime.now()`` call sites
in CSP. Therefore:

    ALTER COLUMN x TYPE timestamptz USING x AT TIME ZONE 'UTC'

preserves the absolute instant (naive ``2026-07-30 08:00:00`` becomes
``2026-07-30T08:00:00+00:00``). Interpreting as Asia/Taipei would shift
every historical row eight hours earlier — rejected (see attic eb741bc).

Columns already ``timestamptz`` on the clean alembic chain are left alone;
only their ORM declarations were corrected to ``DateTime(timezone=True)``.

This revision converts up to 90 columns. ``upgrade``/``downgrade``
skip any column that is missing or already the target type, so the same
revision is safe on:

* a clean alembic chain (82 naive columns at r1_0022), and
* a live DB that grew extra ORM-aligned columns
  (``api_keys.expires_at``, alert first/last seen, ``*.updated_at``) via
  earlier startup DDL — those eight are included in ``_CONVERT`` and
  converted when present.

Database is disposable before go-live (PLAN 0.4); no elaborate backfill.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "r1_0023"
down_revision: Union[str, None] = "r1_0022"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Naive values are UTC wall clocks — see module docstring.
_INTERPRETATION_TZ = "UTC"

_CONVERT: tuple[tuple[str, str], ...] = (
    ("agent_credentials", "created_at"),
    ("agent_credentials", "revoked_at"),
    ("agent_credentials", "service_token_issued_at"),
    ("agent_credentials", "service_token_previous_expires_at"),
    ("agent_credentials", "service_token_rotated_at"),
    ("agents", "bootstrap_token_consumed_at"),
    ("agents", "bootstrap_token_expires_at"),
    ("artifact_jobs", "created_at"),
    ("artifact_jobs", "expires_at"),
    ("artifact_jobs", "updated_at"),
    ("artifact_versions", "created_at"),
    ("artifacts", "classification_latched_at"),
    ("artifacts", "created_at"),
    ("artifacts", "updated_at"),
    ("attachments", "extracted_at"),
    ("citations", "created_at"),
    ("classification_authority_assignments", "created_at"),
    ("classification_authority_assignments", "revoked_at"),
    ("classification_events", "created_at"),
    ("conversations", "classification_latched_at"),
    ("declassification_requests", "created_at"),
    ("declassification_requests", "decided_at"),
    ("dev_db_credentials", "expires_at"),
    ("dev_db_credentials", "issued_at"),
    ("dev_db_credentials", "reminder_sent_at"),
    ("dev_db_credentials", "revoked_at"),
    ("document_chunks", "classification_latched_at"),
    ("document_chunks", "created_at"),
    ("endpoint_author_grants", "granted_at"),
    ("endpoint_author_grants", "revoked_at"),
    ("export_records", "classification_latched_at"),
    ("export_records", "created_at"),
    ("ingestion_collections", "classification_latched_at"),
    ("ingestion_collections", "created_at"),
    ("ingestion_collections", "updated_at"),
    ("ingestion_documents", "classification_latched_at"),
    ("ingestion_documents", "indexed_at"),
    ("ingestion_documents", "uploaded_at"),
    ("ingestion_eval_runs", "completed_at"),
    ("ingestion_eval_runs", "created_at"),
    ("ingestion_eval_runs", "started_at"),
    ("ingestion_images", "created_at"),
    ("ingestion_images", "updated_at"),
    ("ingestion_jobs", "completed_at"),
    ("ingestion_jobs", "enqueued_at"),
    ("ingestion_jobs", "started_at"),
    ("message_action_bindings", "created_at"),
    ("message_actions", "created_at"),
    ("message_actions", "updated_at"),
    ("messages", "classification_latched_at"),
    ("policy_decisions", "created_at"),
    ("registered_services", "created_at"),
    ("registered_services", "last_seeded_at"),
    ("registered_services", "updated_at"),
    ("service_access_grants", "granted_at"),
    ("service_access_grants", "revoked_at"),
    ("service_audit_callbacks", "received_at"),
    ("service_clients", "created_at"),
    ("service_clients", "revoked_at"),
    ("service_clients", "service_token_issued_at"),
    ("service_clients", "service_token_previous_expires_at"),
    ("service_clients", "service_token_rotated_at"),
    ("service_launches", "consumed_at"),
    ("service_launches", "expires_at"),
    ("service_launches", "issued_at"),
    ("service_project_bindings", "created_at"),
    ("source_snapshots", "classification_latched_at"),
    ("source_snapshots", "created_at"),
    ("task_runs", "classification_latched_at"),
    ("task_runs", "created_at"),
    ("task_runs", "finished_at"),
    ("task_runs", "started_at"),
    ("tasks", "classification_latched_at"),
    ("tasks", "created_at"),
    ("tasks", "updated_at"),
    ("token_usage", "request_timestamp"),
    ("trusted_hosts", "created_at"),
    ("unit_admin_assignments", "granted_at"),
    ("unit_admin_assignments", "revoked_at"),
    ("user_llm_credentials", "created_at"),
    ("user_llm_credentials", "last_used_at"),
    ("users", "last_login_at"),
    ("alerts", "acknowledged_at"),
    ("alerts", "first_seen_at"),
    ("alerts", "last_seen_at"),
    ("alerts", "resolved_at"),
    ("api_keys", "expires_at"),
    ("departments", "updated_at"),
    ("model_registry", "updated_at"),
    ("users", "updated_at"),
)


def _column_data_type(bind, table: str, column: str) -> str | None:
    row = bind.execute(
        sa.text(
            """
            SELECT data_type
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = :table
              AND column_name = :column
            """
        ),
        {"table": table, "column": column},
    ).fetchone()
    return None if row is None else row[0]


def upgrade() -> None:
    # Fail fast on lock contention rather than queue behind a long txn.
    op.execute("SET LOCAL lock_timeout = '15s'")
    bind = op.get_bind()
    for table, column in _CONVERT:
        dtype = _column_data_type(bind, table, column)
        if dtype is None:
            continue  # clean chain lacks live-only columns
        if dtype != "timestamp without time zone":
            continue  # already timestamptz (or unexpected)
        op.execute(
            f'ALTER TABLE "{table}" ALTER COLUMN "{column}" '
            f"TYPE timestamptz USING \"{column}\" AT TIME ZONE '{_INTERPRETATION_TZ}'"
        )


def downgrade() -> None:
    """Restore naive UTC wall clocks (inverse of AT TIME ZONE UTC).

    Only touches columns this revision would have converted — i.e. present
    and currently ``timestamp with time zone``. Does not demote columns that
    were already timestamptz before r1_0023.
    """
    op.execute("SET LOCAL lock_timeout = '15s'")
    bind = op.get_bind()
    # Track which ones we actually altered on the way up is not persisted;
    # demote only members of _CONVERT that are currently timestamptz. On a
    # clean chain the pre-existing timestamptz columns are NOT in _CONVERT.
    for table, column in reversed(_CONVERT):
        dtype = _column_data_type(bind, table, column)
        if dtype != "timestamp with time zone":
            continue
        op.execute(
            f'ALTER TABLE "{table}" ALTER COLUMN "{column}" '
            f"TYPE timestamp WITHOUT TIME ZONE USING \"{column}\" AT TIME ZONE '{_INTERPRETATION_TZ}'"
        )
