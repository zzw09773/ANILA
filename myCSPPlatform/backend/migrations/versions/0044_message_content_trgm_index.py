"""Trigram GIN index on ``messages.content`` for fast ILIKE search.

The conversation full-text search (GET /api/conversations/search) uses ILIKE
substring matching so it works for Chinese without a CJK tokenizer. pg_trgm's
GIN index makes that ILIKE fast at scale. pg_trgm ships with the pgvector
postgres image used here.

Revision ID: 0044
Revises: 0043
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0044"
down_revision: Union[str, None] = "0043"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_messages_content_trgm "
        "ON messages USING gin (content gin_trgm_ops)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_conversations_title_trgm "
        "ON conversations USING gin (title gin_trgm_ops)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_conversations_title_trgm")
    op.execute("DROP INDEX IF EXISTS ix_messages_content_trgm")
