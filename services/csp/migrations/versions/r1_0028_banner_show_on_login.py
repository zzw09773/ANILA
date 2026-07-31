# -*- coding: utf-8 -*-
"""banners.show_on_login —— 讓公告能出現在登入頁。

Revision ID: r1_0028
Revises: r1_0027
Create Date: 2026-07-31

登入頁是全院最多人看到的一頁,而看到「等待管理員核准」的人**還沒有帳號**,
也就拿不到 ``GET /api/banners/active``(需要 token)。擁有者在治理中心貼的
公告因此只有已經進得來的人讀得到,正好漏掉需要它的那一群。

這個欄位是那條路的開關,而且刻意是**逐則 opt-in**:

- ``server_default false`` —— 既有的每一則公告在 upgrade 之後仍然不公開。
  這個功能上線不會讓任何一則現有公告變成任何人讀得到。
- ``nullable=False`` —— 沒有「不確定」的第三態;讀取端不必猜 NULL 的意思。

沒有 index:banners 是個位數到兩位數列的表,登入頁一次全掃比維護索引便宜。

downgrade 直接砍欄位。掉的只是「哪幾則曾經被勾去登入頁」這件事,公告內容
本身留著;重新勾一次即可,沒有不可逆的資料損失。
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "r1_0028"
down_revision: Union[str, None] = "r1_0027"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "banners",
        sa.Column(
            "show_on_login",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("banners", "show_on_login")
