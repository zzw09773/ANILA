# -*- coding: utf-8 -*-
"""撤銷全部仍有效的長效 agent 憑證。

代理改用 5 分鐘派工 JWT。核發端點已回 410。0027 曾把同一把
CSP_SERVICE_TOKEN 寫進每個已核准 agent 的 active agent_credentials，
而自動核發只輪替 router-primary。compose 不再傳入那顆環境變數之後，
這些列仍會被當成 agent 身分。

這裡不讀環境變數。凡是還 active，或寬限期裡還留著上一把權杖的列，
一律撤銷並清掉上一把。降版救不回被撤掉的有效狀態。

Revision ID: r1_0048
Revises: r1_0047
Create Date: 2026-09-26
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "r1_0048"
down_revision: Union[str, None] = "r1_0047"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    bind.execute(
        sa.text(
            """
            UPDATE agent_credentials
            SET is_active = :inactive,
                revoked_at = COALESCE(revoked_at, :now),
                service_token_previous_envelope = NULL,
                service_token_previous_lookup_hash = NULL,
                service_token_previous_expires_at = NULL
            WHERE is_active = :active
               OR service_token_previous_lookup_hash IS NOT NULL
               OR service_token_previous_envelope IS NOT NULL
            """
        ),
        {
            "inactive": False,
            "active": True,
            "now": datetime.now(timezone.utc),
        },
    )


def downgrade() -> None:
    # 列還在，但哪些原本有效已無法還原。
    pass
