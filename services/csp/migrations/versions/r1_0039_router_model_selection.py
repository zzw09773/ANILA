# -*- coding: utf-8 -*-
"""Router model selection: grants, groups, conversation choice, usage kinds.

Revision ID: r1_0039
Revises: r1_0038
Create Date: 2026-09-11
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "r1_0039"
down_revision: Union[str, None] = "r1_0038"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "model_registry",
        sa.Column(
            "router_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )

    op.create_table(
        "model_access_groups",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("name", name="uq_model_access_groups_name"),
    )
    op.create_table(
        "model_access_group_members",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "group_id",
            sa.Integer(),
            sa.ForeignKey("model_access_groups.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("group_id", "user_id", name="uq_model_access_group_members_group_user"),
    )
    op.create_index(
        "ix_model_access_group_members_user_id",
        "model_access_group_members",
        ["user_id"],
    )

    op.create_table(
        "router_model_grants",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "model_id",
            sa.Integer(),
            sa.ForeignKey("model_registry.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("scope_type", sa.String(length=20), nullable=False),
        sa.Column(
            "department_id",
            sa.Integer(),
            sa.ForeignKey("departments.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "group_id",
            sa.Integer(),
            sa.ForeignKey("model_access_groups.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "include_descendants",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_by",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "("
            "(scope_type = 'all' AND department_id IS NULL AND group_id IS NULL AND user_id IS NULL) OR "
            "(scope_type = 'department' AND department_id IS NOT NULL AND group_id IS NULL AND user_id IS NULL) OR "
            "(scope_type = 'group' AND group_id IS NOT NULL AND department_id IS NULL AND user_id IS NULL) OR "
            "(scope_type = 'user' AND user_id IS NOT NULL AND department_id IS NULL AND group_id IS NULL)"
            ")",
            name="ck_router_model_grants_scope_fk",
        ),
    )
    op.create_index("ix_router_model_grants_model_id", "router_model_grants", ["model_id"])
    op.create_index(
        "uq_router_model_grants_all",
        "router_model_grants",
        ["model_id"],
        unique=True,
        postgresql_where=sa.text("scope_type = 'all'"),
        sqlite_where=sa.text("scope_type = 'all'"),
    )
    op.create_index(
        "uq_router_model_grants_department",
        "router_model_grants",
        ["model_id", "department_id", "include_descendants"],
        unique=True,
        postgresql_where=sa.text("scope_type = 'department'"),
        sqlite_where=sa.text("scope_type = 'department'"),
    )
    op.create_index(
        "uq_router_model_grants_group",
        "router_model_grants",
        ["model_id", "group_id"],
        unique=True,
        postgresql_where=sa.text("scope_type = 'group'"),
        sqlite_where=sa.text("scope_type = 'group'"),
    )
    op.create_index(
        "uq_router_model_grants_user",
        "router_model_grants",
        ["model_id", "user_id"],
        unique=True,
        postgresql_where=sa.text("scope_type = 'user'"),
        sqlite_where=sa.text("scope_type = 'user'"),
    )

    op.add_column(
        "conversations",
        sa.Column(
            "router_model_id",
            sa.Integer(),
            sa.ForeignKey("model_registry.id", ondelete="RESTRICT"),
            nullable=True,
        ),
    )
    op.add_column(
        "conversations",
        sa.Column(
            "router_selection_version",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )

    op.add_column("token_usage", sa.Column("invocation_id", sa.String(length=64), nullable=True))
    op.add_column(
        "token_usage",
        sa.Column("usage_kind", sa.String(length=32), nullable=False, server_default="inference"),
    )
    op.add_column(
        "token_usage",
        sa.Column("token_source", sa.String(length=20), nullable=False, server_default="unknown"),
    )
    op.add_column(
        "token_usage",
        sa.Column("outcome", sa.String(length=20), nullable=False, server_default="success"),
    )
    op.add_column("token_usage", sa.Column("model_name_snapshot", sa.String(length=200), nullable=True))
    op.create_index(
        "uq_token_usage_invocation_id",
        "token_usage",
        ["invocation_id"],
        unique=True,
        postgresql_where=sa.text("invocation_id IS NOT NULL"),
        sqlite_where=sa.text("invocation_id IS NOT NULL"),
    )

    # Existing campus primary LLM becomes router-enabled + campus grant.
    # Other models stay closed. anila-router itself is never a base model.
    op.execute(
        """
        UPDATE model_registry
        SET router_enabled = true
        WHERE is_router_primary = true
          AND is_active = true
          AND model_type = 'llm'
          AND name <> 'anila-router'
        """
    )
    op.execute(
        """
        INSERT INTO router_model_grants (model_id, scope_type, include_descendants, created_at)
        SELECT id, 'all', false, CURRENT_TIMESTAMP
        FROM model_registry
        WHERE router_enabled = true
          AND is_router_primary = true
          AND name <> 'anila-router'
        """
    )


def downgrade() -> None:
    op.drop_index("uq_token_usage_invocation_id", table_name="token_usage")
    op.drop_column("token_usage", "model_name_snapshot")
    op.drop_column("token_usage", "outcome")
    op.drop_column("token_usage", "token_source")
    op.drop_column("token_usage", "usage_kind")
    op.drop_column("token_usage", "invocation_id")
    op.drop_column("conversations", "router_selection_version")
    op.drop_column("conversations", "router_model_id")
    op.drop_index("uq_router_model_grants_user", table_name="router_model_grants")
    op.drop_index("uq_router_model_grants_group", table_name="router_model_grants")
    op.drop_index("uq_router_model_grants_department", table_name="router_model_grants")
    op.drop_index("uq_router_model_grants_all", table_name="router_model_grants")
    op.drop_index("ix_router_model_grants_model_id", table_name="router_model_grants")
    op.drop_table("router_model_grants")
    op.drop_index("ix_model_access_group_members_user_id", table_name="model_access_group_members")
    op.drop_table("model_access_group_members")
    op.drop_table("model_access_groups")
    op.drop_column("model_registry", "router_enabled")
