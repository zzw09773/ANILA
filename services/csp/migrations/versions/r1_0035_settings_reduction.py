# -*- coding: utf-8 -*-
"""收斂平台設定表為十二個即時生效的 C 類設定。

部署環境、祕密與程式常數不再由 ``platform_settings`` 代理；資料表保留
十二個仍由管理頁調整的 key。這是資料清理，不會清空整張表，也不會影響
保留下來的列。

Revision ID: r1_0035
Revises: r1_0034
Create Date: 2026-08-11
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "r1_0035"
down_revision: Union[str, None] = "r1_0034"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_KEEP_KEYS = (
    "institutional_kb.score_threshold",
    "memory.retrieve_min_cosine",
    "memory.retrieve_top_k",
    "proxy.llm_timeout",
    "proxy.embedding_timeout",
    "auth.access_token_expire_minutes",
    "auth.refresh_token_expire_days",
    "limits.department_max_depth",
    "limits.action_invoke_per_min",
    "limits.attachment_budget_ratio",
    "intl.zh_normalize",
    "intl.query_expansion",
)


def upgrade() -> None:
    # The key list is code-owned and intentionally explicit.  Existing rows for
    # the twelve retained settings are left untouched, including their values
    # and audit metadata; only obsolete rows are removed.
    quoted = ", ".join(f"'{key}'" for key in _KEEP_KEYS)
    op.execute(sa.text(f"DELETE FROM platform_settings WHERE key NOT IN ({quoted})"))


def downgrade() -> None:
    # Deleted setting rows have no authoritative value to restore.  Recreating
    # them with guessed defaults would fabricate configuration state.
    pass
