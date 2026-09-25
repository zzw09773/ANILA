# -*- coding: utf-8 -*-
"""清掉對話摘要。開發期沒有真實使用者，整表刪除後由閒置整理重寫。

先前模型呼叫沒帶金鑰，401 之後會把使用者原文存成摘要。那些列不能留。
擁有者已同意清記憶資料。降版救不回被刪的列。

Revision ID: r1_0047
Revises: r1_0046
Create Date: 2026-09-25
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "r1_0047"
down_revision: Union[str, None] = "r1_0046"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("DELETE FROM conversation_summaries")


def downgrade() -> None:
    # 列已刪，沒有可還原的內容。
    pass
