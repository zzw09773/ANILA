# -*- coding: utf-8 -*-
"""P1.3 — unit_admin_assignments（單位管理員綁定表）.

SYSTEM-MAP 定義單位管理員綁在部門樹節點、權限涵蓋子樹；指派由本組
admin 執行（外單位來文）。本 migration 只建 binding 表——``users.role``
與全域角色系統不動。

額度分配（SYSTEM-MAP「調整自己單位內的額度分配」）刻意遞延至
credit-ledger epic，不在本包、本表亦不承載額度欄位。

每節點最多 3 名 active 管理員由 service 層在 ``acquire_dept_tree_lock``
下強制；不做 DB CHECK（無先例）。

Idempotency
===========

Upgrade 用 raw ``CREATE TABLE IF NOT EXISTS`` / ``CREATE INDEX IF NOT
EXISTS``，容忍 ``create_all`` 已建表的 DB。Downgrade 同樣 ``IF EXISTS``。

SQLite note: partial unique index 的 predicate 在 Postgres 路徑發出；
測試環境由 SQLAlchemy ``create_all`` / model ``sqlite_where`` 對齊。

Revision ID: r1_0010
Revises: r1_0009
"""

from typing import Sequence, Union

from alembic import op

revision: str = "r1_0010"
down_revision: Union[str, None] = "r1_0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS unit_admin_assignments (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL,
            department_id INTEGER NOT NULL,
            granted_by INTEGER,
            granted_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
            revoked_at TIMESTAMP WITHOUT TIME ZONE,
            CONSTRAINT fk_unit_admin_assignments_user
              FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
            CONSTRAINT fk_unit_admin_assignments_department
              FOREIGN KEY (department_id) REFERENCES departments(id)
              ON DELETE CASCADE,
            CONSTRAINT fk_unit_admin_assignments_granted_by
              FOREIGN KEY (granted_by) REFERENCES users(id) ON DELETE SET NULL
        )
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS ix_unit_admin_assignments_active_pair
          ON unit_admin_assignments (user_id, department_id)
          WHERE revoked_at IS NULL
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP INDEX IF EXISTS ix_unit_admin_assignments_active_pair"
    )
    op.execute("DROP TABLE IF EXISTS unit_admin_assignments")
