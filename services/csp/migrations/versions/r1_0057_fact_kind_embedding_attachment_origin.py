# -*- coding: utf-8 -*-
"""事實種類與向量，以及附件來源。

偏好每一輪都注入。事實要有向量，只在與這一輪問題相近時注入。
平台產出的長文附件（origin=generated）不占對話容量。

Revision ID: r1_0057
Revises: r1_0056
Create Date: 2026-09-27
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "r1_0057"
down_revision: Union[str, None] = "r1_0056"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_EMBED_DIM = 4000


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    fact_columns = {column["name"] for column in inspector.get_columns("user_facts")}
    if "kind" not in fact_columns:
        op.add_column(
            "user_facts",
            sa.Column(
                "kind",
                sa.String(length=20),
                nullable=False,
                server_default="fact",
            ),
        )
        op.execute(
            "UPDATE user_facts SET kind = 'preference' WHERE key LIKE 'preference.%'"
        )
    if "embedding" not in fact_columns:
        if bind.dialect.name == "postgresql":
            op.execute(
                f"ALTER TABLE user_facts ADD COLUMN embedding halfvec({_EMBED_DIM})"
            )
        else:
            op.add_column("user_facts", sa.Column("embedding", sa.Text(), nullable=True))
    if "embedding_source_model" not in fact_columns:
        op.add_column(
            "user_facts",
            sa.Column("embedding_source_model", sa.String(length=200), nullable=True),
        )
    if "embedding_native_dim" not in fact_columns:
        op.add_column(
            "user_facts",
            sa.Column("embedding_native_dim", sa.Integer(), nullable=True),
        )
    if bind.dialect.name == "postgresql":
        op.execute(
            """
            CREATE INDEX IF NOT EXISTS ix_user_facts_embedding_hnsw
                ON user_facts
                USING hnsw (embedding halfvec_cosine_ops)
                WITH (m = 16, ef_construction = 64)
                WHERE embedding IS NOT NULL
            """
        )

    attachment_columns = {
        column["name"] for column in inspector.get_columns("attachments")
    }
    if "origin" not in attachment_columns:
        op.add_column(
            "attachments",
            sa.Column(
                "origin",
                sa.String(length=20),
                nullable=False,
                server_default="upload",
            ),
        )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_user_facts_embedding_hnsw")
    op.drop_column("attachments", "origin")
    op.drop_column("user_facts", "embedding_native_dim")
    op.drop_column("user_facts", "embedding_source_model")
    op.drop_column("user_facts", "embedding")
    op.drop_column("user_facts", "kind")
