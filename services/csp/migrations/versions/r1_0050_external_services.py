# -*- coding: utf-8 -*-
"""治理中心管理的外部服務：文件解析與語音辨識。

位址與憑證不再放在 .env。憑證欄是 CSP 專用金鑰的 enc::ext1::
外殼，不是 worker 拿 SECRET_KEY 解得開的 enc::v1::。

Revision ID: r1_0050
Revises: r1_0049
Create Date: 2026-09-26
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "r1_0050"
down_revision: Union[str, None] = "r1_0049"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "external_services",
        sa.Column("service_key", sa.String(length=40), primary_key=True),
        sa.Column(
            "enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column(
            "base_url",
            sa.String(length=500),
            nullable=False,
            server_default="",
        ),
        sa.Column("credential_envelope", sa.Text(), nullable=True),
        sa.Column(
            "protocol",
            sa.String(length=20),
            nullable=False,
            server_default="native",
        ),
        sa.Column(
            "openai_model",
            sa.String(length=120),
            nullable=False,
            server_default="whisper-1",
        ),
        sa.Column(
            "health_status",
            sa.String(length=20),
            nullable=False,
            server_default="unknown",
        ),
        sa.Column("health_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("health_detail", sa.Text(), nullable=True),
        sa.Column(
            "env_seeded",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_by_user_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(
            ["updated_by_user_id"],
            ["users.id"],
            name="fk_external_services_updated_by_user",
            ondelete="SET NULL",
        ),
    )
    # 兩列固定鍵。之後的匯入與畫面都假設它們在。
    op.execute(
        sa.text(
            """
            INSERT INTO external_services (
                service_key, enabled, base_url, protocol, openai_model,
                health_status, env_seeded, updated_at
            ) VALUES
                ('document_parser', false, '', 'native', 'whisper-1',
                 'unknown', false, CURRENT_TIMESTAMP),
                ('speech', false, '', 'native', 'whisper-1',
                 'unknown', false, CURRENT_TIMESTAMP)
            """
        )
    )


def downgrade() -> None:
    op.drop_table("external_services")
