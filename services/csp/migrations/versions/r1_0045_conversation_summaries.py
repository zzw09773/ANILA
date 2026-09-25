# -*- coding: utf-8 -*-
"""對話摘要表，並清空開發期的舊記憶。

擁有者 2026-09-25：仍在開發、沒有真實使用者。舊的
``conversation_memory_chunks`` 與 ``user_facts`` 整表刪除，不轉換。
新的長期記憶只留事實（仍用 ``user_facts``）與每段對話一則摘要。

Revision ID: r1_0045
Revises: r1_0044
Create Date: 2026-09-25
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "r1_0045"
down_revision: Union[str, None] = "r1_0044"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_EMBED_DIM = 4000


def upgrade() -> None:
    # 開發期清空。降版無法把這些列救回來。
    op.execute("DELETE FROM conversation_memory_chunks")
    op.execute("DELETE FROM user_facts")

    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "conversation_summaries" in set(inspector.get_table_names()):
        return

    op.execute(
        f"""
        CREATE TABLE conversation_summaries (
            id BIGSERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL
                REFERENCES users(id) ON DELETE CASCADE,
            conversation_id INTEGER NOT NULL
                REFERENCES conversations(id) ON DELETE CASCADE,
            summary TEXT NOT NULL,
            covered_message_id INTEGER,
            embedding halfvec({_EMBED_DIM}),
            embedding_source_model VARCHAR(200),
            embedding_native_dim INTEGER,
            is_encrypted BOOLEAN NOT NULL DEFAULT FALSE,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL
                DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP WITH TIME ZONE NOT NULL
                DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT uq_conversation_summaries_conversation
                UNIQUE (conversation_id)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_conversation_summaries_user_id
            ON conversation_summaries (user_id)
        """
    )
    op.execute(
        """
        CREATE INDEX ix_conversation_summaries_embedding_hnsw
            ON conversation_summaries
            USING hnsw (embedding halfvec_cosine_ops)
            WITH (m = 16, ef_construction = 64)
            WHERE embedding IS NOT NULL
        """
    )


def downgrade() -> None:
    # 升級時已刪掉舊的 chunks／facts，降版再丟摘要就兩代都沒了。
    # 表裡還有列就拒絕，空表才允許拆掉。
    bind = op.get_bind()
    count = 0
    try:
        counted = bind.execute(sa.text("SELECT COUNT(*) FROM conversation_summaries"))
        count = int(counted.scalar() or 0)
    except Exception:
        count = 0
    if count > 0:
        raise RuntimeError("conversation_summaries 仍有資料，拒絕降版")
    op.execute("DROP INDEX IF EXISTS ix_conversation_summaries_embedding_hnsw")
    op.execute("DROP INDEX IF EXISTS ix_conversation_summaries_user_id")
    op.execute("DROP TABLE IF EXISTS conversation_summaries")
