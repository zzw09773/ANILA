# -*- coding: utf-8 -*-
"""P2.7 稽核帳防竄改:收回擁有權 + append-only 觸發器 + 每日檢查點。

Revision ID: r1_0027
Revises: r1_0025
Create Date: 2026-07-31

背景:``0014_add_ingestion_platform`` 把 public schema 全部表的 owner 轉給了
runtime role ``csp_app``(理由是開機時的 ``ALTER TABLE`` 需要 ownership)。
Postgres 裡 owner 隱含全部權限,而且 owner 可以 ``DROP TRIGGER`` —— 所以在
那個姿態下,任何拿到 runtime 憑證的人都能安靜地改寫或刪除稽核歷史。

本 migration 只對**稽核家族四張表**收回擁有權,其他表一概不動(爆炸半徑最小):

1. 把 ``startup_migrations`` 對 ``audit_logs`` 做的 DDL 收編進 alembic ——
   稽核表的 schema 從此只由 migration 身分管。開機路徑那一段改用第二支
   migration engine 執行(見 ``app/services/startup_migrations.py``),
   舊部署照樣自我修復,新部署走這裡。
2. 建 ``audit_checkpoints``(日級雜湊鏈)。
3. 拆掉稽核表上的 FK。FK 的 ``ON DELETE SET NULL`` 會由 DB 自己對稽核表
   發 UPDATE,那既會被 append-only 觸發器擋下(刪使用者/task 會炸),也正好
   是威脅模型裡最想要的功能:**刪掉帳號就洗掉自己在稽核帳上的身分**
   (``app/api/users.py`` 原本手動做這件事)。欄位與值保留,歸屬不再被洗掉。
4. 換主人 + 收權:owner → migration role;``csp_app`` 只剩 SELECT / INSERT。
5. append-only 觸發器:UPDATE / TRUNCATE 一律 RAISE;DELETE 只放行超過
   保留期(180 天,SYSTEM-MAP §8「留半年」)的列。

**誠實標注**:owner 與 superuser 永遠繞得過觸發器。這一層防的是「持 runtime
憑證的人」;對持有主機的人只能靠日級雜湊鏈 + 匯出檔外部錨點**事後查得出來**,
不是防得住。詳見 ``app/services/audit_ledger.py``。
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "r1_0027"
# r1_0026(部門名稱改為同層唯一)已於 2026-07-31 落地,合併時接上。
# 兩者無資料相依,順序只是鏈的先後。
down_revision: Union[str, None] = "r1_0026"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# 受保護集合。這是**歷史事實**,凍結在此 revision;runtime 那份在
# ``app/services/audit_ledger.AUDIT_LEDGER_TABLES``。加新的稽核表要寫新的
# migration,不是回頭改這裡。
_EVENT_TABLES = ("audit_logs", "policy_decisions", "classification_events")
_PROTECTED_TABLES = _EVENT_TABLES + ("audit_checkpoints",)
_RETENTION_DAYS = 180


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    # ── 1. audit_logs 的 schema 收編(原本在 startup_migrations) ──────────
    if _is_postgres():
        for col, ddl in (
            ("status", "VARCHAR(20) NOT NULL DEFAULT 'ok'"),
            ("actor_user_id", "INTEGER NULL"),
            ("actor_username", "VARCHAR(100) NULL"),
            ("ip_address", "VARCHAR(64) NULL"),
            ("metadata_json", "TEXT NULL"),
        ):
            op.execute(
                f"ALTER TABLE audit_logs ADD COLUMN IF NOT EXISTS {col} {ddl}"
            )
        # 0001 baseline 把 resource_id 宣告成 INTEGER,model 是 VARCHAR(100)。
        op.execute(
            "ALTER TABLE audit_logs ALTER COLUMN resource_id "
            "TYPE VARCHAR(100) USING resource_id::VARCHAR(100)"
        )

    # ── 2. 檢查點表 ────────────────────────────────────────────────────────
    op.create_table(
        "audit_checkpoints",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column(
            "digest_version", sa.SmallInteger(), nullable=False, server_default="1"
        ),
        sa.Column("day_digest", sa.String(length=64), nullable=False),
        sa.Column("prev_hash", sa.String(length=64), nullable=False),
        sa.Column("chain_head", sa.String(length=64), nullable=False),
        sa.Column(
            "row_counts",
            sa.JSON().with_variant(postgresql.JSONB, "postgresql"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_audit_checkpoints_day", "audit_checkpoints", ["day"], unique=True
    )

    if not _is_postgres():
        # SQLite(單元測試)沒有 role / trigger 語意可言;schema 到此為止。
        return

    # ── 3. 拆掉稽核表上的 FK(見檔頭 §3) ───────────────────────────────────
    op.execute(
        """
        DO $$
        DECLARE r RECORD;
        BEGIN
            FOR r IN
                SELECT conrelid::regclass::text AS tbl, conname
                  FROM pg_constraint
                 WHERE contype = 'f'
                   AND conrelid IN (
                        'audit_logs'::regclass,
                        'policy_decisions'::regclass,
                        'classification_events'::regclass)
            LOOP
                EXECUTE format('ALTER TABLE %s DROP CONSTRAINT %I',
                               r.tbl, r.conname);
            END LOOP;
        END $$
        """
    )

    # ── 4. 換主人 + 收權 ──────────────────────────────────────────────────
    tables_sql = ", ".join(f"'{t}'" for t in _PROTECTED_TABLES)
    op.execute(
        f"""
        DO $$
        DECLARE
            r RECORD;
            seq TEXT;
        BEGIN
            FOR r IN
                SELECT c.relname AS tbl
                  FROM pg_class c
                  JOIN pg_namespace n ON n.oid = c.relnamespace
                 WHERE n.nspname = 'public' AND c.relname IN ({tables_sql})
            LOOP
                EXECUTE format('ALTER TABLE public.%I OWNER TO CURRENT_USER',
                               r.tbl);
                seq := pg_get_serial_sequence('public.' || r.tbl, 'id');
                IF seq IS NOT NULL THEN
                    EXECUTE format('ALTER SEQUENCE %s OWNER TO CURRENT_USER', seq);
                END IF;
                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'csp_app') THEN
                    EXECUTE format('REVOKE ALL ON TABLE public.%I FROM csp_app',
                                   r.tbl);
                    IF r.tbl = 'audit_checkpoints' THEN
                        -- 檢查點表:runtime role 只讀,連 INSERT 都不給。
                        -- 摘要沒有祕密,有 SELECT 就算得出自洽的鏈頭;若還給
                        -- INSERT,拿到 runtime 憑證的人可以塞一列日期在未來的
                        -- 檢查點,封存游標從此跳過今天 —— 每日封存永久停擺,
                        -- 而且一行日誌都不會寫。封存改由 migration 身分執行
                        -- (app/services/audit_ledger._ledger_write_session),
                        -- 所以這裡不需要、也不可以給 INSERT。
                        EXECUTE format(
                            'GRANT SELECT ON TABLE public.%I TO csp_app', r.tbl);
                    ELSE
                        EXECUTE format(
                            'GRANT SELECT, INSERT ON TABLE public.%I TO csp_app',
                            r.tbl);
                    END IF;
                    IF seq IS NOT NULL THEN
                        EXECUTE format('REVOKE ALL ON SEQUENCE %s FROM csp_app',
                                       seq);
                        IF r.tbl <> 'audit_checkpoints' THEN
                            EXECUTE format(
                                'GRANT USAGE, SELECT ON SEQUENCE %s TO csp_app',
                                seq);
                        END IF;
                    END IF;
                END IF;
            END LOOP;
        END $$
        """
    )

    # ── 5. append-only 觸發器 ─────────────────────────────────────────────
    op.execute(
        """
        CREATE OR REPLACE FUNCTION audit_ledger_append_only()
        RETURNS trigger AS $fn$
        BEGIN
            RAISE EXCEPTION
                '稽核帳為 append-only:% 不允許 % (P2.7)',
                TG_TABLE_NAME, TG_OP
                USING ERRCODE = '42501';
        END
        $fn$ LANGUAGE plpgsql
        """
    )
    # ⚠ 保留期(SYSTEM-MAP §8「留半年」)目前**沒有執行者**:``csp_app`` 沒有
    # DELETE,平台裡也沒有任何清除工作。所以這個觸發器現在的作用是「把唯一
    # 合法的刪除形狀寫死成文件」,而不是在執行政策 —— 稽核列實際上會一直累積。
    # 這是刻意的:十萬級列數的 Postgres 無感,而一條半生不熟的刪除路徑貼在
    # 稽核帳旁邊,比資料長大危險得多。真要做清除時,先讀 audit_ledger 裡
    # ``purged_days`` 的處理,再決定 genesis 要不要前移。
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION audit_ledger_retention_delete()
        RETURNS trigger AS $fn$
        BEGIN
            IF OLD.created_at IS NULL
               OR OLD.created_at >= now() - interval '{_RETENTION_DAYS} days' THEN
                RAISE EXCEPTION
                    '稽核帳為 append-only:% 只有超過保留期({_RETENTION_DAYS} 天)'
                    '的列可以刪除 (P2.7)', TG_TABLE_NAME
                    USING ERRCODE = '42501';
            END IF;
            RETURN OLD;
        END
        $fn$ LANGUAGE plpgsql
        """
    )
    for table in _PROTECTED_TABLES:
        op.execute(
            f"CREATE TRIGGER {table}_no_update BEFORE UPDATE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION audit_ledger_append_only()"
        )
        op.execute(
            f"CREATE TRIGGER {table}_no_truncate BEFORE TRUNCATE ON {table} "
            "FOR EACH STATEMENT EXECUTE FUNCTION audit_ledger_append_only()"
        )
        if table == "audit_checkpoints":
            # 檢查點是驗證舊匯出檔的唯一依據,不隨保留期清除(一年 365 列)。
            op.execute(
                f"CREATE TRIGGER {table}_no_delete BEFORE DELETE ON {table} "
                "FOR EACH ROW EXECUTE FUNCTION audit_ledger_append_only()"
            )
        else:
            op.execute(
                f"CREATE TRIGGER {table}_retention_delete BEFORE DELETE ON {table} "
                "FOR EACH ROW EXECUTE FUNCTION audit_ledger_retention_delete()"
            )


def downgrade() -> None:
    if _is_postgres():
        for table in _PROTECTED_TABLES:
            for suffix in (
                "no_update", "no_truncate", "no_delete", "retention_delete",
            ):
                op.execute(f"DROP TRIGGER IF EXISTS {table}_{suffix} ON {table}")
        op.execute("DROP FUNCTION IF EXISTS audit_ledger_append_only()")
        op.execute("DROP FUNCTION IF EXISTS audit_ledger_retention_delete()")
        # 把擁有權與權限還給 csp_app(回到 0014 的姿態)。FK 不重建 ——
        # 重建會讓硬刪帳號再度洗掉稽核歸屬,那是本 migration 要修的缺陷。
        op.execute(
            """
            DO $$
            DECLARE
                r RECORD;
                seq TEXT;
            BEGIN
                IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'csp_app')
                THEN RETURN; END IF;
                FOR r IN
                    SELECT c.relname AS tbl
                      FROM pg_class c
                      JOIN pg_namespace n ON n.oid = c.relnamespace
                     WHERE n.nspname = 'public'
                       AND c.relname IN ('audit_logs', 'policy_decisions',
                                         'classification_events',
                                         'audit_checkpoints')
                LOOP
                    EXECUTE format('ALTER TABLE public.%I OWNER TO csp_app',
                                   r.tbl);
                    EXECUTE format('GRANT ALL ON TABLE public.%I TO csp_app',
                                   r.tbl);
                    seq := pg_get_serial_sequence('public.' || r.tbl, 'id');
                    IF seq IS NOT NULL THEN
                        EXECUTE format('ALTER SEQUENCE %s OWNER TO csp_app', seq);
                        EXECUTE format('GRANT ALL ON SEQUENCE %s TO csp_app', seq);
                    END IF;
                END LOOP;
            END $$
            """
        )

    op.drop_index("ix_audit_checkpoints_day", table_name="audit_checkpoints")
    op.drop_table("audit_checkpoints")
