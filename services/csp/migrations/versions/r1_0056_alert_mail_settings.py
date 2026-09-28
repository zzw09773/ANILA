# -*- coding: utf-8 -*-
"""警報寄信設定，以及稽核保留期改為 365 天。

寄信帳密放在治理中心，不放環境變數。密碼欄是既有憑證加密的 enc::v1::。
稽核 DELETE 觸發器原本寫死 180 天，與 AUDIT_RETENTION_DAYS 一起改成 365。

Revision ID: r1_0056
Revises: r1_0055
Create Date: 2026-09-27
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "r1_0056"
down_revision: Union[str, None] = "r1_0055"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_PREVIOUS_RETENTION_DAYS = 180


def _retention_function(days: int) -> str:
    return f"""
        CREATE OR REPLACE FUNCTION audit_ledger_retention_delete()
        RETURNS trigger AS $fn$
        BEGIN
            IF OLD.created_at IS NULL
               OR OLD.created_at >= now() - interval '{days} days' THEN
                RAISE EXCEPTION
                    '稽核帳為 append-only:% 只有超過保留期({days} 天)'
                    '的列可以刪除 (P2.7)', TG_TABLE_NAME
                    USING ERRCODE = '42501';
            END IF;
            RETURN OLD;
        END
        $fn$ LANGUAGE plpgsql
        """


def upgrade() -> None:
    op.create_table(
        "alert_mail_settings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("smtp_host", sa.String(length=253), nullable=False, server_default=""),
        sa.Column("smtp_port", sa.Integer(), nullable=False, server_default="587"),
        sa.Column("security", sa.String(length=20), nullable=False, server_default="starttls"),
        sa.Column("username", sa.String(length=320), nullable=False, server_default=""),
        sa.Column("password_envelope", sa.Text(), nullable=True),
        sa.Column("from_address", sa.String(length=320), nullable=False, server_default=""),
        sa.Column("recipients", sa.Text(), nullable=False, server_default=""),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("last_error_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_by_user_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(
            ["updated_by_user_id"],
            ["users.id"],
            name="fk_alert_mail_settings_updated_by_user",
            ondelete="SET NULL",
        ),
    )
    op.execute(
        sa.text(
            """
            INSERT INTO alert_mail_settings (
                id, enabled, smtp_host, smtp_port, security, username,
                from_address, recipients, updated_at
            ) VALUES (
                1, false, '', 587, 'starttls', '', '', '', CURRENT_TIMESTAMP
            )
            """
        )
    )
    op.create_table(
        "alert_mail_deliveries",
        sa.Column("fingerprint", sa.String(length=200), primary_key=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION audit_ledger_retention_delete()
        RETURNS trigger AS $fn$
        BEGIN
            IF OLD.created_at IS NULL
               OR OLD.created_at >= now() - interval '365 days' THEN
                RAISE EXCEPTION
                    '稽核帳為 append-only:% 只有超過保留期(365 天)'
                    '的列可以刪除 (P2.7)', TG_TABLE_NAME
                    USING ERRCODE = '42501';
            END IF;
            RETURN OLD;
        END
        $fn$ LANGUAGE plpgsql
        """
    )


def downgrade() -> None:
    op.execute(_retention_function(_PREVIOUS_RETENTION_DAYS))
    op.drop_table("alert_mail_deliveries")
    op.drop_table("alert_mail_settings")
