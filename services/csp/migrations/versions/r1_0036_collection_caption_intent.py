# -*- coding: utf-8 -*-
"""Collection-level caption intent + per-image caption provenance.

Invariant
=========
A knowledge base can choose whether to caption figures and which model
to ask. That choice is *intent* and lives on ``ingestion_collections``.
The model that actually produced a caption is *fact* and lives on
``ingestion_images.caption_source_model`` — same shape as
``embedding_source_model`` (r1_0018). Intent and fact can disagree
(the model was swapped, or every caption failed); that disagreement
must stay visible.

NULL on the collection columns means "follow the platform-level
``enable_image_captions`` / ``VISION_MODEL``". A NOT NULL default would
freeze existing collections at create-time values so a later platform
change would not apply. Existing rows are not backfilled.

No CHECK constraint on model names. New models must not require a
migration; validation belongs in the API.

``ingestion_images`` is FORCE RLS (0037). Adding a nullable column
does not change the policy. ``ingestion_collections`` has no RLS
policy (measured).

Revision ID: r1_0036
Revises: r1_0035
Create Date: 2026-08-24
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "r1_0036"
down_revision: Union[str, None] = "r1_0035"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "ingestion_collections" in tables:
        cols = {c["name"] for c in inspector.get_columns("ingestion_collections")}
        if "caption_enabled" not in cols:
            op.add_column(
                "ingestion_collections",
                sa.Column("caption_enabled", sa.Boolean(), nullable=True),
            )
        if "caption_model" not in cols:
            op.add_column(
                "ingestion_collections",
                sa.Column("caption_model", sa.String(length=200), nullable=True),
            )

    if "ingestion_images" in tables:
        cols = {c["name"] for c in inspector.get_columns("ingestion_images")}
        if "caption_source_model" not in cols:
            op.add_column(
                "ingestion_images",
                sa.Column("caption_source_model", sa.String(length=200), nullable=True),
            )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "ingestion_images" in tables:
        cols = {c["name"] for c in inspector.get_columns("ingestion_images")}
        if "caption_source_model" in cols:
            op.drop_column("ingestion_images", "caption_source_model")

    if "ingestion_collections" in tables:
        cols = {c["name"] for c in inspector.get_columns("ingestion_collections")}
        if "caption_model" in cols:
            op.drop_column("ingestion_collections", "caption_model")
        if "caption_enabled" in cols:
            op.drop_column("ingestion_collections", "caption_enabled")
