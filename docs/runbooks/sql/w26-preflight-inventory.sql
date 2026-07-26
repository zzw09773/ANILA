-- W2-6 / drift gate 部署前盤點 —— **純唯讀,零寫入**。
--
-- 為什麼需要在 .15 上跑這支
-- --------------------------
-- 兩件事只有 .15 的真實資料能回答,而開發機與 dev 庫都答不了(dev 庫太新太乾淨):
--
-- ① r1_0036 會在建 `UNIQUE(alerts.fingerprint)` 之前先處理空指紋與重複列。
--    dev 庫實測「空指紋 0、重複群組 0」→ 不需要去重。但 .15 更老、跑更久,
--    可能有一批空指紋列;那條 UPDATE 的鎖時間只在合成資料上驗過。
--
-- ② drift gate 新抓到三個「DB 有、ORM 完全不知道」的孤兒欄。程式碼零引用
--    (已驗:`AuditLog.actor_id` 引用數 = 0;那 74 個 actor_id 是
--    `policy_decisions` 的欄,不同表),dev 庫三欄非空列數皆 0。
--    要刪它們就必須確認 .15 也是 0 —— 刪掉有資料的欄是不可逆的資料遺失。
--
-- 怎麼跑(在 .15 平台主機上):
--   docker exec -i <csp-db 容器> psql -U csp -d csp -f - < w26-preflight-inventory.sql
--   或
--   docker exec -it <csp-db 容器> psql -U csp -d csp
--   然後貼上本檔內容。
--
-- 把輸出貼回來即可,不需要做任何判斷。

\echo '=== ① alerts 的指紋狀態(決定 r1_0036 的去重成本)==='

SELECT
    count(*)                                              AS 總列數,
    count(*) FILTER (WHERE fingerprint IS NULL)           AS 指紋為_null,
    count(*) FILTER (WHERE fingerprint = '')              AS 指紋為空字串,
    pg_size_pretty(pg_total_relation_size('alerts'))       AS 表大小
FROM alerts;

-- 真正的重複群組:同一個「非空」指紋出現在多列。這才是建 UNIQUE 會炸的原因;
-- 空字串那些不算重複(它們是 startup DDL 的哨兵值,migration 會各自改成
-- legacy:<id> 而**不刪列**,因為那些是彼此不同的歷史告警)。
\echo '--- 非空指紋的重複群組(每一列 = 一個會擋住 UNIQUE 的群組)---'
SELECT fingerprint, count(*) AS 列數
FROM alerts
WHERE fingerprint IS NOT NULL AND fingerprint <> ''
GROUP BY fingerprint
HAVING count(*) > 1
ORDER BY count(*) DESC
LIMIT 20;

\echo '--- alerts 上現有的非主鍵 unique 約束/索引(確認 UNIQUE 真的還沒建)---'
SELECT conname, contype, pg_get_constraintdef(oid) AS 定義
FROM pg_constraint
WHERE conrelid = 'alerts'::regclass AND contype IN ('u', 'p', 'f')
ORDER BY contype, conname;

\echo '--- 孤兒 acknowledged_by_user_id(指向已不存在的 user;migration 會清成 NULL)---'
SELECT count(*) AS 孤兒筆數
FROM alerts a
WHERE a.acknowledged_by_user_id IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM users u WHERE u.id = a.acknowledged_by_user_id);

\echo ''
\echo '=== ② 三個孤兒欄的實際資料(決定能不能刪)==='
\echo '--- 若三個數字都是 0,才可以排清理 migration ---'

SELECT 'agents.last_reviewer_id'   AS 欄位,
       count(last_reviewer_id)     AS 非空列數,
       count(*)                    AS 該表總列數
FROM agents
UNION ALL
SELECT 'audit_logs.actor_id', count(actor_id), count(*) FROM audit_logs
UNION ALL
SELECT 'users.auth_provider_id', count(auth_provider_id), count(*) FROM users;

\echo ''
\echo '=== ③ 現行 alembic 版本(確認起點)==='
SELECT version_num FROM alembic_version;

\echo ''
\echo '=== ④ 三欄若有資料,值長什麼樣(僅取樣 5 筆,協助判斷還在不在用)==='
SELECT 'agents' AS 表, id, last_reviewer_id::text AS 值
FROM agents WHERE last_reviewer_id IS NOT NULL LIMIT 5;
SELECT 'audit_logs' AS 表, id, actor_id::text AS 值
FROM audit_logs WHERE actor_id IS NOT NULL ORDER BY id DESC LIMIT 5;
SELECT 'users' AS 表, id, auth_provider_id::text AS 值
FROM users WHERE auth_provider_id IS NOT NULL LIMIT 5;
