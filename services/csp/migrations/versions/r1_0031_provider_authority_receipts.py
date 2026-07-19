"""Bind Gate 5 receipts to the immutable provider-authority snapshot.

Revision ID: r1_0031
Revises: r1_0030
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "r1_0031"
down_revision: Union[str, None] = "r1_0030"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_COLUMNS = (
    ("provider_binding_id", sa.String(length=128)),
    ("provider_locality", sa.String(length=32)),
    ("transport_target_sha256", sa.String(length=64)),
    ("model_registry_revision", sa.String(length=256)),
    ("upstream_provider_locality", sa.String(length=32)),
    ("upstream_transport_target_sha256", sa.String(length=64)),
    ("egress_policy_id", sa.String(length=128)),
    ("upstream_egress_policy_id", sa.String(length=128)),
    ("profile_content_sha256", sa.String(length=64)),
    ("inventory_sha256", sa.String(length=64)),
)


def upgrade() -> None:
    for name, type_ in _COLUMNS:
        op.add_column(
            "model_governance_receipts",
            sa.Column(name, type_, nullable=True),
        )
    op.create_check_constraint(
        "ck_model_governance_receipts_provider_snapshot",
        "model_governance_receipts",
        "(provider_binding_id IS NULL AND provider_locality IS NULL "
        "AND transport_target_sha256 IS NULL "
        "AND model_registry_revision IS NULL "
        "AND upstream_provider_locality IS NULL "
        "AND upstream_transport_target_sha256 IS NULL "
        "AND egress_policy_id IS NULL AND upstream_egress_policy_id IS NULL) OR "
        "(provider_binding_id IS NOT NULL AND provider_locality IS NOT NULL "
        "AND transport_target_sha256 IS NOT NULL "
        "AND model_registry_revision IS NOT NULL "
        "AND profile_content_sha256 IS NOT NULL AND inventory_sha256 IS NOT NULL "
        "AND ((upstream_provider_locality IS NULL "
        "AND upstream_transport_target_sha256 IS NULL) OR "
        "(upstream_provider_locality IS NOT NULL "
        "AND upstream_transport_target_sha256 IS NOT NULL)))",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_model_governance_receipts_provider_snapshot",
        "model_governance_receipts",
        type_="check",
    )
    for name, _ in reversed(_COLUMNS):
        op.drop_column("model_governance_receipts", name)
