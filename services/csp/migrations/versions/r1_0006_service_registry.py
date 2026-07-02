# -*- coding: utf-8 -*-
"""Slice 7a — Service Registry (doc 07 §3/§5/§10/§13/§14).

Additive upgrade of ``platform_links`` into the full ``registered_services``
Registry, plus the launch-gateway audit trail tables:

- ``registered_services``      : 33 doc §3 fields + carried-over ``sort_order``.
- ``service_launches``         : one row per minted launch token (TTL window).
- ``service_audit_callbacks``  : append-only service→CSP audit events.
- ``service_project_bindings`` : service ↔ project entry bindings (doc §13).

Existing ``platform_links`` rows are data-migrated into ``registered_services``
PRESERVING their integer id (grant mapping) and create timestamp, with
``config_source`` detected from ``AUTO_REGISTER_LINKS`` (env_seeded vs db). See
``app.services.registry_backfill``.

``platform_links`` is KEPT intact (downgrade safety, marked deprecated in the
model). ``service_access_grants`` gains a ``service_id`` FK with
``ON DELETE SET NULL`` (preserve-history blocker, doc §14/§15.1) and its legacy
``platform_link_id`` is relaxed to nullable; both are backfilled.

Revision ID: r1_0006
Revises: r1_0005
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.services.registry_backfill import backfill_registered_services

revision: str = "r1_0006"
down_revision: Union[str, None] = "r1_0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Postgres → JSONB, everything else → JSON (SQLite offline runs).
_JSON = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
_EMPTY_LIST = sa.text("'[]'")


def upgrade() -> None:
    # ── 0. 冪等補齊 platform_links 缺漏欄(修 Alembic 鏈漂移) ────────────────────
    # icon / sort_order / required_roles 由 ORM(models/platform_link.py)宣告,卻
    # 從無 migration 建立(0001 只建 id/name/url/description/is_active/created_at,
    # 0013 補 is_public)。走過 startup create_all fallback 的既有 DB 有這些欄,
    # 乾淨 Alembic-only DB 沒有 → 下方 backfill_registered_services 讀 link.icon
    # 等會 UndefinedColumn。inspector 冪等補建:既有 DB 跳過、乾淨 DB 建欄。
    # (與 r1_0005 補 model_registry.health_* 同源;doc 10 §17.3 預警的漂移。)
    _insp = sa.inspect(op.get_bind())
    _pl_cols = {c["name"] for c in _insp.get_columns("platform_links")}
    if "icon" not in _pl_cols:
        op.add_column(
            "platform_links", sa.Column("icon", sa.String(length=50), nullable=True)
        )
    if "sort_order" not in _pl_cols:
        op.add_column(
            "platform_links",
            sa.Column("sort_order", sa.Integer(), nullable=True, server_default="0"),
        )
    if "required_roles" not in _pl_cols:
        op.add_column(
            "platform_links",
            sa.Column(
                "required_roles",
                sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
                nullable=False,
                server_default="[]",
            ),
        )

    # ── registered_services (33 doc fields + sort_order) ─────────────────────
    op.create_table(
        "registered_services",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("slug", sa.String(length=120), nullable=False),
        sa.Column("description", sa.String(length=255), nullable=True),
        sa.Column("icon", sa.String(length=50), nullable=True),
        sa.Column(
            "owner_department_id",
            sa.Integer(),
            sa.ForeignKey("departments.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "owner_admin_user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "service_admin_user_ids", _JSON, nullable=False, server_default=_EMPTY_LIST
        ),
        sa.Column(
            "service_type",
            sa.String(length=30),
            nullable=False,
            server_default="project_portal",
        ),
        sa.Column(
            "project_entry", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("project_id", sa.String(length=100), nullable=True),
        sa.Column("entry_url", sa.String(length=500), nullable=False),
        sa.Column(
            "allowed_origins", _JSON, nullable=False, server_default=_EMPTY_LIST
        ),
        sa.Column(
            "launch_mode",
            sa.String(length=20),
            nullable=False,
            server_default="new_tab",
        ),
        sa.Column(
            "iframe_allowed", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column(
            "sso_mode", sa.String(length=20), nullable=False, server_default="card_sso"
        ),
        sa.Column(
            "supports_launch_token",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column(
            "data_ownership",
            sa.String(length=20),
            nullable=False,
            server_default="self_managed",
        ),
        sa.Column("data_ingress", _JSON, nullable=False, server_default=_EMPTY_LIST),
        sa.Column("data_egress", _JSON, nullable=False, server_default=_EMPTY_LIST),
        sa.Column("healthcheck_url", sa.String(length=500), nullable=True),
        sa.Column("audit_callback_url", sa.String(length=500), nullable=True),
        sa.Column("trace_callback_url", sa.String(length=500), nullable=True),
        sa.Column("classification_ceiling", sa.String(length=20), nullable=True),
        sa.Column("required_roles", _JSON, nullable=False, server_default=_EMPTY_LIST),
        sa.Column(
            "is_public", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column(
            "is_active", sa.Boolean(), nullable=False, server_default=sa.true()
        ),
        sa.Column(
            "config_source", sa.String(length=20), nullable=False, server_default="db"
        ),
        sa.Column("env_seed_key", sa.String(length=150), nullable=True),
        sa.Column(
            "db_editable_fields", _JSON, nullable=False, server_default=_EMPTY_LIST
        ),
        sa.Column("last_seeded_at", sa.DateTime(), nullable=True),
        sa.Column(
            "sort_order", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index(
        "ix_registered_services_slug", "registered_services", ["slug"], unique=True
    )

    # ── service_launches ─────────────────────────────────────────────────────
    op.create_table(
        "service_launches",
        sa.Column("launch_id", sa.String(length=64), primary_key=True),
        sa.Column(
            "service_id",
            sa.Integer(),
            sa.ForeignKey("registered_services.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "task_id",
            sa.Integer(),
            sa.ForeignKey("tasks.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("trace_id", sa.String(length=64), nullable=True),
        sa.Column("source_snapshot_id", sa.Integer(), nullable=True),
        sa.Column(
            "classification_level",
            sa.String(length=20),
            nullable=False,
            server_default="無機密",
        ),
        sa.Column("mode", sa.String(length=20), nullable=False),
        sa.Column(
            "status", sa.String(length=20), nullable=False, server_default="issued"
        ),
        sa.Column("issued_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("consumed_at", sa.DateTime(), nullable=True),
    )
    op.create_index(
        "ix_service_launches_service_id", "service_launches", ["service_id"]
    )
    op.create_index("ix_service_launches_trace_id", "service_launches", ["trace_id"])

    # ── service_audit_callbacks (append-only) ────────────────────────────────
    op.create_table(
        "service_audit_callbacks",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "service_id",
            sa.Integer(),
            sa.ForeignKey("registered_services.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "launch_id",
            sa.String(length=64),
            sa.ForeignKey("service_launches.launch_id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("event_type", sa.String(length=100), nullable=False),
        sa.Column("payload", _JSON, nullable=True),
        sa.Column("classification_level", sa.String(length=20), nullable=True),
        sa.Column(
            "integration_key_id",
            sa.Integer(),
            sa.ForeignKey("service_clients.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("received_at", sa.DateTime(), nullable=False),
    )
    op.create_index(
        "ix_service_audit_callbacks_service_id",
        "service_audit_callbacks",
        ["service_id"],
    )
    op.create_index(
        "ix_service_audit_callbacks_launch_id",
        "service_audit_callbacks",
        ["launch_id"],
    )

    # ── service_project_bindings ─────────────────────────────────────────────
    op.create_table(
        "service_project_bindings",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "service_id",
            sa.Integer(),
            sa.ForeignKey("registered_services.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("project_id", sa.String(length=100), nullable=False),
        sa.Column(
            "is_primary_entry",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column(
            "created_by",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint(
            "service_id", "project_id", name="uq_service_project_binding"
        ),
    )
    op.create_index(
        "ix_service_project_bindings_service_id",
        "service_project_bindings",
        ["service_id"],
    )

    # ── service_access_grants: preserve-history service_id FK ────────────────
    op.add_column(
        "service_access_grants",
        sa.Column("service_id", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "fk_service_access_grants_service_id",
        "service_access_grants",
        "registered_services",
        ["service_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_service_access_grants_service_id",
        "service_access_grants",
        ["service_id"],
    )
    # New registry services have no platform_links row, so the legacy FK column
    # must be nullable.
    op.alter_column(
        "service_access_grants",
        "platform_link_id",
        existing_type=sa.Integer(),
        nullable=True,
    )

    # ── data migration + Postgres sequence reset ─────────────────────────────
    bind = op.get_bind()
    backfill_registered_services(bind)
    if bind.dialect.name == "postgresql":
        op.execute(
            "SELECT setval("
            "pg_get_serial_sequence('registered_services', 'id'), "
            "GREATEST((SELECT COALESCE(MAX(id), 1) FROM registered_services), 1)"
            ")"
        )


def downgrade() -> None:
    op.drop_index(
        "ix_service_access_grants_service_id", table_name="service_access_grants"
    )
    op.drop_constraint(
        "fk_service_access_grants_service_id",
        "service_access_grants",
        type_="foreignkey",
    )
    op.drop_column("service_access_grants", "service_id")
    # platform_link_id is left nullable on downgrade: re-imposing NOT NULL could
    # fail for grants issued against db-only services (no platform_links row).

    op.drop_index(
        "ix_service_project_bindings_service_id",
        table_name="service_project_bindings",
    )
    op.drop_table("service_project_bindings")
    op.drop_index(
        "ix_service_audit_callbacks_launch_id", table_name="service_audit_callbacks"
    )
    op.drop_index(
        "ix_service_audit_callbacks_service_id", table_name="service_audit_callbacks"
    )
    op.drop_table("service_audit_callbacks")
    op.drop_index("ix_service_launches_trace_id", table_name="service_launches")
    op.drop_index("ix_service_launches_service_id", table_name="service_launches")
    op.drop_table("service_launches")
    op.drop_index("ix_registered_services_slug", table_name="registered_services")
    op.drop_table("registered_services")
