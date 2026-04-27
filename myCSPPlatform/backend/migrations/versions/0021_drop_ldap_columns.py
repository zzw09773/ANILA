"""Drop legacy LDAP columns from auth_providers.

Sprint 6 X / A1: LDAP 已自系統移除（將以 OIDC SSO 取代），Sprint 5 X 的
`fix(security)` commit 在 application 層停止讀寫 ldap_*，但 schema 上
還留著 8 個 deprecated 欄位。此 migration 把它們乾淨 drop 掉，避免：

1. 廢欄位被新功能不小心撞名復用（潛在對映錯誤）。
2. 既有 ``ldap_bind_password`` plaintext 留在備份 / DB dump 裡。
3. 安全掃描工具誤報 schema 仍含 LDAP 機密欄位。

down() 重新 ADD COLUMN nullable，可以還原 schema 結構但不會還原資料 —
拿到本 migration 的下游若需要回退，必須自行從備份匯入舊資料。

Revision ID: 0021
Revises: 0020
Create Date: 2026-04-27
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "0021"
down_revision: Union[str, None] = "0020"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# 與 auth_providers 表上的 LDAP 欄位一一對應（schema 來自
# startup_migrations._ensure_column 的 LDAP 段）。
_LDAP_COLUMNS: tuple[tuple[str, sa.types.TypeEngine, dict], ...] = (
    ("ldap_server_uri", sa.String(255), {}),
    ("ldap_bind_dn", sa.String(255), {}),
    ("ldap_bind_password", sa.String(255), {}),
    ("ldap_base_dn", sa.String(255), {}),
    ("ldap_user_filter", sa.String(255), {}),
    ("ldap_email_attribute", sa.String(100), {}),
    ("ldap_display_name_attribute", sa.String(100), {}),
    ("ldap_start_tls", sa.Boolean(), {"server_default": sa.text("FALSE")}),
)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = {c["name"] for c in inspector.get_columns("auth_providers")}

    with op.batch_alter_table("auth_providers") as batch:
        for col_name, _type, _opts in _LDAP_COLUMNS:
            # idempotent：已 drop 過的就跳過，避免 re-run 失敗。
            if col_name in existing:
                batch.drop_column(col_name)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = {c["name"] for c in inspector.get_columns("auth_providers")}

    with op.batch_alter_table("auth_providers") as batch:
        for col_name, col_type, opts in _LDAP_COLUMNS:
            if col_name in existing:
                continue
            batch.add_column(sa.Column(col_name, col_type, nullable=True, **opts))
