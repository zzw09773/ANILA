"""Add quota_policies table and quota_policy_id FK on users/api_keys.

Revision ID: 0003
Revises: 0002
Create Date: 2026-04-20
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── quota_policies ────────────────────────────────────────────────────────
    op.create_table(
        "quota_policies",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("description", sa.String(500), nullable=True),
        sa.Column("token_limit_per_day", sa.Integer(), nullable=True),
        sa.Column("token_limit_per_month", sa.Integer(), nullable=True),
        sa.Column("request_limit_per_minute", sa.Integer(), nullable=True),
        sa.Column("request_limit_per_hour", sa.Integer(), nullable=True),
        sa.Column(
            "is_default",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )
    op.create_index("ix_quota_policies_name", "quota_policies", ["name"])

    # ── users.quota_policy_id ─────────────────────────────────────────────────
    op.add_column(
        "users",
        sa.Column(
            "quota_policy_id",
            sa.Integer(),
            sa.ForeignKey("quota_policies.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index("ix_users_quota_policy_id", "users", ["quota_policy_id"])

    # ── api_keys.quota_policy_id ──────────────────────────────────────────────
    op.add_column(
        "api_keys",
        sa.Column(
            "quota_policy_id",
            sa.Integer(),
            sa.ForeignKey("quota_policies.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index("ix_api_keys_quota_policy_id", "api_keys", ["quota_policy_id"])


def downgrade() -> None:
    op.drop_index("ix_api_keys_quota_policy_id", table_name="api_keys")
    op.drop_column("api_keys", "quota_policy_id")

    op.drop_index("ix_users_quota_policy_id", table_name="users")
    op.drop_column("users", "quota_policy_id")

    op.drop_index("ix_quota_policies_name", table_name="quota_policies")
    op.drop_table("quota_policies")
