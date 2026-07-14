"""Add durable auth sessions, refresh families, and scoped revocations.

Revision ID: r1_0014
Revises: r1_0013
Create Date: 2026-07-12
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "r1_0014"
down_revision: Union[str, None] = "r1_0013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "auth_sessions",
        sa.Column("sid", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("refresh_family_id", sa.String(length=64), nullable=False),
        sa.Column("amr_json", sa.Text(), nullable=False),
        sa.Column("acr", sa.String(length=128), nullable=False),
        sa.Column("auth_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "break_glass",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoke_reason", sa.String(length=128), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.CheckConstraint(
            "length(sid) >= 32", name="ck_auth_sessions_sid_entropy"
        ),
        sa.CheckConstraint(
            "length(refresh_family_id) >= 32",
            name="ck_auth_sessions_family_entropy",
        ),
        sa.CheckConstraint(
            "length(acr) > 0", name="ck_auth_sessions_acr_nonempty"
        ),
        sa.PrimaryKeyConstraint("sid"),
        sa.UniqueConstraint("refresh_family_id"),
    )
    op.create_index("ix_auth_sessions_user_id", "auth_sessions", ["user_id"])
    op.create_index("ix_auth_sessions_revoked_at", "auth_sessions", ["revoked_at"])
    op.create_index(
        "ix_auth_sessions_user_active",
        "auth_sessions",
        ["user_id", "revoked_at"],
    )

    op.create_table(
        "auth_refresh_tokens",
        sa.Column("jti_hash", sa.String(length=64), nullable=False),
        sa.Column("sid", sa.String(length=64), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("parent_jti_hash", sa.String(length=64), nullable=True),
        sa.Column(
            "issued_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["sid"], ["auth_sessions.sid"], ondelete="CASCADE"),
        sa.CheckConstraint(
            "length(jti_hash) = 64", name="ck_auth_refresh_jti_hash_length"
        ),
        sa.CheckConstraint(
            "parent_jti_hash IS NULL OR length(parent_jti_hash) = 64",
            name="ck_auth_refresh_parent_hash_length",
        ),
        sa.CheckConstraint(
            "generation >= 0", name="ck_auth_refresh_generation_nonnegative"
        ),
        sa.CheckConstraint(
            "expires_at > issued_at", name="ck_auth_refresh_expiry_order"
        ),
        sa.PrimaryKeyConstraint("jti_hash"),
        sa.UniqueConstraint("sid", "generation", name="uq_auth_refresh_sid_generation"),
    )
    op.create_index("ix_auth_refresh_tokens_sid", "auth_refresh_tokens", ["sid"])
    op.create_index(
        "ix_auth_refresh_tokens_expires_at",
        "auth_refresh_tokens",
        ["expires_at"],
    )

    op.add_column(
        "token_revocations",
        sa.Column(
            "scope",
            sa.String(length=16),
            nullable=False,
            server_default="user_version",
        ),
    )
    op.add_column(
        "token_revocations",
        sa.Column("token_jti_hash", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "token_revocations",
        sa.Column("session_id_hash", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "token_revocations",
        sa.Column("token_type", sa.String(length=16), nullable=True),
    )
    op.add_column(
        "token_revocations",
        sa.Column("reason", sa.String(length=128), nullable=True),
    )
    op.create_check_constraint(
        "ck_token_revocations_scope",
        "token_revocations",
        "scope IN ('user_version', 'jti', 'sid')",
    )
    op.create_check_constraint(
        "ck_token_revocations_scope_shape",
        "token_revocations",
        "(scope = 'user_version' AND token_jti_hash IS NULL "
        "AND session_id_hash IS NULL AND token_type IS NULL) OR "
        "(scope = 'jti' AND token_jti_hash IS NOT NULL "
        "AND session_id_hash IS NULL "
        "AND token_type IN ('access', 'refresh')) OR "
        "(scope = 'sid' AND session_id_hash IS NOT NULL "
        "AND token_jti_hash IS NULL AND token_type IS NULL)",
    )
    op.create_check_constraint(
        "ck_token_revocations_jti_hash_length",
        "token_revocations",
        "token_jti_hash IS NULL OR length(token_jti_hash) = 64",
    )
    op.create_check_constraint(
        "ck_token_revocations_sid_hash_length",
        "token_revocations",
        "session_id_hash IS NULL OR length(session_id_hash) = 64",
    )
    op.create_index("ix_token_revocations_scope", "token_revocations", ["scope"])
    op.create_index(
        "ix_token_revocations_token_jti_hash",
        "token_revocations",
        ["token_jti_hash"],
    )
    op.create_index(
        "ix_token_revocations_session_id_hash",
        "token_revocations",
        ["session_id_hash"],
    )
    op.create_index(
        "ix_token_revocations_jti_scope",
        "token_revocations",
        ["scope", "token_jti_hash"],
    )
    op.create_index(
        "ix_token_revocations_sid_scope",
        "token_revocations",
        ["scope", "session_id_hash"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_token_revocations_sid_hash_length",
        "token_revocations",
        type_="check",
    )
    op.drop_constraint(
        "ck_token_revocations_jti_hash_length",
        "token_revocations",
        type_="check",
    )
    op.drop_constraint(
        "ck_token_revocations_scope_shape",
        "token_revocations",
        type_="check",
    )
    op.drop_constraint(
        "ck_token_revocations_scope",
        "token_revocations",
        type_="check",
    )
    op.drop_index("ix_token_revocations_sid_scope", table_name="token_revocations")
    op.drop_index("ix_token_revocations_jti_scope", table_name="token_revocations")
    op.drop_index(
        "ix_token_revocations_session_id_hash", table_name="token_revocations"
    )
    op.drop_index(
        "ix_token_revocations_token_jti_hash", table_name="token_revocations"
    )
    op.drop_index("ix_token_revocations_scope", table_name="token_revocations")
    op.drop_column("token_revocations", "reason")
    op.drop_column("token_revocations", "token_type")
    op.drop_column("token_revocations", "session_id_hash")
    op.drop_column("token_revocations", "token_jti_hash")
    op.drop_column("token_revocations", "scope")

    op.drop_index("ix_auth_refresh_tokens_expires_at", table_name="auth_refresh_tokens")
    op.drop_index("ix_auth_refresh_tokens_sid", table_name="auth_refresh_tokens")
    op.drop_table("auth_refresh_tokens")
    op.drop_index("ix_auth_sessions_user_active", table_name="auth_sessions")
    op.drop_index("ix_auth_sessions_revoked_at", table_name="auth_sessions")
    op.drop_index("ix_auth_sessions_user_id", table_name="auth_sessions")
    op.drop_table("auth_sessions")
