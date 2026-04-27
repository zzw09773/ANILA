"""Service access control: required_roles + service_access_grants + dev_db_credentials.

Backs the multi-service integration plan (docs/multi-service-integration-plan.md
§7.5 + §10.2). Three new pieces of state:

1. ``platform_links.required_roles`` — JSONB array of role names. Empty array
   ([]) means every authenticated user can see the link; ['admin'] means only
   admins. This is the "role gate" — the cheap first filter before the
   per-user/per-department grant check.

2. ``service_access_grants`` — admin-issued opt-in grants. Either user-level
   (user_id IS NOT NULL, department_id IS NULL) OR department-level (user_id
   IS NULL, department_id IS NOT NULL). The XOR check enforces this so admins
   can't accidentally create a grant that targets neither (or both). Partial
   unique indexes only enforce uniqueness over **active** grants
   (revoked_at IS NULL), so a previously revoked grant can be re-issued
   without manual cleanup.

3. ``dev_db_credentials`` — issued pgvector role per (developer, agent) pair,
   30-day TTL by default. ``pg_role_name`` is the actual Postgres role name
   created out-of-band (CSP backend will issue ``CREATE ROLE`` + ``ALTER ROLE
   ... SET anila.agent_id`` so RLS auto-scopes). Reminder system uses
   ``reminder_sent_at`` to ensure the 7-day-before-expiry email fires exactly
   once per credential.

PK type is Integer (not BigInteger) to match every other table in this
schema. We don't expect grant volume > 10K rows.

Revision ID: 0012
Revises: 0011
Create Date: 2026-04-25
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0012"
down_revision: Union[str, None] = "0011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── 1. platform_links.required_roles ────────────────────────────────────
    # Default '[]'::jsonb so existing rows behave as "everyone allowed" — no
    # behavioural change for already-deployed links until an admin opts them
    # into a stricter role gate.
    op.add_column(
        "platform_links",
        sa.Column(
            "required_roles",
            JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )

    # ── 2. service_access_grants ────────────────────────────────────────────
    op.create_table(
        "service_access_grants",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "department_id",
            sa.Integer(),
            sa.ForeignKey("departments.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "platform_link_id",
            sa.Integer(),
            sa.ForeignKey("platform_links.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "granted_by",
            sa.Integer(),
            # SET NULL on grantor delete: keep audit row even if the admin who
            # issued the grant is later removed.
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "granted_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        # Exactly one of user_id / department_id must be set.
        sa.CheckConstraint(
            "(user_id IS NOT NULL) <> (department_id IS NOT NULL)",
            name="ck_service_access_grants_user_xor_department",
        ),
    )
    # Lookup indexes — both directions are queried frequently:
    #   - "what services can THIS user see?"  → idx on (user_id)
    #   - "who has access to THIS service?"   → idx on (platform_link_id)
    op.create_index(
        "ix_service_access_grants_user_id",
        "service_access_grants",
        ["user_id"],
        postgresql_where=sa.text("user_id IS NOT NULL"),
    )
    op.create_index(
        "ix_service_access_grants_department_id",
        "service_access_grants",
        ["department_id"],
        postgresql_where=sa.text("department_id IS NOT NULL"),
    )
    op.create_index(
        "ix_service_access_grants_platform_link_id",
        "service_access_grants",
        ["platform_link_id"],
    )
    # Partial unique: only enforce uniqueness over ACTIVE grants. A revoked
    # grant can be re-issued (creates a new row) without violating uniqueness.
    op.create_index(
        "uq_service_access_grants_user_active",
        "service_access_grants",
        ["user_id", "platform_link_id"],
        unique=True,
        postgresql_where=sa.text("user_id IS NOT NULL AND revoked_at IS NULL"),
    )
    op.create_index(
        "uq_service_access_grants_department_active",
        "service_access_grants",
        ["department_id", "platform_link_id"],
        unique=True,
        postgresql_where=sa.text("department_id IS NOT NULL AND revoked_at IS NULL"),
    )

    # ── 3. dev_db_credentials ───────────────────────────────────────────────
    op.create_table(
        "dev_db_credentials",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "agent_id",
            sa.Integer(),
            sa.ForeignKey("agents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # Actual Postgres role name created out-of-band. Globally unique
        # because Postgres role names live in a flat namespace.
        sa.Column("pg_role_name", sa.String(length=100), nullable=False, unique=True),
        sa.Column(
            "issued_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        # Set the moment the 7-day-before-expiry reminder is dispatched, so a
        # cron rerun doesn't double-send.
        sa.Column("reminder_sent_at", sa.DateTime(), nullable=True),
    )
    op.create_index(
        "ix_dev_db_credentials_user_id",
        "dev_db_credentials",
        ["user_id"],
    )
    op.create_index(
        "ix_dev_db_credentials_agent_id",
        "dev_db_credentials",
        ["agent_id"],
    )
    # One active credential per (developer, agent) pair. Re-issue requires
    # explicit revocation first (so we have a clean audit trail).
    op.create_index(
        "uq_dev_db_credentials_user_agent_active",
        "dev_db_credentials",
        ["user_id", "agent_id"],
        unique=True,
        postgresql_where=sa.text("revoked_at IS NULL"),
    )
    # Cron job will scan WHERE revoked_at IS NULL AND expires_at < now() to
    # auto-revoke expired credentials; index keeps that scan O(active rows).
    op.create_index(
        "ix_dev_db_credentials_expires_at",
        "dev_db_credentials",
        ["expires_at"],
        postgresql_where=sa.text("revoked_at IS NULL"),
    )


def downgrade() -> None:
    # Drop in reverse order. Indexes go with the tables (Postgres semantics)
    # so we only need to drop named indexes that aren't auto-attached.
    op.drop_index("ix_dev_db_credentials_expires_at", table_name="dev_db_credentials")
    op.drop_index(
        "uq_dev_db_credentials_user_agent_active",
        table_name="dev_db_credentials",
    )
    op.drop_index("ix_dev_db_credentials_agent_id", table_name="dev_db_credentials")
    op.drop_index("ix_dev_db_credentials_user_id", table_name="dev_db_credentials")
    op.drop_table("dev_db_credentials")

    op.drop_index(
        "uq_service_access_grants_department_active",
        table_name="service_access_grants",
    )
    op.drop_index(
        "uq_service_access_grants_user_active",
        table_name="service_access_grants",
    )
    op.drop_index(
        "ix_service_access_grants_platform_link_id",
        table_name="service_access_grants",
    )
    op.drop_index(
        "ix_service_access_grants_department_id",
        table_name="service_access_grants",
    )
    op.drop_index(
        "ix_service_access_grants_user_id",
        table_name="service_access_grants",
    )
    op.drop_table("service_access_grants")

    op.drop_column("platform_links", "required_roles")
