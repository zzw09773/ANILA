# -*- coding: utf-8 -*-
"""Bind privileged password sessions to their break-glass incident window.

Revision ID: r1_0019
Revises: r1_0018
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "r1_0019"
down_revision: Union[str, None] = "r1_0018"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "auth_sessions",
        sa.Column("break_glass_ticket", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "auth_sessions",
        sa.Column(
            "break_glass_expires_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    # Sessions minted before this revision have no durable incident binding.
    # They cannot be safely attributed after upgrade, so revoke their complete
    # refresh families and downgrade the now-dead rows to ordinary assurance
    # before installing the invariant. A fresh login in the current named
    # window is required.
    op.execute(
        sa.text(
            "UPDATE auth_refresh_tokens SET revoked_at = CURRENT_TIMESTAMP "
            "WHERE revoked_at IS NULL AND sid IN "
            "(SELECT sid FROM auth_sessions WHERE break_glass = true)"
        )
    )
    op.execute(
        sa.text(
            "UPDATE auth_sessions SET "
            "revoked_at = COALESCE(revoked_at, CURRENT_TIMESTAMP), "
            "revoke_reason = COALESCE(revoke_reason, "
            "'break_glass_binding_migration'), break_glass = false "
            "WHERE break_glass = true"
        )
    )
    op.create_check_constraint(
        "ck_auth_sessions_break_glass_binding",
        "auth_sessions",
        "(break_glass = false AND break_glass_ticket IS NULL "
        "AND break_glass_expires_at IS NULL) OR "
        "(break_glass = true AND break_glass_ticket IS NOT NULL "
        "AND break_glass_expires_at IS NOT NULL)",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_auth_sessions_break_glass_binding",
        "auth_sessions",
        type_="check",
    )
    op.drop_column("auth_sessions", "break_glass_expires_at")
    op.drop_column("auth_sessions", "break_glass_ticket")
