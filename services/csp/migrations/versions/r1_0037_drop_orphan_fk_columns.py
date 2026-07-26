# -*- coding: utf-8 -*-
"""Drop three orphan FK columns the ORM never declared.

Revision ID: r1_0037
Revises: r1_0036
Create Date: 2026-07-26

為什麼有這支
------------
把 drift gate 的 FK 檢查從「數量比對」改成「約束形狀比對」之後,冒出三條
`UNDECLARED_FK` —— **DB 有 FK 但 ORM 完全不知道那個欄**:

    agents.last_reviewer_id      → users.id
    audit_logs.actor_id          → users.id           (ORM 只有 actor_user_id)
    users.auth_provider_id       → auth_providers.id

舊的計數寫法永遠看不到這個方向的漂移(它只比數字大小)。

三條都是**孤兒欄**,不是漏寫 `ForeignKey`:
- 程式碼引用 = 0。`AuditLog.actor_id` 的引用數是 0;全 repo 那 74 個 `actor_id`
  屬於 `policy_decisions.actor_id`(不同表,而且那個欄刻意沒有 FK,因為它依
  `actor_type` 指向 users / service client / agent)。
- dev 實庫三欄非空列數皆 0,其中 `audit_logs` **30,990 列全部是 NULL** ——
  這個欄從來沒被寫過。
- `.15`(內網平台主機)也是新建、尚未正式開放使用,同樣沒有生產資料風險。

`audit_logs.actor_id` 特別值得記:它與 ORM 的 `actor_user_id` 並存,是「同一件事
兩個欄」的典型 —— 稽核查詢若挑錯欄就會查到一個永遠是 NULL 的欄,然後得出
「沒有人做過這件事」。留著它比刪掉危險。

刪除後 drift gate 的 `UNDECLARED_FK` 上限可從 3 降到 0,那一類就不再需要豁免。

downgrade
---------
把三個欄與 FK 加回來,但**資料不會回來**(本來就全是 NULL,所以實際上無損)。
`auth_providers` 表若不存在則跳過該欄的 FK —— 見 upgrade 內的說明。
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "r1_0037"
down_revision: Union[str, None] = "r1_0036"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# (table, column, referred_table, referred_column)
_ORPHANS = (
    ("agents", "last_reviewer_id", "users", "id"),
    ("audit_logs", "actor_id", "users", "id"),
    ("users", "auth_provider_id", "auth_providers", "id"),
)


def _existing_columns(bind, table: str) -> set[str]:
    insp = sa.inspect(bind)
    if table not in insp.get_table_names():
        return set()
    return {c["name"] for c in insp.get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()

    # 冪等 + fail-safe:欄位可能已經不在(不同部署的歷史不同),也可能有人在
    # 這支 migration 之後才開始使用它。所以先確認「還在,而且全是 NULL」才刪。
    for table, column, _ref_t, _ref_c in _ORPHANS:
        if column not in _existing_columns(bind, table):
            continue

        # ⚠ 這個檢查是刻意的:如果某個部署其實在用這個欄,寧可 migration 失敗
        # 並要求人來看,也不要靜默刪掉別人的資料。判斷依據是「非 NULL 列數」而
        # 不是「表有沒有資料」—— audit_logs 有三萬列,但這個欄全是 NULL。
        non_null = bind.execute(
            sa.text(f'SELECT count("{column}") FROM "{table}"')  # noqa: S608 — 欄名來自本檔常數
        ).scalar_one()
        if non_null:
            raise RuntimeError(
                f"{table}.{column} 有 {non_null} 列非 NULL —— 這與「孤兒欄」的前提"
                f"不符。請先查清楚是誰在寫它(可能是 repo 外的 SQL/BI/工作流),"
                f"再決定要刪除還是補進 ORM 模型。本 migration 刻意不繼續。"
            )

        op.drop_column(table, column)


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    tables = set(insp.get_table_names())

    for table, column, ref_t, ref_c in _ORPHANS:
        if table not in tables or column in _existing_columns(bind, table):
            continue
        op.add_column(table, sa.Column(column, sa.Integer(), nullable=True))
        # 被參照表可能不存在(auth_providers 在某些較舊的部署沒有建),那就只
        # 還原欄位、不還原 FK —— 降版是 DR/測試用途,不值得為此整支失敗。
        if ref_t in tables:
            op.create_foreign_key(
                f"{table}_{column}_fkey", table, ref_t, [column], [ref_c]
            )
