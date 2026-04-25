"""Add ``agent_llm_credentials`` for dev-supplied judge / external LLMs.

Phase 2 Sprint 3 (docs/ingestion-platform-design.md §3.1, §6.5). Devs
running the Chunking Evaluator may bring their own LLM endpoint (e.g.
their own OpenAI key, a private gpt-4o-mini, or a co-located vLLM) to
serve as judge in LLM-as-judge scoring. Storing the key in the
collection's ``chunking_config`` would leak it through audit logs;
storing it AES-encrypted in a dedicated table keeps that surface
locked down.

Encryption shape:
- ``api_key_encrypted`` BYTEA: AES-256-GCM ciphertext.
- ``api_key_nonce``     BYTEA: per-row 12-byte nonce.
- ``api_key_tag``       BYTEA: 16-byte GCM auth tag (separate column so
  rotating the AAD scheme doesn't require ALTER TABLE).
- Key derivation: PBKDF2(SHA-256, ``CSP_SECRET_KEY``, 100k iter,
  salt=``"agent_llm_credentials_v1"``). Application-level concern;
  schema just stores the bytes.

Uniqueness: ``UNIQUE (agent_id, name)`` so each agent has its own
named slot ("openai-judge" / "local-llama70b") without colliding
across agents.

Soft delete: not added in Sprint 3. If a credential is revoked, the
row is hard-deleted; we don't keep ciphertext for revoked credentials
because the audit story is "nobody could decrypt this anyway after
master key rotation".

Revision ID: 0017
Revises: 0016
Create Date: 2026-04-25
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0017"
down_revision: Union[str, None] = "0016"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "agent_llm_credentials",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "agent_id",
            sa.Integer(),
            sa.ForeignKey("agents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("endpoint_url", sa.String(length=1000), nullable=False),
        sa.Column("model_name", sa.String(length=200), nullable=False),
        sa.Column("api_key_encrypted", sa.LargeBinary(), nullable=False),
        sa.Column("api_key_nonce", sa.LargeBinary(length=16), nullable=False),
        sa.Column("api_key_tag", sa.LargeBinary(length=16), nullable=False),
        sa.Column("last_used_at", sa.DateTime(), nullable=True),
        sa.Column(
            "created_by",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.UniqueConstraint("agent_id", "name", name="uq_agent_llm_credentials_name"),
    )
    op.create_index(
        "ix_agent_llm_credentials_agent_id",
        "agent_llm_credentials",
        ["agent_id"],
    )

    # csp_app needs ALTER privileges via ownership transfer (FORCE RLS
    # owners). Mirror the 0014 pattern.
    op.execute("ALTER TABLE agent_llm_credentials OWNER TO csp_app")


def downgrade() -> None:
    op.drop_index(
        "ix_agent_llm_credentials_agent_id",
        table_name="agent_llm_credentials",
    )
    op.drop_table("agent_llm_credentials")
