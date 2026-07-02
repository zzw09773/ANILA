"""Add agents, user_agent_permissions, api_key_agent_permissions tables.

Revision ID: 0002
Revises: 0001
Create Date: 2026-04-20
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── agents ────────────────────────────────────────────────────────────────
    op.create_table(
        "agents",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("owner_user_id", sa.Integer(), nullable=False),
        sa.Column("base_model_id", sa.Integer(), nullable=True),
        sa.Column("endpoint_url", sa.String(500), nullable=False),
        sa.Column("api_version", sa.String(20), nullable=False, server_default="v1"),
        sa.Column("description_for_router", sa.Text(), nullable=False,
                  server_default=""),
        sa.Column("input_schema", postgresql.JSONB(), nullable=True),
        sa.Column("capabilities", postgresql.JSONB(), nullable=True),
        sa.Column("health_status", sa.String(20), nullable=False,
                  server_default="unknown"),
        sa.Column("approval_status", sa.String(20), nullable=False,
                  server_default="pending"),
        sa.Column("approved_by", sa.Integer(), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["approved_by"], ["users.id"],
                                ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["base_model_id"], ["model_registry.id"],
                                ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )
    op.create_index("ix_agents_name", "agents", ["name"])
    op.create_index("ix_agents_approval_status", "agents", ["approval_status"])

    # ── user_agent_permissions ────────────────────────────────────────────────
    op.create_table(
        "user_agent_permissions",
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("agent_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id", "agent_id"),
    )

    # ── api_key_agent_permissions ─────────────────────────────────────────────
    op.create_table(
        "api_key_agent_permissions",
        sa.Column("api_key_id", sa.Integer(), nullable=False),
        sa.Column("agent_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["api_key_id"], ["api_keys.id"],
                                ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("api_key_id", "agent_id"),
    )


def downgrade() -> None:
    op.drop_table("api_key_agent_permissions")
    op.drop_table("user_agent_permissions")
    op.drop_table("agents")
