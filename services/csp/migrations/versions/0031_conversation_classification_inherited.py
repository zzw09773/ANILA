"""Mark conversations whose classification was inherited via memory recall.

Adds ``conversations.classification_inherited`` to disambiguate the two
ways a conversation can end up classified:

  1. **originally classified** — caller targeted an agent with
     ``requires_encryption=true``, or an admin manually classified the
     thread via the existing ``/classify`` endpoint. ``classified=true``,
     ``classification_inherited=false``.

  2. **inherited classified** — caller targeted a non-classified agent
     (or a base LLM directly), but the platform's per-user memory
     pulled in chunks that were originally written from an encrypted
     source. Bell-LaPadula style "no write down": the consuming
     conversation must adopt the higher classification of any input
     it touched. ``classified=true``, ``classification_inherited=true``.

Why a separate column instead of repurposing ``classified_by``:

  * ``classified_by`` is a user FK. Inheritance has no user actor —
    the latch is automatic. Using NULL would conflate "auto-latch"
    with "agent-driven classification" (which also has no user).
  * Provenance matters in the UI: an inherited latch carries a
    different message ("此對話因引用過往加密記憶而升級為機密")
    from a manually-set one. The frontend needs a stable boolean to
    branch on.

Latch is one-way. Once ``classification_inherited`` flips to TRUE for
a conversation, it stays TRUE — clearing it would let a single
non-encrypted turn launder the classification.

Revision ID: 0031
Revises: 0030
Create Date: 2026-05-04
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0031"
down_revision: Union[str, None] = "0030"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if "conversations" not in inspector.get_table_names():  # pragma: no cover
        return
    existing = {col["name"] for col in inspector.get_columns("conversations")}
    if "classification_inherited" in existing:
        return

    op.add_column(
        "conversations",
        sa.Column(
            "classification_inherited",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE conversations DROP COLUMN IF EXISTS classification_inherited"
    )
