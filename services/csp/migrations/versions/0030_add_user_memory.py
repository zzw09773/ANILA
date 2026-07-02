"""User-scoped memory: structured facts + cross-conversation RAG chunks.

Adds two tables for the platform-level memory system (P1 of the
ANILALM memory feature). This is *not* the same as anila-core's
agent-level memdir — that lives in the agent process's filesystem and
is per-agent. This memory is per-user, persisted in Postgres, queried
synchronously on every chat completion, and written asynchronously
after each turn.

Schema decisions
================

``user_facts`` — small structured facts (name, role, long-term
preferences). Unique per ``(user_id, key)`` so the writer can use a
naive ``ON CONFLICT DO UPDATE`` upsert and the LLM extractor can
overwrite stale values without dedup logic. ``confidence`` lets a
future scorer down-weight low-evidence extractions during retrieval;
P1 reads them all unconditionally.

``conversation_memory_chunks`` — per-message embeddings for semantic
recall across conversations. Mirrors ``document_chunks``' choice of
``halfvec(4000)`` + HNSW (cosine) — the platform already truncates
NV-embed-V2's native 4096-d to 4000-d to fit halfvec's HNSW dim cap,
and reusing the same dim keeps the embedder client (Sprint 1
``Embedder``) and runtime caches uniform.

The ``is_encrypted`` column tags content that originated from an
agent with ``requires_encryption=true``. The retrieval path returns
this flag alongside each hit so ``proxy.py`` can latch the *new*
conversation into encrypted state when any retrieved chunk was
encrypted at write time — Bell-LaPadula style "no write down"
inheritance. Without this column, encrypted material would silently
leak into unclassified conversations via memory.

Both tables cascade on user delete so account removal also wipes
memory; conversation delete also cascades the chunk side because the
chunk's content is meaningless once the originating thread is gone.

Revision ID: 0030
Revises: 0029
Create Date: 2026-05-03
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0030"
down_revision: Union[str, None] = "0029"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Mirrors document_chunks.embedding (migration 0015). Keep these two in
# sync — runtime client truncates NV-embed-V2 4096-d → 4000-d.
_EMBED_DIM = 4000


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())

    # ── user_facts ──────────────────────────────────────────────────────
    if "user_facts" not in existing_tables:
        op.create_table(
            "user_facts",
            sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
            sa.Column(
                "user_id",
                sa.Integer(),
                sa.ForeignKey("users.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("key", sa.String(length=120), nullable=False),
            sa.Column("value", sa.Text(), nullable=False),
            sa.Column(
                "source_conversation_id",
                sa.Integer(),
                sa.ForeignKey("conversations.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("source_message_id", sa.Integer(), nullable=True),
            sa.Column(
                "confidence",
                sa.Float(),
                nullable=False,
                server_default=sa.text("1.0"),
            ),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("CURRENT_TIMESTAMP"),
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("CURRENT_TIMESTAMP"),
            ),
            sa.UniqueConstraint("user_id", "key", name="uq_user_facts_user_key"),
        )
        op.create_index(
            "ix_user_facts_user_id",
            "user_facts",
            ["user_id"],
        )

    # ── conversation_memory_chunks ──────────────────────────────────────
    if "conversation_memory_chunks" not in existing_tables:
        # SQLAlchemy doesn't ship a halfvec type, so we issue raw SQL for
        # the table create. This matches how 0014 / 0015 handle the
        # equivalent column on document_chunks.
        op.execute(
            f"""
            CREATE TABLE conversation_memory_chunks (
                id BIGSERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL
                    REFERENCES users(id) ON DELETE CASCADE,
                conversation_id INTEGER NOT NULL
                    REFERENCES conversations(id) ON DELETE CASCADE,
                message_id INTEGER,
                role VARCHAR(20) NOT NULL,
                content TEXT NOT NULL,
                embedding halfvec({_EMBED_DIM}) NOT NULL,
                is_encrypted BOOLEAN NOT NULL DEFAULT FALSE,
                created_at TIMESTAMP WITH TIME ZONE NOT NULL
                    DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        # HNSW index for ANN cosine search; mirrors document_chunks. m=16
        # / ef_construction=64 are the platform default — tune later if
        # recall@k drops on real workloads.
        op.execute(
            """
            CREATE INDEX ix_memchunks_embedding_hnsw
                ON conversation_memory_chunks
                USING hnsw (embedding halfvec_cosine_ops)
                WITH (m = 16, ef_construction = 64)
            """
        )
        # Time-ordered scan for "most recent N" fallback when ANN misses.
        op.create_index(
            "ix_memchunks_user_recent",
            "conversation_memory_chunks",
            ["user_id", sa.text("created_at DESC")],
        )
        # Lookup by source conversation — needed by P3 (encryption
        # inheritance audit) and by the upcoming UI's "see what was
        # remembered from this thread" panel.
        op.create_index(
            "ix_memchunks_conversation_id",
            "conversation_memory_chunks",
            ["conversation_id"],
        )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_memchunks_conversation_id")
    op.execute("DROP INDEX IF EXISTS ix_memchunks_user_recent")
    op.execute("DROP INDEX IF EXISTS ix_memchunks_embedding_hnsw")
    op.execute("DROP TABLE IF EXISTS conversation_memory_chunks")

    op.execute("DROP INDEX IF EXISTS ix_user_facts_user_id")
    op.execute("DROP TABLE IF EXISTS user_facts")
