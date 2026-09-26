# -*- coding: utf-8 -*-
"""JWT 簽章金鑰圈。

私鑰加密後放在 jwt_signing_keys。狀態 next／active／retiring／retired。
同時只能有一列 active、一列 next。

Revision ID: r1_0052
Revises: r1_0051
Create Date: 2026-09-26

並行工作若也從 r1_0049 長出遷移，修 head 時以這個 revision id 辨認，
不要和別的 r1_0050 混淆。
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "r1_0052"
down_revision: Union[str, None] = "r1_0051"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "jwt_signing_keys",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("kid", sa.String(length=64), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("private_ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("private_nonce", sa.LargeBinary(), nullable=False),
        sa.Column("private_tag", sa.LargeBinary(), nullable=False),
        sa.Column("public_pem", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retiring_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "accept_missing_iat",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.CheckConstraint(
            "state IN ('next', 'active', 'retiring', 'retired')",
            name="ck_jwt_signing_keys_state",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("kid"),
    )
    op.create_index(
        "uq_jwt_signing_keys_one_active",
        "jwt_signing_keys",
        ["state"],
        unique=True,
        postgresql_where=sa.text("state = 'active'"),
        sqlite_where=sa.text("state = 'active'"),
    )
    op.create_index(
        "uq_jwt_signing_keys_one_next",
        "jwt_signing_keys",
        ["state"],
        unique=True,
        postgresql_where=sa.text("state = 'next'"),
        sqlite_where=sa.text("state = 'next'"),
    )


def downgrade() -> None:
    op.drop_index("uq_jwt_signing_keys_one_next", table_name="jwt_signing_keys")
    op.drop_index("uq_jwt_signing_keys_one_active", table_name="jwt_signing_keys")
    op.drop_table("jwt_signing_keys")
