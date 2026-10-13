# -*- coding: utf-8 -*-
"""差評結案：狀態、結案方式、結案時間。

結案通知走既有的 notifications 表（一筆對一個使用者），
不另開廣播表。回報者是 messages.feedback_submitter_id。

Revision ID: r1_0073
Revises: r1_0072
Create Date: 2026-10-12
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "r1_0073"
down_revision: Union[str, None] = "r1_0072"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_CHECKS = (
    (
        "ck_messages_feedback_status",
        "feedback_status IS NULL OR feedback_status IN ('open', 'closed')",
    ),
    (
        "ck_messages_feedback_resolution",
        "feedback_resolution IS NULL OR feedback_resolution IN ('fixed', 'wont_fix', 'duplicate')",
    ),
    (
        "ck_messages_feedback_closed_has_resolution",
        "feedback_status IS NULL OR feedback_status <> 'closed' OR feedback_resolution IS NOT NULL",
    ),
    (
        "ck_messages_feedback_wont_fix_has_note",
        "("
        "feedback_resolution IS NULL OR feedback_resolution <> 'wont_fix' OR ("
        "feedback_resolution_note IS NOT NULL AND length(trim(feedback_resolution_note)) > 0"
        ")"
        ")",
    ),
)


def upgrade() -> None:
    op.add_column("messages", sa.Column("feedback_status", sa.String(length=16), nullable=True))
    op.add_column("messages", sa.Column("feedback_resolution", sa.String(length=16), nullable=True))
    op.add_column("messages", sa.Column("feedback_resolution_note", sa.Text(), nullable=True))
    op.add_column(
        "messages",
        sa.Column("feedback_resolved_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "messages",
        sa.Column("feedback_submitter_id", sa.Integer(), nullable=True),
    )
    op.create_index("ix_messages_feedback_status", "messages", ["feedback_status"])
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        # SQLite 不能 ALTER 加外鍵，批次重建才加得上。正式庫走下面的 Postgres。
        with op.batch_alter_table("messages") as batch:
            batch.create_foreign_key(
                "fk_messages_feedback_submitter_id",
                "users",
                ["feedback_submitter_id"],
                ["id"],
                ondelete="SET NULL",
            )
        return
    op.create_foreign_key(
        "fk_messages_feedback_submitter_id",
        "messages",
        "users",
        ["feedback_submitter_id"],
        ["id"],
        ondelete="SET NULL",
    )
    for name, expr in _CHECKS:
        op.create_check_constraint(name, "messages", expr)


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("messages") as batch:
            batch.drop_constraint(
                "fk_messages_feedback_submitter_id", type_="foreignkey"
            )
            batch.drop_index("ix_messages_feedback_status")
            batch.drop_column("feedback_submitter_id")
            batch.drop_column("feedback_resolved_at")
            batch.drop_column("feedback_resolution_note")
            batch.drop_column("feedback_resolution")
            batch.drop_column("feedback_status")
        return
    for name, _expr in reversed(_CHECKS):
        op.drop_constraint(name, "messages", type_="check")
    op.drop_constraint(
        "fk_messages_feedback_submitter_id", "messages", type_="foreignkey"
    )
    op.drop_index("ix_messages_feedback_status", table_name="messages")
    op.drop_column("messages", "feedback_submitter_id")
    op.drop_column("messages", "feedback_resolved_at")
    op.drop_column("messages", "feedback_resolution_note")
    op.drop_column("messages", "feedback_resolution")
    op.drop_column("messages", "feedback_status")
