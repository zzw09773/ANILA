"""Add ``agents.bound_collection_id`` for RAG-agent csk- search scope (S-Q1).

A registered agent may declare a single collection its service token
(``csk-``) is allowed to search via CSP's HTTP search API. This collapses
the previous two-token model (separate ``CSP_SERVICE_TOKEN`` for inbound
Router→agent auth + ``CSP_SEARCH_TOKEN`` for outbound search) into one
``csk-``: the same credential now authorises search, but is hard-scoped to
this one collection (least privilege) and only if the agent's owner still
has access to it. NULL = non-RAG agent (no collection search at all).

``ON DELETE SET NULL`` so removing a collection silently unbinds dependent
agents rather than cascading them away.

Revision ID: 0038
Revises: 0037
Create Date: 2026-06-03
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0038"
down_revision: Union[str, None] = "0037"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("agents") as batch:
        batch.add_column(sa.Column("bound_collection_id", sa.Integer(), nullable=True))
        batch.create_foreign_key(
            "fk_agents_bound_collection_id",
            "ingestion_collections",
            ["bound_collection_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    with op.batch_alter_table("agents") as batch:
        batch.drop_constraint("fk_agents_bound_collection_id", type_="foreignkey")
        batch.drop_column("bound_collection_id")
