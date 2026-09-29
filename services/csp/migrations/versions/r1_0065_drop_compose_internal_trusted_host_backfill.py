# -*- coding: utf-8 -*-
"""拿掉開機從 ANILA_TRUSTED_HOSTS 自動寫進表的 compose 內部名稱。

只刪 host 為 docling 或 router、建立者是空的、而且備註仍是當時
自動匯入那句的列。IP、其他主機、管理員自己加的列留下。
降版不把列加回去。

Revision ID: r1_0065
Revises: r1_0064
Create Date: 2026-09-30
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "r1_0065"
down_revision: Union[str, None] = "r1_0064"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_AUTO_IMPORT_NOTE = "imported from ANILA_TRUSTED_HOSTS env at startup"


def drop_auto_imported_compose_hosts(bind) -> None:
    bind.execute(
        sa.text(
            "DELETE FROM trusted_hosts "
            "WHERE lower(host) IN ('docling', 'router') "
            "AND created_by_user_id IS NULL "
            "AND note = :note"
        ),
        {"note": _AUTO_IMPORT_NOTE},
    )


def upgrade() -> None:
    drop_auto_imported_compose_hosts(op.get_bind())


def downgrade() -> None:
    pass
