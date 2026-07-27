# -*- coding: utf-8 -*-
"""W2-11: classification sampling review ledger (input-side correctness).

Revision ID: r1_0042
Revises: r1_0041
Create Date: 2026-07-27

Why this exists
---------------
The five-level classification machine previously had no continuous input-side
review surface — only a one-shot cutover inventory. W2-11 adds a durable
sampling-review table so authorized reviewers can attest a random sample of
documents (title + level + uploader + collection), and the attest action has
a row to hang audit metadata on.

⚠ Revision number: ``r1_0039`` was reserved for an earlier draft of this
package but never landed; ``r1_0040`` / ``r1_0041`` shipped first. This
revision is intentionally ``r1_0042`` (``r1_0039`` stays an empty number).
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "r1_0042"
down_revision: Union[str, None] = "r1_0041"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_LEVELS = ("無機密", "營業秘密", "機密", "極機密", "絕對機密")
_OUTCOMES = ("confirmed", "mismatch", "needs_followup")


def upgrade() -> None:
    op.create_table(
        "classification_sampling_reviews",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "document_id",
            sa.Integer(),
            sa.ForeignKey("ingestion_documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "collection_id",
            sa.Integer(),
            sa.ForeignKey("ingestion_collections.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "reviewer_user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("document_level_at_review", sa.String(length=20), nullable=False),
        sa.Column("attested_level", sa.String(length=20), nullable=False),
        sa.Column("outcome", sa.String(length=32), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "document_level_at_review IN ("
            + ", ".join(f"'{level}'" for level in _LEVELS)
            + ")",
            name="ck_classification_sampling_reviews_document_level",
        ),
        sa.CheckConstraint(
            "attested_level IN ("
            + ", ".join(f"'{level}'" for level in _LEVELS)
            + ")",
            name="ck_classification_sampling_reviews_attested_level",
        ),
        sa.CheckConstraint(
            "outcome IN ("
            + ", ".join(f"'{value}'" for value in _OUTCOMES)
            + ")",
            name="ck_classification_sampling_reviews_outcome",
        ),
    )
    op.create_index(
        "ix_classification_sampling_reviews_created_at",
        "classification_sampling_reviews",
        ["created_at"],
    )
    op.create_index(
        "ix_classification_sampling_reviews_collection_id",
        "classification_sampling_reviews",
        ["collection_id"],
    )
    op.create_index(
        "ix_classification_sampling_reviews_document_id",
        "classification_sampling_reviews",
        ["document_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_classification_sampling_reviews_document_id",
        table_name="classification_sampling_reviews",
    )
    op.drop_index(
        "ix_classification_sampling_reviews_collection_id",
        table_name="classification_sampling_reviews",
    )
    op.drop_index(
        "ix_classification_sampling_reviews_created_at",
        table_name="classification_sampling_reviews",
    )
    op.drop_table("classification_sampling_reviews")
