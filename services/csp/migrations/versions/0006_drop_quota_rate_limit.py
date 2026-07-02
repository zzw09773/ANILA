"""Drop quota_policies table and quota_policy_id FKs (on-prem local model → no quota).

Rationale:
    ANILA targets on-prem local model deployments, which do not require
    token/request quota enforcement. Reverses 0003 entirely so the schema
    stops carrying unused machinery. nginx IP-level rate limiting is
    unrelated (DDoS guard) and remains in place.

Revision ID: 0006
Revises: 0005
Create Date: 2026-04-21
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    api_key_indexes = {ix["name"] for ix in inspector.get_indexes("api_keys")}
    if "ix_api_keys_quota_policy_id" in api_key_indexes:
        op.drop_index("ix_api_keys_quota_policy_id", table_name="api_keys")
    api_key_cols = {c["name"] for c in inspector.get_columns("api_keys")}
    if "quota_policy_id" in api_key_cols:
        with op.batch_alter_table("api_keys") as batch_op:
            batch_op.drop_column("quota_policy_id")

    user_indexes = {ix["name"] for ix in inspector.get_indexes("users")}
    if "ix_users_quota_policy_id" in user_indexes:
        op.drop_index("ix_users_quota_policy_id", table_name="users")
    user_cols = {c["name"] for c in inspector.get_columns("users")}
    if "quota_policy_id" in user_cols:
        with op.batch_alter_table("users") as batch_op:
            batch_op.drop_column("quota_policy_id")

    if inspector.has_table("quota_policies"):
        quota_indexes = {ix["name"] for ix in inspector.get_indexes("quota_policies")}
        if "ix_quota_policies_name" in quota_indexes:
            op.drop_index("ix_quota_policies_name", table_name="quota_policies")
        op.drop_table("quota_policies")


def downgrade() -> None:
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
