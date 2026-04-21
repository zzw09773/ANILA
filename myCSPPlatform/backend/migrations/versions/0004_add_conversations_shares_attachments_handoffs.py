"""Add conversations, messages, conversation_shares, attachments, handoffs, notifications;
   add conversation_id + trace_id to token_usage.

Revision ID: 0004
Revises: 0003
Create Date: 2026-04-20
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── conversations ─────────────────────────────────────────────────────────
    op.create_table(
        "conversations",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("agent_id", sa.Integer(), nullable=True),
        sa.Column("title", sa.String(255), nullable=False, server_default="新對話"),
        sa.Column("classified", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("classified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("classified_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["classified_by"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_conversations_user_id", "conversations", ["user_id"])

    # ── messages ──────────────────────────────────────────────────────────────
    op.create_table(
        "messages",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("conversation_id", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(20), nullable=False),
        sa.Column("content", sa.Text(), nullable=False, server_default=""),
        sa.Column("trace_id", sa.String(128), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("model_name", sa.String(100), nullable=True),
        sa.Column("agent_name", sa.String(100), nullable=True),
        sa.Column("metadata", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_messages_conversation_id", "messages", ["conversation_id"])
    op.create_index("ix_messages_trace_id", "messages", ["trace_id"])
    op.create_index("ix_messages_created_at", "messages", ["created_at"])

    # ── conversation_shares ───────────────────────────────────────────────────
    op.create_table(
        "conversation_shares",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("conversation_id", sa.Integer(), nullable=False),
        sa.Column("token", sa.String(64), nullable=False),
        sa.Column("mode", sa.String(20), nullable=False, server_default="read_only"),
        sa.Column("allow_fork", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("view_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token"),
    )
    op.create_index("ix_conversation_shares_conversation_id", "conversation_shares", ["conversation_id"])
    op.create_index("ix_conversation_shares_token", "conversation_shares", ["token"])

    # ── attachments ───────────────────────────────────────────────────────────
    op.create_table(
        "attachments",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("reference_id", sa.String(36), nullable=False),
        sa.Column("conversation_id", sa.Integer(), nullable=True),
        sa.Column("message_id", sa.Integer(), nullable=True),
        sa.Column("uploaded_by", sa.Integer(), nullable=True),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("content_type", sa.String(100), nullable=False, server_default="application/octet-stream"),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("storage_path", sa.String(500), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["message_id"], ["messages.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["uploaded_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("reference_id"),
    )
    op.create_index("ix_attachments_reference_id", "attachments", ["reference_id"])
    op.create_index("ix_attachments_conversation_id", "attachments", ["conversation_id"])

    # ── handoffs ──────────────────────────────────────────────────────────────
    op.create_table(
        "handoffs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("conversation_id", sa.Integer(), nullable=False),
        sa.Column("from_user_id", sa.Integer(), nullable=True),
        sa.Column("to_user_id", sa.Integer(), nullable=True),
        sa.Column("to_agent", sa.String(100), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["from_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["to_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_handoffs_conversation_id", "handoffs", ["conversation_id"])
    op.create_index("ix_handoffs_status", "handoffs", ["status"])

    # ── notifications ─────────────────────────────────────────────────────────
    op.create_table(
        "notifications",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("type", sa.String(50), nullable=False),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("body", sa.Text(), nullable=False, server_default=""),
        sa.Column("payload", postgresql.JSONB(), nullable=True),
        sa.Column("is_read", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_notifications_user_id", "notifications", ["user_id"])
    op.create_index("ix_notifications_type", "notifications", ["type"])
    op.create_index("ix_notifications_created_at", "notifications", ["created_at"])

    # ── token_usage: add audit columns ────────────────────────────────────────
    op.add_column("token_usage", sa.Column("conversation_id", sa.String(128), nullable=True))
    op.add_column("token_usage", sa.Column("trace_id", sa.String(128), nullable=True))
    op.create_index("ix_token_usage_conversation_id", "token_usage", ["conversation_id"])
    op.create_index("ix_token_usage_trace_id", "token_usage", ["trace_id"])


def downgrade() -> None:
    op.drop_index("ix_token_usage_trace_id", table_name="token_usage")
    op.drop_index("ix_token_usage_conversation_id", table_name="token_usage")
    op.drop_column("token_usage", "trace_id")
    op.drop_column("token_usage", "conversation_id")

    op.drop_table("notifications")
    op.drop_table("handoffs")
    op.drop_table("attachments")
    op.drop_table("conversation_shares")
    op.drop_table("messages")
    op.drop_table("conversations")
