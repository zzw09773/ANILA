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


def _existing_tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _existing_indexes(table: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    if table not in inspector.get_table_names():
        return set()
    return {ix["name"] for ix in inspector.get_indexes(table)}


def upgrade() -> None:
    # Every DDL operation is guarded INDEPENDENTLY because this chain has to
    # survive a replay: `test_w26_startup_ddl_absorption_pg` stamps the version
    # pointer back and runs `upgrade head` again, which is how a real
    # deployment recovers when the pointer and the schema disagree. r1_0036 /
    # 0038 / 0040 / 0041 all tolerate that; this one did not, and the gap went
    # unnoticed because the test skips itself when no PostgreSQL DSN is set, so
    # the runs that gated the original merge never executed it.
    #
    # Guarding per-object rather than returning early on "table exists" is the
    # difference between a replay and a REPAIR. A database whose table survived
    # but lost an index needs the missing index recreated; an early return
    # leaves that gap permanently, and check_orm_pg_drift classifies it as
    # STRUCTURAL — a hard CI failure with no migration able to fix it.
    if "classification_sampling_reviews" not in _existing_tables():
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
    existing = _existing_indexes("classification_sampling_reviews")
    for name, column in (
        ("ix_classification_sampling_reviews_created_at", "created_at"),
        ("ix_classification_sampling_reviews_collection_id", "collection_id"),
        ("ix_classification_sampling_reviews_document_id", "document_id"),
    ):
        if name not in existing:
            op.create_index(name, "classification_sampling_reviews", [column])


def downgrade() -> None:
    if "classification_sampling_reviews" not in _existing_tables():
        return
    existing = _existing_indexes("classification_sampling_reviews")
    for name in (
        "ix_classification_sampling_reviews_document_id",
        "ix_classification_sampling_reviews_collection_id",
        "ix_classification_sampling_reviews_created_at",
    ):
        if name in existing:
            op.drop_index(name, table_name="classification_sampling_reviews")
    op.drop_table("classification_sampling_reviews")
