# P27 設計:一人氣隙部署下,真正防得住的稽核帳(fable)

## 0. 先講結論與對題目框架的三點異議

**結論**:兩層設計。第一層(資料庫權限分離)把「持 runtime 憑證的內部人」徹底關在門外,
一次做完、之後零維護;第二層(每日批次雜湊鏈,鏈頭嵌進本來就要交的月報)把「持主機/superuser
的內部人」從「防不了」變成「改了會被抓到」。熱路徑零延遲,操作者每週零分鐘。

**異議一:brief 把 `users.py:746` 的 UPDATE 當成要遷就的既有路徑,我認為它本身就是稽核缺陷。**
硬刪使用者時把 `audit_logs.actor_user_id` 清成 NULL(`services/csp/app/api/users.py:745-747`),
等於**刪帳號就能洗掉自己在稽核帳上的數字身分**——這正是威脅模型裡的人最想要的功能。
`actor_username` 已經反正規化存字串(`app/models/audit_log.py:12`),正確解法是**拿掉 FK、保留整數 id**,
不是設計一條合法 UPDATE 通道去遷就它。修掉之後,稽核家族表的合法 UPDATE 路徑=零,全樹已驗證
(`grep query(AuditLog)` 全樹僅此一處 update;PolicyDecision/ClassificationEvent 無任何 update/delete)。

**異議二:「hash chain 在 3000 人上是 real cost」方向對、理由不對。** 氣隙內網的稽核 INSERT 量
不會讓每列串鏈的鎖成為瓶頸;真正的理由是:**未錨定前,列級鏈與日級鏈的證據力相同**——內部人
改寫必然連鏈頭一起重算,所以粒度只影響「最後一個已分發錨點之後的窗口」,列級化只是把窗口從
24h 縮到 0 並換來熱路徑序列化點。窗口 24h 對「月報防假」這個需求綽綽有餘。

**異議三:「收回 ownership 與 boot-time ALTER 相撞」被講成全域衝突,實際上很窄。**
`run_startup_migrations` 碰 audit_logs 的只有 `app/services/startup_migrations.py:260-277`
(補三個欄位+放寬 resource_id 長度)。把這幾條 DDL 搬進 alembic(本來就該在那),0014 的
ownership 模型**其他表一概不動**,只有稽核家族三張表換主人。不需要重做整個權限架構。

## 1. 保護集合(明確列舉,單一出處)

- `audit_logs`(`app/models/audit_log.py:8`)
- `policy_decisions`(`app/models/policy_decision.py:47`)
- `classification_events`(`app/models/classification.py:55`)
- 新增的 `audit_checkpoints`(第二層,見 §3)

**刻意排除**:`declassification_requests`(`classification.py:86`)是工作流表,狀態會合法變更;
`token_usage` 是計量表,有 backfill 需求(`startup_migrations.py:161-180`),列為日後候選、不進首發。
保護集合定義在一個 Python 常數,migration、boot 自檢、pytest 三處共用同一份,不各自抄。

## 2. 第一層:資料庫權限分離(一次性,DB 現在可拋,PLAN 0.4 之窗)

現況已驗證:`migrations/versions/0014_add_ingestion_platform.py:129` GRANT ALL、`:131-133`
ALTER DEFAULT PRIVILEGES、`:145-165` 把全部表的 OWNER 轉給 `csp_app`。runtime 走 `csp_app`、
alembic 走 `csp` superuser(`migrations/env.py:52`、`infra/compose/platform.yml:68-69`)。

新 alembic migration(r1_00xx,跑在 `csp` 身分下)做四件事:

1. **搬家**:把 `startup_migrations.py:260-277` 的 audit_logs DDL 收編為 alembic 步驟,
   並從 boot 路徑刪除該段(加一條 pytest:startup_migrations 的 DDL 清單與保護集合交集必須為空)。
2. **換主人+收權**:保護集合 `OWNER TO csp`(或非登入的 `csp_owner`);
   `REVOKE ALL FROM csp_app`;`GRANT SELECT, INSERT` 回來;序列 `GRANT USAGE, SELECT`(nextval 要用)。
   從此 `csp_app` 沒有 UPDATE/DELETE、不是 owner、DROP TRIGGER/ALTER 一律 "must be owner" 失敗。
3. **觸發器(皮帶加吊帶)**:BEFORE UPDATE 一律 RAISE;BEFORE DELETE 僅允許
   `OLD.created_at < now() - interval '180 days'`(SYSTEM-MAP §8 留半年,保留期購毀是唯一合法刪除,
   任何角色跑都行,不用開後門 GUC)。owner 非 `csp_app`,所以 runtime 憑證拆不掉它。
   **誠實標注**:superuser 永遠繞得過——這句話上一代寫對了,錯在接著宣稱觸發器對任何 role 成立;
   本設計不重複那個謊,superuser 交給第二層。
4. **應用側修正**:刪 `users.py:745-747` 的 update;drop `audit_logs.actor_user_id` 的 FK
   (欄位保留、值保留)。附帶效果:硬刪帳號後稽核歸屬不再被洗掉。

**回歸防線(關鍵,因為 `:131-133` 的 default privileges 還在對未來新表發 GRANT ALL)**:
在 `startup_security.py` 加 production 自檢——對保護集合逐表
`has_table_privilege('csp_app', t, 'UPDATE'|'DELETE'|'TRUNCATE')` 必須全 false、owner 必須非 csp_app,
否則拒絕啟動。跟現有「secrets 非 dev 值否則不給起」同一個 fail-closed 模式,零操作者成本,
未來哪個 migration 忘了收權,csp 起不來,當天就會被發現,而不是六個月後在稽核現場發現。

## 3. 第二層:日級雜湊鏈+月報錨定(對付 superuser/host/備份)

原理:主機持有者改什麼都可以,**唯一改不了的是已經送出去的東西**。本平台恰好有一個
天然的、已存在的外送管道:月報與稽核匯出(SYSTEM-MAP §8「要能匯出給稽核單位」)。

- 每日 00:05,csp 內排程作業把前一日保護集合新列(按 id 序、canonical JSON)算 SHA-256,
  與前日鏈頭串接:`h_n = H(h_{n-1} ‖ digest(day_n))`,寫入 `audit_checkpoints`
  (本身也在保護集合,csp_app 只能 INSERT)。同時把鏈頭打到容器 stdout(docker logs 留一份時間線)。
- **月報/稽核匯出的頁尾自動嵌入**:目前鏈頭、涵蓋的 checkpoint 區間、列數。
  領導與稽核單位留存的紙本/檔案副本就是錨——一人氣隙部署裡唯一真正在維運者手外的東西。
- 附一支管理指令 `verify-audit-chain --head <hex>`:重算全鏈、比對任一份歷史報告上的鏈頭。
  只在有爭議時才跑,平時不跑。

**INSERT 偽造要誠實承認**:持 csp_app 憑證的人仍可插入假列(捏造,非竄改)。鏈不防這個;
防的是「事後改寫已分發區間」。捏造的列會被鏈住、帶著時間戳與 ip,反而成為呈堂證據。

## 4. 防得住什麼、防不住什麼(逐一)

| 對手 | 結果 |
|---|---|
| app admin(角色),無 DB 憑證 | **全防**。API 無稽核寫改端點;匯出照 `audit_logs` 遮蔽規則 |
| 持 `csp_app` runtime 憑證(=brief 點名的人) | **改寫/刪除/拆觸發器全部做不到**(非 owner+REVOKE+觸發器);只剩 INSERT 捏造,見 §3 |
| 持 `csp` superuser / host root / docker(=維運者本人) | **防不住,只能事證化**:改寫已分發鏈頭涵蓋的區間→驗鏈必露餡;未分發窗口(≤24h+當月未出報部分)是盲區,講明白 |
| 換備份檔的人 | 還原後鏈頭對不上任何一份已分發報告→**還原程序必含 verify**,寫進 runbook 一行 |

## 5. 成本(照 brief 要求講死)

- **熱路徑**:0ms。INSERT 語句一字不改,無序列化點,無新鎖。
- **每週操作者時間**:0 分鐘。checkpoint 全自動;月報鏈頭自動嵌入,不多按一個鍵。
- **一次性**:一個 alembic migration+一個排程作業+匯出頁尾+boot 自檢,估 1–2 個工作包(紅線級,雙票跨家)。
- **放著一個月不管**:什麼都不壞。checkpoint 照跑;唯一劣化是「最後分發錨點」變舊,盲區窗口變長。
  半年不出報也只是回到今天的現狀,不會更差——**沒有任何會把平台鎖死的失敗模式**,
  唯一 fail-closed 是 boot 自檢,而它只在有人真的把權限改壞時才觸發。
- **保留期購毀**:每季跑一次購毀指令(或不跑;十萬級列數放兩年 Postgres 無感)。

## 6. 便宜 80% 方案

只做第一層(§2 全部,含 boot 自檢與 users.py 修正),不做 checkpoint/錨定。
把 brief 點名的「持 runtime 憑證的內部人」完整關掉,一次性成本再減半,長期成本仍為零。
放棄的是對 superuser/host 的事證力——但要說清楚:**那正是規格上「防止 admin 偷偷做假」
最後指向的人**(一人維運=admin=host root),所以 80% 方案其實只覆蓋威脅模型的外圈。
我的建議仍是全做:第二層的邊際成本幾乎只有月報頁尾那幾行。

## 7. 我不會做的事

1. **列級 per-write hash chain**——熱路徑序列化+程式複雜度,證據力不高於日級鏈(§0 異議二)。
2. **第二台「稽核金庫」主機/logical replication/pgaudit 外送**——一人維運沒有第二個信任域,
   多一台機器=多一個沒人顧的東西,正是上一代被棄養的路。
3. **WORM 硬體、HSM 簽章、區塊鏈式帳本**——8 月底上線,氣隙採購,不存在的選項。
4. **為保留 FK 而禁止硬刪帳號**——拿掉 FK 即可,別把稽核需求變成使用者管理的枷鎖。
5. **任何需要每週人工儀式的設計**——擁有者原話已判死刑;會被跳過的控制=沒有控制。
6. **宣稱觸發器「對任何 role 都成立」**——上一代的假話。文件與 UI 措辭照 SYSTEM-MAP 的誠實原則:
   寫「可事證,superuser 級竄改由外部錨點偵測」,不寫「不可竄改」。

## 8. 落地順序

DB 現在可拋(PLAN 0.4)→ 第一層 migration 趁現在進,從零重建直接長成正確姿態;
第二層可與 P4 平行,唯一硬相依是稽核匯出格式(P2 前收斂)。紅線級審查(權限+migration+不可逆)。
