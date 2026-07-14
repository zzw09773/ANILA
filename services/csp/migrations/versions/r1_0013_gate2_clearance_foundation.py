# -*- coding: utf-8 -*-
"""Gate 2 G2a clearance, compartment, and need-to-know foundation.

Revision ID: r1_0013
Revises: r1_0012
Create Date: 2026-07-12

``r1_0012`` is produced by the preceding Gate 2 classification-invariant
slice.  This branch intentionally references it even when reviewed alone;
the full PostgreSQL upgrade runs after both slices are integrated.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "r1_0013"
down_revision: Union[str, None] = "r1_0012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_LEVELS_SQL = "'無機密', '營業秘密', '機密', '極機密', '絕對機密'"


def _created_at() -> sa.Column:
    return sa.Column(
        "created_at",
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.text("CURRENT_TIMESTAMP"),
    )


def upgrade() -> None:
    op.create_table(
        "security_compartments",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "is_active", sa.Boolean(), nullable=False, server_default=sa.true()
        ),
        sa.Column("created_by_user_id", sa.Integer(), nullable=False),
        _created_at(),
        sa.CheckConstraint(
            "length(trim(code)) > 0 AND code = upper(code) AND "
            "code NOT LIKE '% %'",
            name="ck_security_compartments_code_canonical",
        ),
        sa.CheckConstraint(
            "length(trim(name)) > 0",
            name="ck_security_compartments_name_nonempty",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"], ["users.id"], ondelete="RESTRICT"
        ),
        sa.UniqueConstraint("code", name="uq_security_compartments_code"),
    )

    op.create_table(
        "clearance_grants",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("subject_user_id", sa.Integer(), nullable=False),
        sa.Column("max_classification_level", sa.String(length=20), nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("basis_ticket", sa.String(length=255), nullable=False),
        sa.Column("issued_by_user_id", sa.Integer(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_by_user_id", sa.Integer(), nullable=True),
        _created_at(),
        sa.CheckConstraint(
            f"max_classification_level IN ({_LEVELS_SQL})",
            name="ck_clearance_grants_classification_level",
        ),
        sa.CheckConstraint(
            "expires_at > valid_from",
            name="ck_clearance_grants_time_order",
        ),
        sa.CheckConstraint(
            "length(trim(basis_ticket)) > 0",
            name="ck_clearance_grants_basis_ticket_nonempty",
        ),
        sa.CheckConstraint(
            "(revoked_at IS NULL AND revoked_by_user_id IS NULL) OR "
            "(revoked_at IS NOT NULL AND revoked_by_user_id IS NOT NULL)",
            name="ck_clearance_grants_revocation_pair",
        ),
        sa.ForeignKeyConstraint(
            ["subject_user_id"], ["users.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["issued_by_user_id"], ["users.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["revoked_by_user_id"], ["users.id"], ondelete="RESTRICT"
        ),
    )
    op.create_index(
        "ix_clearance_grants_subject_window",
        "clearance_grants",
        ["subject_user_id", "revoked_at", "valid_from", "expires_at"],
    )
    op.create_index(
        "ix_clearance_grants_expires_at", "clearance_grants", ["expires_at"]
    )

    op.create_table(
        "clearance_grant_compartments",
        sa.Column("clearance_grant_id", sa.Integer(), primary_key=True),
        sa.Column("compartment_id", sa.Integer(), primary_key=True),
        _created_at(),
        sa.ForeignKeyConstraint(
            ["clearance_grant_id"],
            ["clearance_grants.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["compartment_id"],
            ["security_compartments.id"],
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "clearance_grant_id",
            "compartment_id",
            name="uq_clearance_grant_compartment",
        ),
    )
    op.create_index(
        "ix_clearance_grant_compartments_compartment",
        "clearance_grant_compartments",
        ["compartment_id"],
    )

    op.create_table(
        "collection_access_grants",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("clearance_grant_id", sa.Integer(), nullable=False),
        sa.Column("collection_id", sa.Integer(), nullable=False),
        sa.Column(
            "membership_granted",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
        sa.Column(
            "need_to_know",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
        sa.Column("basis_ticket", sa.String(length=255), nullable=False),
        sa.Column("issued_by_user_id", sa.Integer(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_by_user_id", sa.Integer(), nullable=True),
        _created_at(),
        sa.CheckConstraint(
            "membership_granted OR need_to_know",
            name="ck_collection_access_grants_nonempty_authority",
        ),
        sa.CheckConstraint(
            "length(trim(basis_ticket)) > 0",
            name="ck_collection_access_grants_basis_ticket_nonempty",
        ),
        sa.CheckConstraint(
            "(revoked_at IS NULL AND revoked_by_user_id IS NULL) OR "
            "(revoked_at IS NOT NULL AND revoked_by_user_id IS NOT NULL)",
            name="ck_collection_access_grants_revocation_pair",
        ),
        sa.ForeignKeyConstraint(
            ["clearance_grant_id"],
            ["clearance_grants.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["collection_id"],
            ["ingestion_collections.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["issued_by_user_id"], ["users.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["revoked_by_user_id"], ["users.id"], ondelete="RESTRICT"
        ),
        sa.UniqueConstraint(
            "clearance_grant_id",
            "collection_id",
            name="uq_collection_access_grant",
        ),
    )
    op.create_index(
        "ix_collection_access_grants_collection",
        "collection_access_grants",
        ["collection_id", "revoked_at"],
    )

    op.create_table(
        "collection_required_compartments",
        sa.Column("collection_id", sa.Integer(), primary_key=True),
        sa.Column("compartment_id", sa.Integer(), primary_key=True),
        sa.Column("basis_ticket", sa.String(length=255), nullable=False),
        sa.Column("assigned_by_user_id", sa.Integer(), nullable=False),
        _created_at(),
        sa.CheckConstraint(
            "length(trim(basis_ticket)) > 0",
            name="ck_collection_required_compartments_basis_nonempty",
        ),
        sa.ForeignKeyConstraint(
            ["collection_id"],
            ["ingestion_collections.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["compartment_id"],
            ["security_compartments.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["assigned_by_user_id"], ["users.id"], ondelete="RESTRICT"
        ),
    )
    op.create_index(
        "ix_collection_required_compartments_compartment",
        "collection_required_compartments",
        ["compartment_id"],
    )

    op.create_table(
        "document_required_compartments",
        sa.Column("document_id", sa.Integer(), primary_key=True),
        sa.Column("compartment_id", sa.Integer(), primary_key=True),
        sa.Column("basis_ticket", sa.String(length=255), nullable=False),
        sa.Column("assigned_by_user_id", sa.Integer(), nullable=False),
        _created_at(),
        sa.CheckConstraint(
            "length(trim(basis_ticket)) > 0",
            name="ck_document_required_compartments_basis_nonempty",
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["ingestion_documents.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["compartment_id"],
            ["security_compartments.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["assigned_by_user_id"], ["users.id"], ondelete="RESTRICT"
        ),
    )
    op.create_index(
        "ix_document_required_compartments_compartment",
        "document_required_compartments",
        ["compartment_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_document_required_compartments_compartment",
        table_name="document_required_compartments",
    )
    op.drop_table("document_required_compartments")
    op.drop_index(
        "ix_collection_required_compartments_compartment",
        table_name="collection_required_compartments",
    )
    op.drop_table("collection_required_compartments")
    op.drop_index(
        "ix_collection_access_grants_collection",
        table_name="collection_access_grants",
    )
    op.drop_table("collection_access_grants")
    op.drop_index(
        "ix_clearance_grant_compartments_compartment",
        table_name="clearance_grant_compartments",
    )
    op.drop_table("clearance_grant_compartments")
    op.drop_index("ix_clearance_grants_expires_at", table_name="clearance_grants")
    op.drop_index(
        "ix_clearance_grants_subject_window", table_name="clearance_grants"
    )
    op.drop_table("clearance_grants")
    op.drop_table("security_compartments")
