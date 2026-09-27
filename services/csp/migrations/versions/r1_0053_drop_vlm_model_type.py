# -*- coding: utf-8 -*-
"""把登錄類型 vlm 併入 llm。

能看圖的模型與聊天模型是同一種。獨立的 vlm 類型會讓它不能當
對話／Router 模型。既有列改成 llm；能不能讀圖不再由類型表示。

降版不做還原。轉換後無法分辨哪些 llm 原先是 vlm、哪些本來就是
llm，把 llm 改回 vlm 會誤傷對話模型。

Revision ID: r1_0053
Revises: r1_0052
Create Date: 2026-09-27
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "r1_0053"
down_revision: Union[str, None] = "r1_0052"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_CONVERT = sa.text(
    "UPDATE model_registry SET model_type = 'llm' WHERE model_type = 'vlm'"
)


def convert_vlm_rows(connection) -> None:
    """把 vlm 列改成 llm。再跑一次不會動到其他類型。"""
    connection.execute(_CONVERT)


def upgrade() -> None:
    convert_vlm_rows(op.get_bind())


def downgrade() -> None:
    """不還原。無法分辨哪些 llm 原先是 vlm。"""
    return None
