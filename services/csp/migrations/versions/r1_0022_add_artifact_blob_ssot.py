# -*- coding: utf-8 -*-
"""Add immutable CSP-owned ArtifactVersion blob metadata.

Revision ID: r1_0022
Revises: r1_0021
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "r1_0022"
down_revision: Union[str, None] = "r1_0021"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("artifact_versions", sa.Column("blob_key", sa.String(80)))
    op.add_column("artifact_versions", sa.Column("blob_size_bytes", sa.Integer()))
    op.add_column("artifact_versions", sa.Column("media_type", sa.String(200)))
    op.add_column("artifact_versions", sa.Column("original_filename", sa.String(255)))
    op.add_column(
        "artifact_versions",
        sa.Column(
            "is_active", sa.Boolean(), nullable=False, server_default=sa.true()
        ),
    )
    op.add_column("artifact_versions", sa.Column("revoked_at", sa.DateTime(timezone=True)))
    op.add_column("artifact_versions", sa.Column("revoked_by_user_id", sa.Integer()))
    op.add_column("artifact_versions", sa.Column("revocation_reason", sa.String(500)))
    op.create_foreign_key(
        "fk_artifact_versions_revoked_by_user_id_users",
        "artifact_versions",
        "users",
        ["revoked_by_user_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_unique_constraint(
        "uq_artifact_versions_blob_key", "artifact_versions", ["blob_key"]
    )
    op.create_check_constraint(
        "ck_artifact_versions_blob_size_positive",
        "artifact_versions",
        "blob_size_bytes IS NULL OR blob_size_bytes > 0",
    )
    op.create_check_constraint(
        "ck_artifact_versions_content_hash_sha256",
        "artifact_versions",
        "content_hash IS NULL OR content_hash ~ '^[0-9a-f]{64}$'",
    )
    op.create_check_constraint(
        "ck_artifact_versions_revocation_state",
        "artifact_versions",
        "(is_active = true AND revoked_at IS NULL AND revoked_by_user_id IS NULL) "
        "OR (is_active = false AND revoked_at IS NOT NULL)",
    )
    op.create_check_constraint(
        "ck_artifact_versions_blob_metadata_complete",
        "artifact_versions",
        "(blob_key IS NULL AND blob_size_bytes IS NULL AND media_type IS NULL "
        "AND original_filename IS NULL) OR "
        "(blob_key IS NOT NULL AND blob_size_bytes IS NOT NULL "
        "AND media_type IS NOT NULL AND original_filename IS NOT NULL "
        "AND content_hash IS NOT NULL)",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_artifact_versions_blob_metadata_complete",
        "artifact_versions",
        type_="check",
    )
    op.drop_constraint(
        "ck_artifact_versions_revocation_state", "artifact_versions", type_="check"
    )
    op.drop_constraint(
        "ck_artifact_versions_content_hash_sha256",
        "artifact_versions",
        type_="check",
    )
    op.drop_constraint(
        "ck_artifact_versions_blob_size_positive",
        "artifact_versions",
        type_="check",
    )
    op.drop_constraint(
        "uq_artifact_versions_blob_key", "artifact_versions", type_="unique"
    )
    op.drop_constraint(
        "fk_artifact_versions_revoked_by_user_id_users",
        "artifact_versions",
        type_="foreignkey",
    )
    for column in (
        "revocation_reason",
        "revoked_by_user_id",
        "revoked_at",
        "is_active",
        "original_filename",
        "media_type",
        "blob_size_bytes",
        "blob_key",
    ):
        op.drop_column("artifact_versions", column)
