# -*- coding: utf-8 -*-
"""Per-user star / folder / tags for conversations.

Star, folder assignment, and user tags are one user's *view* of a
conversation — not facts about the thread. Named sharing (P4.3) means
user A's organisation must not appear (or race-overwrite) for user B.
``users.ui_settings`` already holds the folder *list*, but a blob keyed
by conversation id grows unboundedly and cannot be joined into the list
endpoint; columns on ``conversations`` would make share recipients share
one organisation. This table is the third shape: one row per
(user, conversation), returned inline on list/get so the sidebar stays
one round-trip.

The system ``classified`` tag is never stored in ``user_tags`` — it is
derived from ``conversations.classified`` on every read.

Revision ID: r1_0024
Revises: r1_0023
Create Date: 2026-07-31
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "r1_0024"
down_revision: Union[str, None] = "r1_0023"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "conversation_user_meta",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("conversation_id", sa.Integer(), nullable=False),
        sa.Column(
            "starred",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "folder",
            sa.String(length=64),
            nullable=False,
            server_default="all",
        ),
        sa.Column(
            "user_tags",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id",
            "conversation_id",
            name="uq_conversation_user_meta_user_conv",
        ),
    )
    op.create_index(
        "ix_conversation_user_meta_user_id",
        "conversation_user_meta",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        "ix_conversation_user_meta_conversation_id",
        "conversation_user_meta",
        ["conversation_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_conversation_user_meta_conversation_id",
        table_name="conversation_user_meta",
    )
    op.drop_index(
        "ix_conversation_user_meta_user_id",
        table_name="conversation_user_meta",
    )
    op.drop_table("conversation_user_meta")
