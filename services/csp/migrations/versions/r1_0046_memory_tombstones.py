# -*- coding: utf-8 -*-
"""記憶刪除墓碑、使用者手改標記，以及閒置整理的認領租約。

分類對話不再進入記憶。使用者刪掉的摘要與事實要留下涵蓋範圍，
手改過的事實不可被下一輪整理蓋掉，多個 CSP worker 也不能同時整理
同一段對話。

Revision ID: r1_0046
Revises: r1_0045
Create Date: 2026-09-25
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "r1_0046"
down_revision: Union[str, None] = "r1_0045"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _count(bind, sql: str) -> int:
    try:
        counted = bind.execute(sa.text(sql))
        return int(counted.scalar() or 0)
    except Exception:
        return 0


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    if "user_facts" in tables:
        columns = {column["name"] for column in inspector.get_columns("user_facts")}
        if "user_edited" not in columns:
            op.add_column(
                "user_facts",
                sa.Column(
                    "user_edited",
                    sa.Boolean(),
                    nullable=False,
                    server_default=sa.text("false"),
                ),
            )
    if "memory_tombstones" not in tables:
        op.create_table(
            "memory_tombstones",
            sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
            sa.Column(
                "user_id",
                sa.Integer(),
                sa.ForeignKey("users.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "conversation_id",
                sa.Integer(),
                sa.ForeignKey("conversations.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("kind", sa.String(length=20), nullable=False),
            sa.Column("fact_key", sa.String(length=120), nullable=True),
            sa.Column("covered_message_id", sa.Integer(), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("CURRENT_TIMESTAMP"),
            ),
        )
        op.create_index(
            "ix_memory_tombstones_user_id",
            "memory_tombstones",
            ["user_id"],
        )
    if "memory_refresh_leases" not in tables:
        op.create_table(
            "memory_refresh_leases",
            sa.Column(
                "conversation_id",
                sa.Integer(),
                sa.ForeignKey("conversations.id", ondelete="CASCADE"),
                primary_key=True,
            ),
            sa.Column("claim_token", sa.String(length=64), nullable=False),
            sa.Column("claimed_until", sa.Integer(), nullable=False),
        )


def downgrade() -> None:
    bind = op.get_bind()
    if _count(bind, "SELECT COUNT(*) FROM memory_tombstones") > 0:
        raise RuntimeError("memory_tombstones 仍有資料，拒絕降版")
    op.execute("DROP TABLE IF EXISTS memory_refresh_leases")
    op.execute("DROP INDEX IF EXISTS ix_memory_tombstones_user_id")
    op.execute("DROP TABLE IF EXISTS memory_tombstones")
    op.execute("ALTER TABLE user_facts DROP COLUMN IF EXISTS user_edited")
