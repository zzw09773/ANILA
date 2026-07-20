"""Persist four-state ModelRegistry provider locality and transport snapshots.

Revision ID: r1_0030
Revises: r1_0029

Historical rows are intentionally backfilled as ``unclassified``.  The
legacy ``is_internal`` boolean is not trustworthy provenance and is therefore
never used to infer a provider locality during migration.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "r1_0030"
down_revision: Union[str, None] = "r1_0029"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_JSON = sa.JSON(none_as_null=True).with_variant(
    postgresql.JSONB(none_as_null=True), "postgresql"
)
_LOCALITIES = (
    "'external_governed', 'internal_isolated', 'internal_shim', 'unclassified'"
)
_EXTERNAL_SNAPSHOT = (
    "provider_locality != 'external_governed' OR ("
    "transport_target IS NOT NULL "
    "AND CAST(transport_target AS TEXT) != 'null' "
    "AND transport_target_sha256 IS NOT NULL "
    "AND model_registry_revision IS NOT NULL "
    "AND egress_policy_id IS NOT NULL "
    "AND upstream_provider_locality IS NULL "
    "AND upstream_transport_target IS NULL "
    "AND upstream_transport_target_sha256 IS NULL "
    "AND upstream_egress_policy_id IS NULL)"
)
_ISOLATED_SNAPSHOT = (
    "provider_locality != 'internal_isolated' OR ("
    "transport_target IS NOT NULL "
    "AND CAST(transport_target AS TEXT) != 'null' "
    "AND transport_target_sha256 IS NOT NULL "
    "AND model_registry_revision IS NOT NULL "
    "AND upstream_provider_locality IS NULL "
    "AND upstream_transport_target IS NULL "
    "AND upstream_transport_target_sha256 IS NULL "
    "AND egress_policy_id IS NULL "
    "AND upstream_egress_policy_id IS NULL)"
)
_SHIM_SNAPSHOT = (
    "provider_locality != 'internal_shim' OR ("
    "transport_target IS NOT NULL "
    "AND CAST(transport_target AS TEXT) != 'null' "
    "AND transport_target_sha256 IS NOT NULL "
    "AND model_registry_revision IS NOT NULL "
    "AND upstream_provider_locality IS NOT NULL "
    "AND upstream_provider_locality != 'internal_shim' "
    "AND upstream_transport_target IS NOT NULL "
    "AND CAST(upstream_transport_target AS TEXT) != 'null' "
    "AND upstream_transport_target_sha256 IS NOT NULL "
    "AND egress_policy_id IS NULL "
    "AND ((upstream_provider_locality = 'external_governed' "
    "AND upstream_egress_policy_id IS NOT NULL) "
    "OR (upstream_provider_locality IN "
    "('internal_isolated', 'unclassified') "
    "AND upstream_egress_policy_id IS NULL)))"
)


def upgrade() -> None:
    op.add_column(
        "model_registry",
        sa.Column(
            "provider_locality",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'unclassified'"),
        ),
    )
    op.add_column("model_registry", sa.Column("transport_target", _JSON, nullable=True))
    op.add_column(
        "model_registry",
        sa.Column("transport_target_sha256", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "model_registry",
        sa.Column("model_registry_revision", sa.String(length=256), nullable=True),
    )
    op.add_column(
        "model_registry",
        sa.Column("upstream_provider_locality", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "model_registry",
        sa.Column("upstream_transport_target", _JSON, nullable=True),
    )
    op.add_column(
        "model_registry",
        sa.Column(
            "upstream_transport_target_sha256",
            sa.String(length=64),
            nullable=True,
        ),
    )
    op.add_column(
        "model_registry",
        sa.Column("egress_policy_id", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "model_registry",
        sa.Column("upstream_egress_policy_id", sa.String(length=128), nullable=True),
    )

    # Make the historical backfill explicit even though the NOT NULL server
    # default already covers new rows.  This keeps the migration safe on
    # databases that materialize a NULL while adding a column with a default.
    op.execute(
        "UPDATE model_registry SET provider_locality = 'unclassified' "
        "WHERE provider_locality IS NULL"
    )
    op.create_check_constraint(
        "ck_model_registry_provider_locality",
        "model_registry",
        f"provider_locality IN ({_LOCALITIES})",
    )
    op.create_check_constraint(
        "ck_model_registry_upstream_provider_locality",
        "model_registry",
        "upstream_provider_locality IS NULL OR "
        f"upstream_provider_locality IN ({_LOCALITIES})",
    )
    op.create_check_constraint(
        "ck_model_registry_external_snapshot",
        "model_registry",
        _EXTERNAL_SNAPSHOT,
    )
    op.create_check_constraint(
        "ck_model_registry_isolated_snapshot",
        "model_registry",
        _ISOLATED_SNAPSHOT,
    )
    op.create_check_constraint(
        "ck_model_registry_shim_snapshot",
        "model_registry",
        _SHIM_SNAPSHOT,
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_model_registry_shim_snapshot",
        "model_registry",
        type_="check",
    )
    op.drop_constraint(
        "ck_model_registry_isolated_snapshot",
        "model_registry",
        type_="check",
    )
    op.drop_constraint(
        "ck_model_registry_external_snapshot",
        "model_registry",
        type_="check",
    )
    op.drop_constraint(
        "ck_model_registry_upstream_provider_locality",
        "model_registry",
        type_="check",
    )
    op.drop_constraint(
        "ck_model_registry_provider_locality",
        "model_registry",
        type_="check",
    )
    op.drop_column("model_registry", "upstream_egress_policy_id")
    op.drop_column("model_registry", "egress_policy_id")
    op.drop_column("model_registry", "upstream_transport_target_sha256")
    op.drop_column("model_registry", "upstream_transport_target")
    op.drop_column("model_registry", "upstream_provider_locality")
    op.drop_column("model_registry", "model_registry_revision")
    op.drop_column("model_registry", "transport_target_sha256")
    op.drop_column("model_registry", "transport_target")
    op.drop_column("model_registry", "provider_locality")
