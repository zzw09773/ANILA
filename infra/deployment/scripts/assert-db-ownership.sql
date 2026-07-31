-- ============================================================================
-- assert-db-ownership.sql — 還原之後把擁有權/權限扳回平台需要的姿態（P2.7）
-- ----------------------------------------------------------------------------
-- 由 restore-csp-db.sh 在 pg_restore 之後自動執行。**操作者不需要記得任何事。**
--
-- 為什麼需要它：
--   pg_restore 已經帶回 dump 當下的擁有權，正常情況下這支腳本什麼都不用改。
--   它存在是為了兩種不正常：
--     (a) 還原的是 **r1_0027 之前**的舊 dump —— 那時稽核表還歸 csp_app 所有、
--         沒有 append-only 觸發器。開機時 alembic 會把 r1_0027 補跑起來，
--         所以這支腳本只要不把事情弄得更糟即可。
--     (b) 有人用 `--no-owner` 之類的參數手動還原過 —— 擁有權地圖被壓平。
--
-- 兩條不變式（順序重要）：
--   1. 平台開機時會 ALTER / CREATE INDEX 的表必須歸 **csp_app** 所有，
--      否則開機那段 DDL 會 `must be owner of table X`，而例外被吞掉 →
--      容器顯示 healthy 但平台其實壞掉。
--   2. 稽核家族必須**不**歸 csp_app 所有，且 csp_app 只有讀（加上事件表的
--      INSERT）。否則 P2.7 的防竄改在第一次還原就蒸發。
--
-- 冪等：在一個剛跑完 alembic 的正常資料庫上執行，什麼都不會變。
-- 這件事有測試在守：services/csp/tests/test_audit_ledger_pg.py
--   ::test_restore_ownership_sql_is_a_noop_on_a_migrated_database
-- ============================================================================

-- 純 SQL，沒有 psql meta-command：restore-csp-db.sh 用 `-v ON_ERROR_STOP=1`
-- 餵它，而測試用一般連線直接執行同一份檔案 —— 兩邊跑的必須是同一段字。
--
-- ── 不變式 1：開機會動 DDL 的表歸 csp_app ────────────────────────────────
-- 清單來自 app/services/startup_migrations.py 的 _ensure_schema_backfills，
-- 只列它真的會 ALTER / CREATE INDEX 的表。刻意不是「public 底下全部」——
-- 沒必要的擁有權就是沒必要的權限。
DO $$
DECLARE
    r RECORD;
    boot_ddl_tables TEXT[] := ARRAY[
        'users', 'model_registry', 'token_usage', 'departments',
        'api_keys', 'alerts', 'platform_links', 'attachments'
    ];
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'csp_app') THEN
        RAISE NOTICE 'csp_app 不存在，略過擁有權校正';
        RETURN;
    END IF;
    FOR r IN
        SELECT c.relname AS tbl
          FROM pg_class c
          JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE n.nspname = 'public'
           AND c.relkind = 'r'
           AND c.relname = ANY (boot_ddl_tables)
           AND pg_get_userbyid(c.relowner) <> 'csp_app'
    LOOP
        RAISE NOTICE '校正擁有權：% → csp_app（開機 DDL 需要）', r.tbl;
        -- ALTER TABLE ... OWNER TO 會一併換掉該表 owned sequence 的 owner，
        -- 所以不要自己再 ALTER SEQUENCE：那在 owner 已經一致時會直接報
        -- "cannot change owner of sequence ... linked to table"。
        EXECUTE format('ALTER TABLE public.%I OWNER TO csp_app', r.tbl);
    END LOOP;
END $$;

-- ── 不變式 2：稽核家族不歸 runtime role，且只讀 ──────────────────────────
-- 注意順序：這一段在後面，所以就算 audit_logs 意外被上面碰到，也會被扳回來。
DO $$
DECLARE
    r RECORD;
    seq TEXT;
    audit_tables TEXT[] := ARRAY[
        'audit_logs', 'policy_decisions', 'classification_events',
        'audit_checkpoints'
    ];
BEGIN
    FOR r IN
        SELECT c.relname AS tbl
          FROM pg_class c
          JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE n.nspname = 'public'
           AND c.relkind = 'r'
           AND c.relname = ANY (audit_tables)
    LOOP
        IF pg_get_userbyid(
               (SELECT relowner FROM pg_class WHERE oid = (
                   'public.' || quote_ident(r.tbl))::regclass)) = 'csp_app' THEN
            RAISE NOTICE '校正擁有權：% 由 csp_app 收回給 %（P2.7）',
                         r.tbl, CURRENT_USER;
            -- owned sequence 的 owner 會跟著一起換，不要另外 ALTER SEQUENCE。
            EXECUTE format('ALTER TABLE public.%I OWNER TO CURRENT_USER', r.tbl);
        END IF;
        seq := pg_get_serial_sequence('public.' || r.tbl, 'id');

        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'csp_app') THEN
            EXECUTE format('REVOKE ALL ON TABLE public.%I FROM csp_app', r.tbl);
            IF r.tbl = 'audit_checkpoints' THEN
                -- 只讀。有 INSERT 就能塞未來日期的檢查點把每日封存卡死。
                EXECUTE format('GRANT SELECT ON TABLE public.%I TO csp_app',
                               r.tbl);
            ELSE
                EXECUTE format(
                    'GRANT SELECT, INSERT ON TABLE public.%I TO csp_app', r.tbl);
                IF seq IS NOT NULL THEN
                    EXECUTE format(
                        'GRANT USAGE, SELECT ON SEQUENCE %s TO csp_app', seq);
                END IF;
            END IF;
        END IF;
    END LOOP;
END $$;

-- ── 最後大聲報一次結果，讓凌晨兩點的人不必自己查 ──────────────────────
SELECT c.relname AS "表",
       pg_get_userbyid(c.relowner) AS "owner",
       has_table_privilege('csp_app', c.oid, 'SELECT') AS "csp_app 讀",
       has_table_privilege('csp_app', c.oid, 'INSERT') AS "csp_app 寫",
       has_table_privilege('csp_app', c.oid, 'UPDATE') AS "csp_app 改",
       has_table_privilege('csp_app', c.oid, 'DELETE') AS "csp_app 刪"
  FROM pg_class c
  JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE n.nspname = 'public'
   AND c.relname IN ('audit_logs', 'policy_decisions',
                     'classification_events', 'audit_checkpoints',
                     'users', 'token_usage')
 ORDER BY 1;
