"""Create the exactly-once Gate 5 model-governance receipt ledger.

Revision ID: r1_0027
Revises: r1_0026
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "r1_0027"
down_revision: Union[str, None] = "r1_0026"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "model_governance_receipts",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("invocation_id", sa.String(length=255), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("model_id", sa.Integer(), nullable=True),
        sa.Column("callsite_id", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="pre"),
        sa.Column("usage_record_id", sa.Integer(), nullable=True),
        sa.Column("pre_audit_id", sa.Integer(), nullable=True),
        sa.Column("post_audit_id", sa.Integer(), nullable=True),
        sa.Column("metadata_json", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(
            ["model_id"], ["model_registry.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["usage_record_id"], ["token_usage.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["pre_audit_id"], ["audit_logs.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["post_audit_id"], ["audit_logs.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "invocation_id", name="uq_model_governance_receipts_invocation"
        ),
        sa.CheckConstraint(
            "status IN ('pre', 'authorized', 'completed', 'failed')",
            name="ck_model_governance_receipts_status",
        ),
    )
    op.create_index(
        "ix_model_governance_receipts_user_created",
        "model_governance_receipts",
        ["user_id", "created_at"],
    )
    op.create_index(
        "ix_model_governance_receipts_callsite_created",
        "model_governance_receipts",
        ["callsite_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_model_governance_receipts_callsite_created",
        table_name="model_governance_receipts",
    )
    op.drop_index(
        "ix_model_governance_receipts_user_created",
        table_name="model_governance_receipts",
    )
    op.drop_table("model_governance_receipts")
