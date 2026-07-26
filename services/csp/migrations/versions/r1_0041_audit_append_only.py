# -*- coding: utf-8 -*-
"""Make the three audit ledgers actually append-only.

Revision ID: r1_0041
Revises: r1_0040
Create Date: 2026-07-27

為什麼有這支
------------
`audit_logs` / `policy_decisions` / `classification_events` 的「append-only」在改動
前**只是註解**。實際的權限是 `0014:129` 的

    GRANT ALL ON ALL TABLES IN SCHEMA public TO csp_app

外加 `ALTER DEFAULT PRIVILEGES ... GRANT ALL`,而三張表**零 trigger**。也就是說
runtime 用的 `csp_app` role 可以 UPDATE 或 DELETE 任何一列稽核紀錄。

2026-06-02 的稽核把這條寫得很清楚:

> an attacker (or admin) with DB write access can silently delete or edit audit
> rows including the `access_classified_conversation` and `classify_conversation`
> events, defeating the whole point of the classified-access trail.

而 W1-1 剛剛才讓「讀取營業秘密對話」開始落稽核列 —— 如果那些列可以被靜默改掉,
那項工作的價值是零。稽核的意義完全建立在「事後改不動」上。

## 一個必須精確處理的例外

硬刪使用者時要把稽核列的 actor 參照設成 NULL 以**保留歷史**(不能連稽核列一起刪):

- `audit_logs.actor_user_id` 的 FK **沒有** `ondelete`,所以由應用層做
  (`api/users.py` 的 manual cleanup #2)。
- `classification_events.actor_user_id` 的 FK 是 `ondelete="SET NULL"`,由 PG 的
  referential action 做 —— 但 **BEFORE UPDATE trigger 對 referential action 也會
  觸發**,所以 trigger 一樣要放行。
- `policy_decisions.actor_id` 沒有 FK,不需要這個例外。

所以「append-only」在這裡的精確定義是:**內容不可變**。把一個指向已刪除使用者的
外鍵設成 NULL 不是內容變動,是參照完整性維護。trigger 用
`to_jsonb(NEW) - 'actor_user_id' = to_jsonb(OLD) - 'actor_user_id'` 判斷「除了那
一欄之外完全相同」—— 這個寫法與欄位清單無關,以後加欄也不用改 trigger。

## 兩層防護

1. **REVOKE**:三表都收回 `DELETE` 與 `TRUNCATE`;`policy_decisions` 與
   `classification_events` 連 `UPDATE` 一起收(前者無例外需求,後者的例外由
   referential action 執行、不吃 role 權限)。
2. **trigger**:即使有人日後重新 GRANT(或用 superuser 連線),trigger 仍然擋。
   REVOKE 是門鎖,trigger 是門本身。

⚠ `csp_app` 之外的 role 不受 REVOKE 影響 —— **superuser 永遠繞得過權限**,這是
PG 的設計。所以 trigger 那一層才是真正的不變式;它對任何 role 都成立(除了
`session_replication_role = replica`,那是還原備份時才會用的模式)。
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "r1_0041"
down_revision: Union[str, None] = "r1_0040"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# (表, 允許 actor 欄轉 NULL 的例外欄位或 None)
_LEDGERS = (
    ("audit_logs", "actor_user_id"),
    ("classification_events", "actor_user_id"),
    ("policy_decisions", None),
)

_APP_ROLE = "csp_app"


def _reject_delete_fn(table: str) -> str:
    return f"""
CREATE OR REPLACE FUNCTION anila_{table}_no_delete() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION
        '{table} 是 append-only 稽核帳:不得 DELETE(id=%)。'
        '稽核紀錄的價值完全建立在事後改不動;要處理保留期請走 retention 流程。',
        OLD.id
        USING ERRCODE = 'raise_exception';
END;
$$ LANGUAGE plpgsql;
"""


def _reject_update_fn(table: str, actor_col: str | None) -> str:
    if actor_col is None:
        body = f"""
    RAISE EXCEPTION
        '{table} 是 append-only 稽核帳:不得 UPDATE(id=%)。'
        '需要更正就補一列新紀錄,不要改舊列。',
        OLD.id
        USING ERRCODE = 'raise_exception';
"""
    else:
        body = f"""
    -- 唯一允許的變動:把指向已刪除使用者的外鍵設成 NULL 以保留歷史。
    -- `to_jsonb(row) - 'col'` 移除該鍵後比較,所以「除了那一欄之外完全相同」
    -- 這個判斷與欄位清單無關 —— 以後加欄也不用改這支 trigger。
    IF NEW.{actor_col} IS NULL
       AND OLD.{actor_col} IS NOT NULL
       AND (to_jsonb(NEW) - '{actor_col}') = (to_jsonb(OLD) - '{actor_col}') THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION
        '{table} 是 append-only 稽核帳:不得 UPDATE(id=%)。'
        '唯一例外是硬刪使用者時把 {actor_col} 設為 NULL(保留歷史),'
        '而這次的變動不只有那一欄。需要更正就補一列新紀錄。',
        OLD.id
        USING ERRCODE = 'raise_exception';
"""
    return f"""
CREATE OR REPLACE FUNCTION anila_{table}_no_update() RETURNS trigger AS $$
BEGIN
{body}END;
$$ LANGUAGE plpgsql;
"""


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        # SQLite 的測試迴路沒有 role 也沒有 plpgsql。這支是 PG-only 的不變式,
        # 對應的測試也標了需要真 PG(見 tests/test_audit_append_only_pg.py)。
        return

    insp = sa.inspect(bind)
    existing = set(insp.get_table_names())

    for table, actor_col in _LEDGERS:
        if table not in existing:
            continue

        op.execute(sa.text(_reject_delete_fn(table)))
        op.execute(sa.text(_reject_update_fn(table, actor_col)))
        # 冪等:重跑時先丟舊 trigger
        op.execute(sa.text(f'DROP TRIGGER IF EXISTS anila_{table}_no_delete ON "{table}"'))
        op.execute(
            sa.text(
                f'CREATE TRIGGER anila_{table}_no_delete BEFORE DELETE ON "{table}" '
                f"FOR EACH ROW EXECUTE FUNCTION anila_{table}_no_delete()"
            )
        )
        op.execute(sa.text(f'DROP TRIGGER IF EXISTS anila_{table}_no_update ON "{table}"'))
        op.execute(
            sa.text(
                f'CREATE TRIGGER anila_{table}_no_update BEFORE UPDATE ON "{table}" '
                f"FOR EACH ROW EXECUTE FUNCTION anila_{table}_no_update()"
            )
        )

        # REVOKE 只在該 role 真的存在時做 —— 乾淨測試庫可能沒有 csp_app。
        revokes = ["DELETE", "TRUNCATE"] + ([] if actor_col else ["UPDATE"])
        if actor_col == "actor_user_id" and table == "classification_events":
            # 這張表的例外由 referential action 執行,不吃 role 權限 → UPDATE 可收。
            revokes.append("UPDATE")
        op.execute(
            sa.text(
                "DO $$ BEGIN "
                f"IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{_APP_ROLE}') THEN "
                f'REVOKE {", ".join(revokes)} ON "{table}" FROM {_APP_ROLE}; '
                "END IF; END $$;"
            )
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    for table, actor_col in _LEDGERS:
        op.execute(sa.text(f'DROP TRIGGER IF EXISTS anila_{table}_no_update ON "{table}"'))
        op.execute(sa.text(f'DROP TRIGGER IF EXISTS anila_{table}_no_delete ON "{table}"'))
        op.execute(sa.text(f"DROP FUNCTION IF EXISTS anila_{table}_no_update()"))
        op.execute(sa.text(f"DROP FUNCTION IF EXISTS anila_{table}_no_delete()"))
        revokes = ["DELETE", "TRUNCATE", "UPDATE"]
        op.execute(
            sa.text(
                "DO $$ BEGIN "
                f"IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{_APP_ROLE}') THEN "
                f'GRANT {", ".join(revokes)} ON "{table}" TO {_APP_ROLE}; '
                "END IF; END $$;"
            )
        )
