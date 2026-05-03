"""Sprint 8 X / Phase A + G — agent_credentials, service_clients,
bootstrap-token columns on agents, and caller attribution on token_usage.

What this migration does
========================

1. ``agents`` — add 4 NULLable columns for the bootstrap-then-provision
   flow:

      bootstrap_token_hash         CHAR(64)   sha256 hex of bsk-* token
      bootstrap_token_expires_at   TIMESTAMP  default 15 min after issue
      bootstrap_token_consumed_at  TIMESTAMP  atomic CAS lock (single use)
      bootstrap_token_issued_by    INTEGER    FK users.id

   All NULL on existing rows; admin issues the first bootstrap token via
   the new ``POST /api/agents/{id}/issue-bootstrap`` endpoint.

2. ``agent_credentials`` (new) — 1:N owned by ``agents``. Holds the
   long-lived per-agent service token plus a 24h-grace previous-token
   for rotation overlap. ``is_legacy=true`` rows are the backfilled
   fleet-shared CSP_SERVICE_TOKEN, kept around so existing agents work
   uninterrupted until admin cuts each over.

3. ``service_clients`` (new) — Router and future workers (admin tools,
   ingestion-worker) that are not registered as agents. Same crypto +
   lookup-hash pattern. Migration backfills one row,
   ``client_name='router-primary'``, with the same legacy token so the
   Router-primary lookup endpoint keeps working.

4. ``token_usage`` — add 2 NULLable FK columns + partial indexes for
   per-agent / per-service-client attribution (Phase G). Both NULL on
   existing rows; new writes can populate them as the proxy / verify
   paths learn the caller identity.

Backfill strategy
=================

For a non-empty ``CSP_SERVICE_TOKEN`` env value:

  * One ``agent_credentials`` row per ``approval_status='approved'``
    agent, ``is_legacy=TRUE``, ``label='legacy-fleet-shared'``,
    encrypted via ``service_token_envelope.encode_service_token_envelope``.
  * One ``service_clients`` row, ``client_name='router-primary'``,
    same envelope, ``is_legacy=TRUE``.

For an empty / unset ``CSP_SERVICE_TOKEN`` (clean dev install): no
backfill — admin must issue bootstrap tokens / static tokens via the
new endpoints. This keeps the migration idempotent and prevents
encrypting an empty string into a confusing "looks valid but matches
nothing" credential.

Rollback notes
==============

``downgrade()`` drops everything cleanly. Be aware that any agent that
has already cut over to a per-agent token (``is_legacy=FALSE``) will
be unable to authenticate after rollback unless its env var is
restored to the old ``CSP_SERVICE_TOKEN`` value AND that value matches
what was on the agent's container at the time. In practice once
cutover starts, rollback is a one-way door — capture an SQL backup
before downgrading.

Revision ID: 0027
Revises: 0026
Create Date: 2026-04-30
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0027"
down_revision: Union[str, None] = "0026"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# ---------------------------------------------------------------------------
# UPGRADE
# ---------------------------------------------------------------------------


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    _add_bootstrap_columns_to_agents(inspector)
    _create_agent_credentials_table(inspector)
    _create_service_clients_table(inspector)
    _add_caller_columns_to_token_usage(inspector)
    _backfill_legacy_credentials(bind)


def _add_bootstrap_columns_to_agents(inspector: sa.Inspector) -> None:
    existing = {col["name"] for col in inspector.get_columns("agents")}
    with op.batch_alter_table("agents") as batch:
        if "bootstrap_token_hash" not in existing:
            batch.add_column(sa.Column("bootstrap_token_hash", sa.String(64), nullable=True))
        if "bootstrap_token_expires_at" not in existing:
            batch.add_column(sa.Column("bootstrap_token_expires_at", sa.DateTime(), nullable=True))
        if "bootstrap_token_consumed_at" not in existing:
            batch.add_column(sa.Column("bootstrap_token_consumed_at", sa.DateTime(), nullable=True))
        if "bootstrap_token_issued_by" not in existing:
            batch.add_column(
                sa.Column(
                    "bootstrap_token_issued_by",
                    sa.Integer(),
                    sa.ForeignKey("users.id", ondelete="SET NULL"),
                    nullable=True,
                )
            )


def _create_agent_credentials_table(inspector: sa.Inspector) -> None:
    if "agent_credentials" in inspector.get_table_names():
        return

    op.create_table(
        "agent_credentials",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "agent_id",
            sa.Integer(),
            sa.ForeignKey("agents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("label", sa.String(100), nullable=True),
        sa.Column("service_token_envelope", sa.Text(), nullable=False),
        sa.Column("service_token_lookup_hash", sa.String(64), nullable=False),
        sa.Column("service_token_previous_envelope", sa.Text(), nullable=True),
        sa.Column("service_token_previous_lookup_hash", sa.String(64), nullable=True),
        sa.Column("service_token_previous_expires_at", sa.DateTime(), nullable=True),
        sa.Column(
            "service_token_issued_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
        sa.Column("service_token_rotated_at", sa.DateTime(), nullable=True),
        sa.Column("client_cert_fingerprint", sa.String(128), nullable=True),
        sa.Column("is_legacy", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.Column(
            "revoked_by",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
    )

    op.create_index(
        "ix_agent_credentials_agent_id",
        "agent_credentials",
        ["agent_id"],
    )
    # Partial indexes — verify path searches by lookup hash among
    # active rows only. Saves a chunk of index size on a fleet of
    # rotated/revoked credentials.
    op.execute(
        "CREATE INDEX idx_agent_credentials_active_hash "
        "ON agent_credentials (service_token_lookup_hash) "
        "WHERE is_active = TRUE;"
    )
    op.execute(
        "CREATE INDEX idx_agent_credentials_active_prev_hash "
        "ON agent_credentials (service_token_previous_lookup_hash) "
        "WHERE is_active = TRUE AND service_token_previous_lookup_hash IS NOT NULL;"
    )


def _create_service_clients_table(inspector: sa.Inspector) -> None:
    if "service_clients" in inspector.get_table_names():
        return

    op.create_table(
        "service_clients",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("client_name", sa.String(100), nullable=False, unique=True),
        sa.Column("client_type", sa.String(20), nullable=False),
        sa.Column("description", sa.String(500), nullable=True),
        sa.Column("service_token_envelope", sa.Text(), nullable=False),
        sa.Column(
            "service_token_lookup_hash",
            sa.String(64),
            nullable=False,
            unique=True,
        ),
        sa.Column("service_token_previous_envelope", sa.Text(), nullable=True),
        sa.Column("service_token_previous_lookup_hash", sa.String(64), nullable=True),
        sa.Column("service_token_previous_expires_at", sa.DateTime(), nullable=True),
        sa.Column(
            "service_token_issued_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
        sa.Column("service_token_rotated_at", sa.DateTime(), nullable=True),
        sa.Column("client_cert_fingerprint", sa.String(128), nullable=True),
        sa.Column("is_legacy", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.Column(
            "revoked_by",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
    )

    op.create_index(
        "ix_service_clients_client_name",
        "service_clients",
        ["client_name"],
    )
    op.execute(
        "CREATE INDEX idx_service_clients_active_prev_hash "
        "ON service_clients (service_token_previous_lookup_hash) "
        "WHERE is_active = TRUE AND service_token_previous_lookup_hash IS NOT NULL;"
    )


def _add_caller_columns_to_token_usage(inspector: sa.Inspector) -> None:
    existing = {col["name"] for col in inspector.get_columns("token_usage")}
    with op.batch_alter_table("token_usage") as batch:
        if "caller_agent_id" not in existing:
            batch.add_column(
                sa.Column(
                    "caller_agent_id",
                    sa.Integer(),
                    sa.ForeignKey("agents.id", ondelete="SET NULL"),
                    nullable=True,
                )
            )
        if "caller_client_id" not in existing:
            batch.add_column(
                sa.Column(
                    "caller_client_id",
                    sa.Integer(),
                    sa.ForeignKey("service_clients.id", ondelete="SET NULL"),
                    nullable=True,
                )
            )

    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_token_usage_caller_agent "
        "ON token_usage (caller_agent_id) "
        "WHERE caller_agent_id IS NOT NULL;"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_token_usage_caller_client "
        "ON token_usage (caller_client_id) "
        "WHERE caller_client_id IS NOT NULL;"
    )


def _backfill_legacy_credentials(bind) -> None:
    """Encrypt the existing CSP_SERVICE_TOKEN env var into per-row credentials.

    Only runs when ``settings.CSP_SERVICE_TOKEN`` is non-empty. Importing
    the helper modules at function scope (not module scope) keeps the
    migration importable even if the app package fails to load —
    important during fresh-DB ``alembic upgrade head`` runs.
    """
    # Local imports — keep migration importable even if app config is
    # half-broken (typical during first-time bring-up).
    from app.config import settings
    from app.services.service_token_envelope import (
        compute_lookup_hash,
        encode_service_token_envelope,
    )

    legacy_token = (settings.CSP_SERVICE_TOKEN or "").strip()
    if not legacy_token:
        # Clean install or operator chose to start without the legacy
        # shared token. No-op: admins issue per-agent tokens via the
        # new endpoints from day one.
        return

    envelope = encode_service_token_envelope(legacy_token)
    lookup_hash = compute_lookup_hash(legacy_token)
    now = datetime.now(timezone.utc)

    # Backfill one ``service_clients`` row for the Router primary-LLM
    # lookup. Use ON CONFLICT to keep the migration re-runnable.
    bind.execute(
        sa.text(
            """
            INSERT INTO service_clients (
                client_name, client_type, description,
                service_token_envelope, service_token_lookup_hash,
                service_token_issued_at, is_legacy, is_active, created_at
            ) VALUES (
                :name, :type, :desc,
                :env, :hash,
                :now, TRUE, TRUE, :now
            )
            ON CONFLICT (client_name) DO NOTHING
            """
        ),
        {
            "name": "router-primary",
            "type": "router",
            "desc": (
                "anila-core-router default identity. Backfilled from "
                "CSP_SERVICE_TOKEN env var on migration 0027."
            ),
            "env": envelope,
            "hash": lookup_hash,
            "now": now,
        },
    )

    # One ``agent_credentials`` row per approved agent. Skip agents
    # that already have a credential row (idempotent re-run).
    approved_agent_ids = bind.execute(
        sa.text(
            """
            SELECT a.id
              FROM agents a
              LEFT JOIN agent_credentials c
                ON c.agent_id = a.id AND c.is_active = TRUE
             WHERE a.approval_status = 'approved'
               AND c.id IS NULL
            """
        )
    ).fetchall()

    if not approved_agent_ids:
        return

    bind.execute(
        sa.text(
            """
            INSERT INTO agent_credentials (
                agent_id, label,
                service_token_envelope, service_token_lookup_hash,
                service_token_issued_at, is_legacy, is_active, created_at
            ) VALUES (
                :agent_id, :label,
                :env, :hash,
                :now, TRUE, TRUE, :now
            )
            """
        ),
        [
            {
                "agent_id": row[0],
                "label": "legacy-fleet-shared",
                "env": envelope,
                "hash": lookup_hash,
                "now": now,
            }
            for row in approved_agent_ids
        ],
    )


# ---------------------------------------------------------------------------
# DOWNGRADE
# ---------------------------------------------------------------------------


def downgrade() -> None:
    # token_usage: drop FK columns + partial indexes.
    op.execute("DROP INDEX IF EXISTS idx_token_usage_caller_agent;")
    op.execute("DROP INDEX IF EXISTS idx_token_usage_caller_client;")
    with op.batch_alter_table("token_usage") as batch:
        batch.drop_column("caller_client_id")
        batch.drop_column("caller_agent_id")

    # service_clients
    op.execute("DROP INDEX IF EXISTS idx_service_clients_active_prev_hash;")
    op.execute("DROP INDEX IF EXISTS ix_service_clients_client_name;")
    op.drop_table("service_clients")

    # agent_credentials
    op.execute("DROP INDEX IF EXISTS idx_agent_credentials_active_prev_hash;")
    op.execute("DROP INDEX IF EXISTS idx_agent_credentials_active_hash;")
    op.execute("DROP INDEX IF EXISTS ix_agent_credentials_agent_id;")
    op.drop_table("agent_credentials")

    # agents: drop bootstrap columns
    with op.batch_alter_table("agents") as batch:
        batch.drop_column("bootstrap_token_issued_by")
        batch.drop_column("bootstrap_token_consumed_at")
        batch.drop_column("bootstrap_token_expires_at")
        batch.drop_column("bootstrap_token_hash")
