# -*- coding: utf-8 -*-
"""平台對話入口的出廠顯示名稱改為 ANILA。

只改 name 是 anila-router、而且顯示名稱仍是「ANILA 自動選助手」的那一列。
管理員已經改過的名稱不動。模型識別碼 anila-router 不變。
降版只把仍叫 ANILA 的這一列改回舊文案；其他列不動。

Revision ID: r1_0064
Revises: r1_0063
Create Date: 2026-09-29
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "r1_0064"
down_revision: Union[str, None] = "r1_0063"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_STOCK_DISPLAY_NAME = "ANILA 自動選助手"
_PLATFORM_DISPLAY_NAME = "ANILA"


def rename_platform_chat_entry_display(bind, *, new_name: str, old_name: str) -> None:
    bind.execute(
        sa.text(
            "UPDATE model_registry "
            "SET display_name = :new_name "
            "WHERE name = 'anila-router' AND display_name = :old_name"
        ),
        {"new_name": new_name, "old_name": old_name},
    )


def upgrade() -> None:
    rename_platform_chat_entry_display(
        op.get_bind(),
        new_name=_PLATFORM_DISPLAY_NAME,
        old_name=_STOCK_DISPLAY_NAME,
    )


def downgrade() -> None:
    # 顯示名稱只是外觀。降版時分不出「這一版改成 ANILA」與「管理員自己取名 ANILA」，
    # 改回去會蓋掉管理員的名稱，所以不動。
    pass
