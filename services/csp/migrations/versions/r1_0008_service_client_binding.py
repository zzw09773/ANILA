# -*- coding: utf-8 -*-
"""R-SEC — audit-callback client↔service binding (doc 07 §3 schema gap).

Slice 7a's ``POST /api/services/{service_id}/audit-callbacks`` authenticated
ANY valid Service Client Token, so any holder of one legitimate integration
key could inject audit events for ANY service (cross-service audit-trail
pollution). doc 07 §3's ``registered_services`` schema block lacked a
client↔service binding column, so the endpoint had no way to require that the
presented token *belongs to* the target service.

This migration adds ``registered_services.service_client_id`` — a nullable FK
to ``service_clients.id`` (``ON DELETE SET NULL``, indexed). A NULL binding
means the service has not been bound to any Service Client; under the
fail-closed / default-deny ruling (ADR-0008) an unbound service rejects all
audit callbacks (403). The column is nullable because the endpoint is
brand-new with zero production data — no backfill / compat concern — and a
service legitimately starts life unbound until an admin binds it.

Revision ID: r1_0008
Revises: r1_0007
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "r1_0008"
down_revision: Union[str, None] = "r1_0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "registered_services",
        sa.Column(
            "service_client_id",
            sa.Integer(),
            sa.ForeignKey(
                "service_clients.id",
                name="fk_registered_services_service_client_id",
                ondelete="SET NULL",
            ),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_registered_services_service_client_id",
        "registered_services",
        ["service_client_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_registered_services_service_client_id",
        table_name="registered_services",
    )
    op.drop_constraint(
        "fk_registered_services_service_client_id",
        "registered_services",
        type_="foreignkey",
    )
    op.drop_column("registered_services", "service_client_id")
