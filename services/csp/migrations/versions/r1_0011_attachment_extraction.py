# -*- coding: utf-8 -*-
"""P1.5 — attachment text extraction + per-conversation token budget.

SYSTEM-MAP: 丟 PDF 直接叫 LLM 分析。上傳時非同步抽文字、估算 token;
extract_status 只記抽取結果（pending / ok / failed / unsupported /
too_large）。是否塞進某次對話的 context 由 runtime admit() 依當下模型
預算推導，不持久化。本 migration 只加 attachments 抽取狀態欄位,
不做切塊檢索、不做 3 日清理。

Idempotency
===========

Upgrade 用 raw ``ALTER TABLE ... ADD COLUMN IF NOT EXISTS``,容忍
``create_all`` / startup_migrations 已補欄的 DB。Downgrade 反向
``DROP COLUMN IF EXISTS``。

Revision ID: r1_0011
Revises: r1_0010
"""

from typing import Sequence, Union

from alembic import op

revision: str = "r1_0011"
down_revision: Union[str, None] = "r1_0010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE attachments "
        "ADD COLUMN IF NOT EXISTS extracted_text TEXT NULL"
    )
    op.execute(
        "ALTER TABLE attachments "
        "ADD COLUMN IF NOT EXISTS token_count INTEGER NULL"
    )
    op.execute(
        "ALTER TABLE attachments "
        "ADD COLUMN IF NOT EXISTS extract_status VARCHAR(20) "
        "NOT NULL DEFAULT 'pending'"
    )
    op.execute(
        "ALTER TABLE attachments "
        "ADD COLUMN IF NOT EXISTS extract_error VARCHAR(500) NULL"
    )
    op.execute(
        "ALTER TABLE attachments "
        "ADD COLUMN IF NOT EXISTS extracted_at TIMESTAMP NULL"
    )
    # page_count：parser metadata 的頁數，僅供 prompt 標籤「N 頁」使用
    # （非預算欄位）。設計注入格式需要，故一併持久化以免聊天時重解析。
    op.execute(
        "ALTER TABLE attachments "
        "ADD COLUMN IF NOT EXISTS page_count INTEGER NULL"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE attachments DROP COLUMN IF EXISTS page_count"
    )
    op.execute(
        "ALTER TABLE attachments DROP COLUMN IF EXISTS extracted_at"
    )
    op.execute(
        "ALTER TABLE attachments DROP COLUMN IF EXISTS extract_error"
    )
    op.execute(
        "ALTER TABLE attachments DROP COLUMN IF EXISTS extract_status"
    )
    op.execute(
        "ALTER TABLE attachments DROP COLUMN IF EXISTS token_count"
    )
    op.execute(
        "ALTER TABLE attachments DROP COLUMN IF EXISTS extracted_text"
    )
