# -*- coding: utf-8 -*-
"""P4.3 — named conversation shares (person XOR unit); retire anonymous token.

SYSTEM-MAP §分享 / PLAN 4.3:分享給指定的人/單位,不是匿名連結。
擁有者 2026-07-30 裁定:人與單位兩者皆可;撤銷=自此不可再讀(非召回)。

本 migration 改寫 ``conversation_shares``:
- 新增 ``target_user_id`` / ``target_department_id``(XOR CHECK)
- 刪除匿名連結欄位 ``token``、瀏覽計數 ``view_count``
- 既有匿名列無法對應具名對象 → upgrade 時清空後改 schema
  (重啟樹可拋庫;本機 create_all 路徑不依賴本檔)

Revision ID: r1_0019
Revises: r1_0018
"""

from typing import Sequence, Union

from alembic import op

revision: str = "r1_0019"
down_revision: Union[str, None] = "r1_0018"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Anonymous rows cannot be mapped to a named target — drop them.
    op.execute("DELETE FROM conversation_shares")

    op.execute(
        "ALTER TABLE conversation_shares "
        "ADD COLUMN IF NOT EXISTS target_user_id INTEGER"
    )
    op.execute(
        "ALTER TABLE conversation_shares "
        "ADD COLUMN IF NOT EXISTS target_department_id INTEGER"
    )

    # Drop anonymous-link columns if present (Postgres).
    op.execute(
        "ALTER TABLE conversation_shares DROP COLUMN IF EXISTS token"
    )
    op.execute(
        "ALTER TABLE conversation_shares DROP COLUMN IF EXISTS view_count"
    )

    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
             WHERE conname = 'fk_conversation_shares_target_user'
          ) THEN
            ALTER TABLE conversation_shares
              ADD CONSTRAINT fk_conversation_shares_target_user
              FOREIGN KEY (target_user_id) REFERENCES users(id)
              ON DELETE CASCADE;
          END IF;
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
             WHERE conname = 'fk_conversation_shares_target_department'
          ) THEN
            ALTER TABLE conversation_shares
              ADD CONSTRAINT fk_conversation_shares_target_department
              FOREIGN KEY (target_department_id) REFERENCES departments(id)
              ON DELETE CASCADE;
          END IF;
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
             WHERE conname = 'ck_conversation_shares_one_target'
          ) THEN
            ALTER TABLE conversation_shares
              ADD CONSTRAINT ck_conversation_shares_one_target
              CHECK (
                (target_user_id IS NOT NULL AND target_department_id IS NULL)
                OR (target_user_id IS NULL AND target_department_id IS NOT NULL)
              );
          END IF;
        END $$;
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_conversation_shares_target_user_id "
        "ON conversation_shares (target_user_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_conversation_shares_target_department_id "
        "ON conversation_shares (target_department_id)"
    )
    # One active share per (conversation, person) / (conversation, unit).
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS ix_conversation_shares_active_user
          ON conversation_shares (conversation_id, target_user_id)
          WHERE target_user_id IS NOT NULL
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS ix_conversation_shares_active_dept
          ON conversation_shares (conversation_id, target_department_id)
          WHERE target_department_id IS NOT NULL
        """
    )


def downgrade() -> None:
    # Downgrade restores anonymous-link shape; named targets are discarded.
    op.execute("DELETE FROM conversation_shares")
    op.execute("DROP INDEX IF EXISTS ix_conversation_shares_active_user")
    op.execute("DROP INDEX IF EXISTS ix_conversation_shares_active_dept")
    op.execute(
        "ALTER TABLE conversation_shares "
        "DROP CONSTRAINT IF EXISTS ck_conversation_shares_one_target"
    )
    op.execute(
        "ALTER TABLE conversation_shares "
        "DROP CONSTRAINT IF EXISTS fk_conversation_shares_target_user"
    )
    op.execute(
        "ALTER TABLE conversation_shares "
        "DROP CONSTRAINT IF EXISTS fk_conversation_shares_target_department"
    )
    op.execute(
        "DROP INDEX IF EXISTS ix_conversation_shares_target_user_id"
    )
    op.execute(
        "DROP INDEX IF EXISTS ix_conversation_shares_target_department_id"
    )
    op.execute(
        "ALTER TABLE conversation_shares "
        "DROP COLUMN IF EXISTS target_user_id"
    )
    op.execute(
        "ALTER TABLE conversation_shares "
        "DROP COLUMN IF EXISTS target_department_id"
    )
    op.execute(
        "ALTER TABLE conversation_shares "
        "ADD COLUMN IF NOT EXISTS token VARCHAR(64)"
    )
    op.execute(
        "ALTER TABLE conversation_shares "
        "ADD COLUMN IF NOT EXISTS view_count INTEGER "
        "DEFAULT 0 NOT NULL"
    )
