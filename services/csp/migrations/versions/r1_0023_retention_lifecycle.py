"""Add durable retention lifecycle, counters, and reaper lease.

Revision ID: r1_0023
Revises: r1_0022
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "r1_0023"
down_revision: Union[str, None] = "r1_0022"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _lifecycle_columns() -> list[sa.Column]:
    return [
        sa.Column("lifecycle_state", sa.String(20), nullable=False,
                  server_default="active"),
        sa.Column("archive_due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("erase_due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("erased_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("legal_hold", sa.Boolean(), nullable=False,
                  server_default=sa.text("false")),
        sa.Column("legal_hold_reason", sa.String(500), nullable=True),
    ]


def upgrade() -> None:
    bind = op.get_bind()
    duplicate_job = bind.execute(sa.text(
        "SELECT job_id FROM artifacts WHERE job_id IS NOT NULL "
        "GROUP BY job_id HAVING count(*) > 1 LIMIT 1"
    )).scalar()
    if duplicate_job is not None:
        raise RuntimeError(
            "artifacts.job_id contains duplicates; retention/idempotency migration "
            "refuses to choose an authoritative artifact"
        )

    op.add_column("artifacts", sa.Column(
        "active_version_count", sa.Integer(), nullable=False, server_default="1"
    ))
    op.add_column("artifacts", sa.Column(
        "erased_version_count", sa.Integer(), nullable=False, server_default="0"
    ))
    op.create_unique_constraint("uq_artifacts_job_id", "artifacts", ["job_id"])
    op.create_check_constraint(
        "ck_artifacts_version_counters_nonnegative", "artifacts",
        "active_version_count >= 0 AND erased_version_count >= 0",
    )

    op.drop_constraint(
        "ck_artifact_versions_revocation_state", "artifact_versions", type_="check"
    )
    for column in _lifecycle_columns():
        op.add_column("artifact_versions", column)
    op.create_index(
        "ix_artifact_versions_lifecycle_state",
        "artifact_versions", ["lifecycle_state"],
    )
    op.create_index(
        "ix_artifact_versions_erase_due_at",
        "artifact_versions", ["erase_due_at"],
    )
    op.execute(
        "UPDATE artifact_versions SET lifecycle_state = "
        "CASE WHEN is_active THEN 'active' ELSE 'revoked' END"
    )
    op.create_check_constraint(
        "ck_artifact_versions_revocation_state", "artifact_versions",
        "(is_active = true AND lifecycle_state = 'active' "
        "AND revoked_at IS NULL AND erased_at IS NULL) OR "
        "(is_active = false AND lifecycle_state IN "
        "('archived','revoked','erase_due','erased'))",
    )
    op.create_check_constraint(
        "ck_artifact_versions_lifecycle_state", "artifact_versions",
        "lifecycle_state IN ('active','archived','revoked','erase_due','erased')",
    )
    op.create_check_constraint(
        "ck_artifact_versions_lifecycle_timestamps", "artifact_versions",
        "(lifecycle_state = 'active' AND archived_at IS NULL AND erased_at IS NULL) OR "
        "(lifecycle_state = 'archived' AND archived_at IS NOT NULL "
        "AND erase_due_at IS NOT NULL AND erased_at IS NULL) OR "
        "(lifecycle_state = 'revoked' AND revoked_at IS NOT NULL "
        "AND erased_at IS NULL) OR "
        "(lifecycle_state = 'erase_due' AND erase_due_at IS NOT NULL "
        "AND erased_at IS NULL) OR "
        "(lifecycle_state = 'erased' AND erased_at IS NOT NULL "
        "AND blob_key IS NULL AND blob_size_bytes IS NULL AND media_type IS NULL "
        "AND original_filename IS NULL)",
    )
    op.create_check_constraint(
        "ck_artifact_versions_legal_hold_reason", "artifact_versions",
        "(legal_hold = true AND legal_hold_reason IS NOT NULL "
        "AND length(legal_hold_reason) > 0) OR "
        "(legal_hold = false AND legal_hold_reason IS NULL)",
    )
    op.add_column("artifact_jobs", sa.Column(
        "artifact_upload_sha256", sa.String(64), nullable=True
    ))
    op.add_column("artifact_jobs", sa.Column(
        "artifact_upload_metadata_digest", sa.String(64), nullable=True
    ))

    for column in _lifecycle_columns():
        op.add_column("ingestion_collections", column)
    op.add_column("ingestion_collections", sa.Column(
        "image_count", sa.Integer(), nullable=False, server_default="0"
    ))
    op.add_column("ingestion_collections", sa.Column(
        "artifact_count", sa.Integer(), nullable=False, server_default="0"
    ))
    op.create_index(
        "ix_ingestion_collections_lifecycle_state", "ingestion_collections",
        ["lifecycle_state"],
    )
    op.create_index(
        "ix_ingestion_collections_erase_due_at", "ingestion_collections",
        ["erase_due_at"],
    )
    op.execute(
        "UPDATE ingestion_collections SET lifecycle_state = "
        "CASE WHEN status='archived' THEN 'archived' ELSE 'active' END, "
        "archived_at = CASE WHEN status='archived' THEN updated_at ELSE NULL END, "
        "erase_due_at = CASE WHEN status='archived' "
        "THEN updated_at + interval '365 days' ELSE NULL END"
    )
    op.create_check_constraint(
        "ck_ingestion_collections_lifecycle_state", "ingestion_collections",
        "lifecycle_state IN ('active','archived','erase_due','erased')",
    )
    op.create_check_constraint(
        "ck_ingestion_collections_lifecycle_timestamps", "ingestion_collections",
        "(lifecycle_state = 'active' AND archived_at IS NULL AND erased_at IS NULL) OR "
        "(lifecycle_state = 'archived' AND archived_at IS NOT NULL "
        "AND erase_due_at IS NOT NULL AND erased_at IS NULL) OR "
        "(lifecycle_state = 'erase_due' AND erase_due_at IS NOT NULL "
        "AND erased_at IS NULL) OR "
        "(lifecycle_state = 'erased' AND erased_at IS NOT NULL)",
    )
    op.create_check_constraint(
        "ck_ingestion_collections_legal_hold_reason", "ingestion_collections",
        "(legal_hold = true AND legal_hold_reason IS NOT NULL "
        "AND length(legal_hold_reason) > 0) OR "
        "(legal_hold = false AND legal_hold_reason IS NULL)",
    )
    op.create_check_constraint(
        "ck_ingestion_collections_counters_nonnegative", "ingestion_collections",
        "document_count >= 0 AND chunk_count >= 0 AND bytes_stored >= 0 "
        "AND image_count >= 0 AND artifact_count >= 0",
    )

    for column in _lifecycle_columns():
        op.add_column("ingestion_documents", column)
    op.create_index(
        "ix_ingestion_documents_lifecycle_state", "ingestion_documents",
        ["lifecycle_state"],
    )
    op.create_index(
        "ix_ingestion_documents_erase_due_at", "ingestion_documents",
        ["erase_due_at"],
    )
    op.create_check_constraint(
        "ck_ingestion_documents_lifecycle_state", "ingestion_documents",
        "lifecycle_state IN ('active','archived','erase_due','erased')",
    )
    op.create_check_constraint(
        "ck_ingestion_documents_lifecycle_timestamps", "ingestion_documents",
        "(lifecycle_state = 'active' AND archived_at IS NULL AND erased_at IS NULL) OR "
        "(lifecycle_state = 'archived' AND archived_at IS NOT NULL "
        "AND erase_due_at IS NOT NULL AND erased_at IS NULL) OR "
        "(lifecycle_state = 'erase_due' AND erase_due_at IS NOT NULL "
        "AND erased_at IS NULL) OR "
        "(lifecycle_state = 'erased' AND erased_at IS NOT NULL)",
    )
    op.create_check_constraint(
        "ck_ingestion_documents_legal_hold_reason", "ingestion_documents",
        "(legal_hold = true AND legal_hold_reason IS NOT NULL "
        "AND length(legal_hold_reason) > 0) OR "
        "(legal_hold = false AND legal_hold_reason IS NULL)",
    )
    op.create_check_constraint(
        "ck_ingestion_documents_counters_nonnegative", "ingestion_documents",
        "chunk_count >= 0 AND (bytes IS NULL OR bytes >= 0)",
    )

    op.execute("""
        UPDATE artifacts a SET
          active_version_count=(SELECT count(*) FROM artifact_versions v
                                WHERE v.artifact_id=a.id AND v.is_active=true
                                  AND v.revoked_at IS NULL),
          erased_version_count=0
    """)
    op.execute("""
        UPDATE ingestion_collections c SET
          document_count=(SELECT count(*) FROM ingestion_documents d
                          WHERE d.collection_id=c.id),
          chunk_count=COALESCE((SELECT sum(d.chunk_count)
                               FROM ingestion_documents d
                               WHERE d.collection_id=c.id),0),
          bytes_stored=COALESCE((SELECT sum(d.bytes)
                                FROM ingestion_documents d
                                WHERE d.collection_id=c.id),0),
          image_count=(SELECT count(*) FROM ingestion_images i
                       WHERE i.collection_id=c.id)
    """)
    # ArtifactJob.collection_id is the one authoritative counter bucket.
    # NULL means no collection counter; a multi-collection SourceSnapshot is
    # counted once in the job's primary collection rather than once per source.
    op.execute("""
        UPDATE ingestion_collections c SET artifact_count=(
          SELECT count(*) FROM artifacts a
          JOIN artifact_jobs j ON j.job_id=a.job_id
          WHERE j.collection_id=c.id
        )
    """)

    op.create_table(
        "retention_reaper_leases",
        sa.Column("lease_name", sa.String(64), primary_key=True),
        sa.Column("lease_token", sa.String(64), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("CURRENT_TIMESTAMP")),
    )


def downgrade() -> None:
    op.drop_table("retention_reaper_leases")

    op.drop_constraint(
        "ck_ingestion_documents_counters_nonnegative", "ingestion_documents",
        type_="check",
    )
    op.drop_constraint(
        "ck_ingestion_documents_legal_hold_reason", "ingestion_documents",
        type_="check",
    )
    op.drop_constraint(
        "ck_ingestion_documents_lifecycle_timestamps", "ingestion_documents",
        type_="check",
    )
    op.drop_constraint(
        "ck_ingestion_documents_lifecycle_state", "ingestion_documents",
        type_="check",
    )
    op.drop_index("ix_ingestion_documents_erase_due_at",
                  table_name="ingestion_documents")
    op.drop_index("ix_ingestion_documents_lifecycle_state",
                  table_name="ingestion_documents")
    for name in (
        "legal_hold_reason", "legal_hold", "erased_at", "erase_due_at",
        "archived_at", "archive_due_at", "lifecycle_state",
    ):
        op.drop_column("ingestion_documents", name)

    op.drop_constraint(
        "ck_ingestion_collections_counters_nonnegative", "ingestion_collections",
        type_="check",
    )
    op.drop_constraint(
        "ck_ingestion_collections_legal_hold_reason", "ingestion_collections",
        type_="check",
    )
    op.drop_constraint(
        "ck_ingestion_collections_lifecycle_timestamps", "ingestion_collections",
        type_="check",
    )
    op.drop_constraint(
        "ck_ingestion_collections_lifecycle_state", "ingestion_collections",
        type_="check",
    )
    op.drop_index("ix_ingestion_collections_erase_due_at",
                  table_name="ingestion_collections")
    op.drop_index("ix_ingestion_collections_lifecycle_state",
                  table_name="ingestion_collections")
    op.drop_column("ingestion_collections", "artifact_count")
    op.drop_column("ingestion_collections", "image_count")
    for name in (
        "legal_hold_reason", "legal_hold", "erased_at", "erase_due_at",
        "archived_at", "archive_due_at", "lifecycle_state",
    ):
        op.drop_column("ingestion_collections", name)

    op.drop_column("artifact_jobs", "artifact_upload_metadata_digest")
    op.drop_column("artifact_jobs", "artifact_upload_sha256")
    op.drop_constraint(
        "ck_artifact_versions_legal_hold_reason", "artifact_versions",
        type_="check",
    )
    op.drop_constraint(
        "ck_artifact_versions_lifecycle_timestamps", "artifact_versions",
        type_="check",
    )
    op.drop_constraint(
        "ck_artifact_versions_lifecycle_state", "artifact_versions", type_="check"
    )
    op.drop_constraint(
        "ck_artifact_versions_revocation_state", "artifact_versions", type_="check"
    )
    op.drop_index("ix_artifact_versions_erase_due_at",
                  table_name="artifact_versions")
    op.drop_index("ix_artifact_versions_lifecycle_state",
                  table_name="artifact_versions")
    for name in (
        "legal_hold_reason", "legal_hold", "erased_at", "erase_due_at",
        "archived_at", "archive_due_at", "lifecycle_state",
    ):
        op.drop_column("artifact_versions", name)
    op.create_check_constraint(
        "ck_artifact_versions_revocation_state", "artifact_versions",
        "(is_active = true AND revoked_at IS NULL AND revoked_by_user_id IS NULL) "
        "OR (is_active = false AND revoked_at IS NOT NULL)",
    )
    op.drop_constraint(
        "ck_artifacts_version_counters_nonnegative", "artifacts", type_="check"
    )
    op.drop_constraint("uq_artifacts_job_id", "artifacts", type_="unique")
    op.drop_column("artifacts", "erased_version_count")
    op.drop_column("artifacts", "active_version_count")
