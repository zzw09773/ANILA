# -*- coding: utf-8 -*-
"""把既有向量搬進可並存多個模型的表，不重新嵌入。

每一筆向量記下產生它的 model_id 與實際維度。欄位是不帶長度的 halfvec。
各模型的 HNSW 用部分表達式索引，維度超過 4000 的維持截斷（那是 halfvec
HNSW 的上限）。搜尋用的「目前模型」與治理中心指定的角色分開：換角色只會
開始重建，不會立刻改搜尋。

Revision ID: r1_0059
Revises: r1_0058
Create Date: 2026-09-28
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "r1_0059"
down_revision: Union[str, None] = "r1_0058"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_HNSW_MAX = 4000


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        op.create_table(
            "embedding_activation",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("active_model_id", sa.Integer(), nullable=True),
            sa.Column("previous_model_id", sa.Integer(), nullable=True),
            sa.Column("switched_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("rebuild_target_model_id", sa.Integer(), nullable=True),
            sa.Column("rebuild_status", sa.String(length=20), nullable=True),
            sa.Column("rebuild_done", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("rebuild_total", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("rebuild_errors", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("rebuild_started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("rebuild_updated_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("rebuild_last_error", sa.Text(), nullable=True),
        )
        return

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS embedding_activation (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            active_model_id INTEGER REFERENCES model_registry(id),
            previous_model_id INTEGER REFERENCES model_registry(id),
            switched_at TIMESTAMP WITH TIME ZONE,
            rebuild_target_model_id INTEGER REFERENCES model_registry(id),
            rebuild_status VARCHAR(20),
            rebuild_done INTEGER NOT NULL DEFAULT 0,
            rebuild_total INTEGER NOT NULL DEFAULT 0,
            rebuild_errors INTEGER NOT NULL DEFAULT 0,
            rebuild_started_at TIMESTAMP WITH TIME ZONE,
            rebuild_updated_at TIMESTAMP WITH TIME ZONE,
            rebuild_last_error TEXT
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS embedding_vectors (
            subject TEXT NOT NULL,
            subject_id BIGINT NOT NULL,
            model_id INTEGER NOT NULL REFERENCES model_registry(id) ON DELETE CASCADE,
            collection_id INTEGER,
            dims INTEGER NOT NULL,
            native_dims INTEGER NOT NULL,
            embedding halfvec NOT NULL,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (subject, subject_id, model_id),
            CONSTRAINT embedding_vectors_subject_chk
                CHECK (subject IN ('chunk', 'fact', 'summary'))
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS embedding_rebuild_failures (
            subject TEXT NOT NULL,
            subject_id BIGINT NOT NULL,
            model_id INTEGER NOT NULL REFERENCES model_registry(id) ON DELETE CASCADE,
            attempts INTEGER NOT NULL DEFAULT 1,
            last_error TEXT,
            PRIMARY KEY (subject, subject_id, model_id)
        )
        """
    )
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_chunks_content_trgm
            ON document_chunks USING gin (content gin_trgm_ops)
        """
    )

    # 既有向量原樣搬過去，標上目前的平台嵌入模型。沒有指定時，依
    # embedding_source_model 的名字對到登錄表（含已停用）。
    op.execute(
        """
        INSERT INTO embedding_vectors
            (subject, subject_id, model_id, collection_id, dims, native_dims, embedding)
        SELECT 'chunk', c.id, m.id, c.collection_id,
               vector_dims(c.embedding),
               COALESCE(c.embedding_native_dim, vector_dims(c.embedding)),
               c.embedding
          FROM document_chunks c
          JOIN model_registry m
            ON m.is_platform_embedding = true
           AND m.model_type = 'embedding'
         WHERE c.embedding IS NOT NULL
        ON CONFLICT (subject, subject_id, model_id) DO NOTHING
        """
    )
    op.execute(
        """
        INSERT INTO embedding_vectors
            (subject, subject_id, model_id, collection_id, dims, native_dims, embedding)
        SELECT 'chunk', c.id, m.id, c.collection_id,
               vector_dims(c.embedding),
               COALESCE(c.embedding_native_dim, vector_dims(c.embedding)),
               c.embedding
          FROM document_chunks c
          JOIN model_registry m
            ON m.model_type = 'embedding'
           AND lower(m.name) = lower(c.embedding_source_model)
         WHERE c.embedding IS NOT NULL
           AND NOT EXISTS (
               SELECT 1 FROM model_registry p
                WHERE p.is_platform_embedding = true
                  AND p.model_type = 'embedding'
           )
        ON CONFLICT (subject, subject_id, model_id) DO NOTHING
        """
    )
    op.execute(
        """
        INSERT INTO embedding_vectors
            (subject, subject_id, model_id, collection_id, dims, native_dims, embedding)
        SELECT 'fact', f.id, m.id, NULL,
               vector_dims(f.embedding),
               COALESCE(f.embedding_native_dim, vector_dims(f.embedding)),
               f.embedding
          FROM user_facts f
          JOIN model_registry m
            ON m.model_type = 'embedding'
           AND (
                m.is_platform_embedding = true
                OR (
                    lower(m.name) = lower(f.embedding_source_model)
                    AND NOT EXISTS (
                        SELECT 1 FROM model_registry p
                         WHERE p.is_platform_embedding = true
                           AND p.model_type = 'embedding'
                    )
                )
           )
         WHERE f.embedding IS NOT NULL
        ON CONFLICT (subject, subject_id, model_id) DO NOTHING
        """
    )
    op.execute(
        """
        INSERT INTO embedding_vectors
            (subject, subject_id, model_id, collection_id, dims, native_dims, embedding)
        SELECT 'summary', s.id, m.id, NULL,
               vector_dims(s.embedding),
               COALESCE(s.embedding_native_dim, vector_dims(s.embedding)),
               s.embedding
          FROM conversation_summaries s
          JOIN model_registry m
            ON m.model_type = 'embedding'
           AND (
                m.is_platform_embedding = true
                OR (
                    lower(m.name) = lower(s.embedding_source_model)
                    AND NOT EXISTS (
                        SELECT 1 FROM model_registry p
                         WHERE p.is_platform_embedding = true
                           AND p.model_type = 'embedding'
                    )
                )
           )
         WHERE s.embedding IS NOT NULL
        ON CONFLICT (subject, subject_id, model_id) DO NOTHING
        """
    )

    op.execute(
        """
        INSERT INTO embedding_activation (id, active_model_id)
        SELECT 1, id
          FROM model_registry
         WHERE is_platform_embedding = true
           AND model_type = 'embedding'
         LIMIT 1
        ON CONFLICT (id) DO NOTHING
        """
    )
    # 沒有平台角色時，用搬進去的向量最多的那個模型當搜尋目前使用的模型。
    op.execute(
        """
        INSERT INTO embedding_activation (id, active_model_id)
        SELECT 1, model_id
          FROM embedding_vectors
         GROUP BY model_id
         ORDER BY count(*) DESC, model_id
         LIMIT 1
        ON CONFLICT (id) DO NOTHING
        """
    )

    _create_partial_indexes(bind)

    for index_name in (
        "ix_chunks_embedding_hnsw",
        "ix_user_facts_embedding_hnsw",
        "ix_conversation_summaries_embedding_hnsw",
    ):
        op.execute(f"DROP INDEX IF EXISTS {index_name}")
    for table in ("document_chunks", "user_facts", "conversation_summaries"):
        op.execute(f"ALTER TABLE {table} ALTER COLUMN embedding TYPE halfvec")

    op.execute("ALTER TABLE embedding_vectors ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE embedding_vectors FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY embedding_vectors_scope ON embedding_vectors
            FOR ALL
            USING (
                subject <> 'chunk'
                OR collection_id = NULLIF(
                    current_setting('anila.collection_id', true),
                    ''
                )::int
            )
            WITH CHECK (
                subject <> 'chunk'
                OR collection_id = NULLIF(
                    current_setting('anila.collection_id', true),
                    ''
                )::int
            )
        """
    )


def _create_partial_indexes(bind) -> None:
    rows = bind.execute(
        sa.text(
            """
            SELECT DISTINCT model_id, dims, subject
              FROM embedding_vectors
             WHERE dims BETWEEN 1 AND :max_dims
            """
        ),
        {"max_dims": _HNSW_MAX},
    ).fetchall()
    for model_id, dims, subject in rows:
        if subject not in {"chunk", "fact", "summary"}:
            continue
        name = f"ix_embvec_hnsw_{subject}_{int(model_id)}_{int(dims)}"
        op.execute(
            f"CREATE INDEX IF NOT EXISTS {name} ON embedding_vectors "
            f"USING hnsw ((embedding::halfvec({int(dims)})) halfvec_cosine_ops) "
            f"WITH (m = 16, ef_construction = 64) "
            f"WHERE model_id = {int(model_id)} AND subject = '{subject}' "
            f"AND dims = {int(dims)}"
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("DROP POLICY IF EXISTS embedding_vectors_scope ON embedding_vectors")
    op.execute("DROP TABLE IF EXISTS embedding_rebuild_failures")
    op.execute("DROP TABLE IF EXISTS embedding_vectors")
    op.execute("DROP TABLE IF EXISTS embedding_activation")
