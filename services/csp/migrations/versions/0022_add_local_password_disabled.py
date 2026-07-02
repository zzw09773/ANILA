"""Add ``users.local_password_disabled`` boolean.

Sprint 6 X / B2: paving the road for SSO to replace local login while
not yet closing local login. This flag lets an admin opt individual
users into SSO-only — once flipped to TRUE, ``authenticate_user`` will
refuse the local password path even if the password hash matches. The
flag defaults to FALSE so existing accounts are unaffected.

Future Sprint will add a global ``settings.LOCAL_LOGIN_DISABLED`` env
override and a UI to bulk-flip the flag during cutover; today both are
admin-controlled per-user knobs.

Revision ID: 0022
Revises: 0021
Create Date: 2026-04-27
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0022"
down_revision: Union[str, None] = "0021"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = {c["name"] for c in inspector.get_columns("users")}
    if "local_password_disabled" in existing:
        return
    with op.batch_alter_table("users") as batch:
        batch.add_column(
            sa.Column(
                "local_password_disabled",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("FALSE"),
            )
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = {c["name"] for c in inspector.get_columns("users")}
    if "local_password_disabled" not in existing:
        return
    with op.batch_alter_table("users") as batch:
        batch.drop_column("local_password_disabled")
