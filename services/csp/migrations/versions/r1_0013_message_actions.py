# -*- coding: utf-8 -*-
"""OW-3 — message_actions + message_action_bindings（訊息級自訂動作）.

docs/plans/ow3-message-actions-blueprint.md §3：兩表 + version/body_sha256；
可見性靠 bindings 聯集、零綁定 fail-closed；無 seed（舊 SPA 三鈕進 runbook）。

``body`` 刻意不落庫加密：威脅模型是能寫入的作者（owner），不是能讀 DB
的營運角色——完整程式碼快照已進 audit_logs；詳見
docs/security/ow3-exec-risk-acceptance.md。

Idempotency
===========

Upgrade 用 raw ``CREATE TABLE IF NOT EXISTS`` / ``CREATE INDEX IF NOT
EXISTS``，容忍 ``create_all`` 已建表的 DB（同 r1_0010）。Downgrade 同樣
``IF EXISTS``。

三個 partial unique index（每 scope_type 一個）—— PG 視 NULL 為 distinct，
單一 composite unique 無法對 role/department/user 去重（同
``ix_unit_admin_assignments_active_pair`` 的理由）。

SQLite note: partial-index predicates 僅在 Postgres 路徑發出；測試環境由
SQLAlchemy ``create_all`` / model ``sqlite_where`` 對齊（r1_0009/r1_0010
同款）。

Revision ID: r1_0013
Revises: r1_0012
"""

from typing import Sequence, Union

from alembic import op

revision: str = "r1_0013"
down_revision: Union[str, None] = "r1_0012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS message_actions (
            id SERIAL PRIMARY KEY,
            name VARCHAR(100) NOT NULL,
            label VARCHAR(120) NOT NULL,
            icon VARCHAR(40) NOT NULL,
            kind VARCHAR(20) NOT NULL,
            result_mode VARCHAR(20) NOT NULL,
            body TEXT NOT NULL,
            body_sha256 VARCHAR(64) NOT NULL,
            choices JSONB,
            notes TEXT,
            version INTEGER NOT NULL DEFAULT 1,
            is_enabled BOOLEAN NOT NULL DEFAULT TRUE,
            created_by_user_id INTEGER,
            updated_by_user_id INTEGER,
            created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
            updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
            CONSTRAINT ck_message_actions_kind
              CHECK (kind IN ('declarative', 'exec')),
            CONSTRAINT ck_message_actions_result_mode
              CHECK (result_mode IN ('to_model', 'direct')),
            CONSTRAINT fk_message_actions_created_by
              FOREIGN KEY (created_by_user_id) REFERENCES users(id)
              ON DELETE SET NULL,
            CONSTRAINT fk_message_actions_updated_by
              FOREIGN KEY (updated_by_user_id) REFERENCES users(id)
              ON DELETE SET NULL
        )
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS ix_message_actions_name
          ON message_actions (name)
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS message_action_bindings (
            id SERIAL PRIMARY KEY,
            action_id INTEGER NOT NULL,
            scope_type VARCHAR(20) NOT NULL,
            role VARCHAR(40),
            department_id INTEGER,
            user_id INTEGER,
            created_by INTEGER,
            created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
            CONSTRAINT ck_message_action_bindings_shape
              CHECK (
                (scope_type = 'role'
                  AND role IS NOT NULL
                  AND department_id IS NULL
                  AND user_id IS NULL)
                OR
                (scope_type = 'department'
                  AND department_id IS NOT NULL
                  AND role IS NULL
                  AND user_id IS NULL)
                OR
                (scope_type = 'user'
                  AND user_id IS NOT NULL
                  AND role IS NULL
                  AND department_id IS NULL)
              ),
            CONSTRAINT fk_message_action_bindings_action
              FOREIGN KEY (action_id) REFERENCES message_actions(id)
              ON DELETE CASCADE,
            CONSTRAINT fk_message_action_bindings_department
              FOREIGN KEY (department_id) REFERENCES departments(id)
              ON DELETE CASCADE,
            CONSTRAINT fk_message_action_bindings_user
              FOREIGN KEY (user_id) REFERENCES users(id)
              ON DELETE CASCADE,
            CONSTRAINT fk_message_action_bindings_created_by
              FOREIGN KEY (created_by) REFERENCES users(id)
              ON DELETE SET NULL
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_message_action_bindings_action_id
          ON message_action_bindings (action_id)
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS
          ix_message_action_bindings_role_pair
          ON message_action_bindings (action_id, role)
          WHERE scope_type = 'role'
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS
          ix_message_action_bindings_department_pair
          ON message_action_bindings (action_id, department_id)
          WHERE scope_type = 'department'
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS
          ix_message_action_bindings_user_pair
          ON message_action_bindings (action_id, user_id)
          WHERE scope_type = 'user'
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP INDEX IF EXISTS ix_message_action_bindings_user_pair"
    )
    op.execute(
        "DROP INDEX IF EXISTS ix_message_action_bindings_department_pair"
    )
    op.execute(
        "DROP INDEX IF EXISTS ix_message_action_bindings_role_pair"
    )
    op.execute(
        "DROP INDEX IF EXISTS ix_message_action_bindings_action_id"
    )
    op.execute("DROP TABLE IF EXISTS message_action_bindings")
    op.execute("DROP INDEX IF EXISTS ix_message_actions_name")
    op.execute("DROP TABLE IF EXISTS message_actions")
