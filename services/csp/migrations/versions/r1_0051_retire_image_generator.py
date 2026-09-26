# -*- coding: utf-8 -*-
"""退役 image-generator，並拿掉主圖像旗標。

清空 AUTO_REGISTER_AGENTS 只停止新增。資料庫裡已核准的 image-generator
仍會出現在派工清單。這支遷移把該列標成停用且不可用，並撤銷使用者與
API key 的指派。重複執行結果相同。

``is_image_primary`` 不再是生圖設定來源。欄位與部分唯一索引一併刪除。
降版只把欄位加回來，不把已退役的 agent 重新核准。

Revision ID: r1_0051
Revises: r1_0050
Create Date: 2026-09-26
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "r1_0051"
down_revision: Union[str, None] = "r1_0050"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_RETIRED = "retired"

_RETIRE_AGENT = sa.text(
    """
    UPDATE agents
    SET approval_status = 'disabled',
        unavailable_reason = :reason,
        health_status = 'unhealthy'
    WHERE name = 'image-generator'
    """
)
_DROP_USER_PERMS = sa.text(
    """
    DELETE FROM user_agent_permissions
    WHERE agent_id IN (
        SELECT id FROM agents WHERE name = 'image-generator'
    )
    """
)
_DROP_KEY_PERMS = sa.text(
    """
    DELETE FROM api_key_agent_permissions
    WHERE agent_id IN (
        SELECT id FROM agents WHERE name = 'image-generator'
    )
    """
)


def retire_image_generator(connection) -> None:
    """停用 image-generator 並撤銷相關權限。第二次執行不再改到其他列。"""
    connection.execute(_RETIRE_AGENT, {"reason": _RETIRED})
    connection.execute(_DROP_USER_PERMS)
    connection.execute(_DROP_KEY_PERMS)


def upgrade() -> None:
    bind = op.get_bind()
    retire_image_generator(bind)
    op.execute("DROP INDEX IF EXISTS uq_model_registry_image_primary")
    op.drop_column("model_registry", "is_image_primary")


def downgrade() -> None:
    op.add_column(
        "model_registry",
        sa.Column(
            "is_image_primary",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_model_registry_image_primary "
        "ON model_registry (is_image_primary) "
        "WHERE is_image_primary = true"
    )
