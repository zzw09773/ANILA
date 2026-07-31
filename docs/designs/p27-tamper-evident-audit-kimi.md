# P27 設計：單人氣隙部署下真正防竄改的稽核帳本
**模型家族：kimi · 2026-07-30 · 設計稿，不含實作**

---

## 0. 事實核查（全部有 file:line，對題述前提的異議見 §8）

| 題述主張 | 核查結果 |
|---|---|
| 稽核表無 REVOKE/trigger/rule | 成立。全 migrations 僅兩處無關命中：`r1_0017_agent_collection_bindings.py:78`（同步 trigger）、`0037_ingestion_images_rls.py:80`（REVOKE 的是 FUNCTION）|
| 0014 GRANT ALL＋DEFAULT PRIVILEGES | 成立，行號微調：GRANT ALL 在 `0014_add_ingestion_platform.py:128-130`，ALTER DEFAULT PRIVILEGES 在 `:131-136` |
| 0014 把 ownership 轉給 csp_app，原因是 boot-time ALTER | 成立。`:139-159` 的 DO 迴圈，註解 `:140-143` 明寫「GRANT ALL doesn't cover ALTER…app would crash on first start」；migration superuser 由 `MIGRATION_DATABASE_URL` 保留（`migrations/env.py:45-53`，compose `infra/compose/platform.yml:69`）|
| owner 可 DROP TRIGGER 後改歷史 | 成立（Postgres 語意：DROP TRIGGER/ALTER TABLE 需 ownership；owner 本就隱含全權限，連 GRANT 都不必）|
| 唯一刻意 UPDATE 路徑 | 成立。`app/api/users.py:745-748`：hard-delete 使用者時把 `audit_logs.actor_user_id` 設 NULL（同檔 `:750-752` 對 alerts 也做一樣的事）|
| boot-time ALTER 的實際位置 | `app/services/startup_migrations.py:79-315`（`_ensure_schema_backfills`），在 lifespan 用 **runtime engine**（`=DATABASE_URL=csp_app`）執行（`:63`）；其中 audit_logs 區塊在 `:260-277` |
| DB 現可拋棄 | 成立（PLAN 0.4 / CLAUDE.md §4，本機已砍庫從零驗到 r1_0011+）|
| 題述未提但關鍵 | **半年 retention 意味著刪除**（SYSTEM-MAP.md:289「留多久：半年」）。目前沒有任何 prune 機制（grep 全 app 無 retention/prune job）。任何 append-only 設計若不定義「封存點前移」語意，第一個月報週期後維運者就會手動 DELETE，而那看起來跟竄改一模一樣 |

---

## 1. 威脅模型分級與一條不證自明的前提

| 代號 | 攻擊者 | 能力 |
|---|---|---|
| A | 應用層 admin（3000 使用者中被授權者） | 只有 UI/API |
| B | 持有 `csp_app` runtime 憑證者（外洩的 DSN、容器 shell、SQLi） | DB 協議、且目前是**所有表的 owner** |
| C | 持有主機 root 者 | `.env` 裡有 `MIGRATION_DATABASE_URL`（superuser）、檔案系統、備份、docker |
| D | 備份檔調換者 | 離線替換 dump |

**前提（不繞路，直接承認）**：單機氣隙內網裡，**任何只存在這台機器上的證據都贏不了 C**。對 C 唯一有效的機制是「證據離開主機、到另一個人手上」。而這個平台已經有一個現成的、每月必然發生的人工儀式：**給長官的月報表**（SYSTEM-MAP.md:326）與「要能匯出給稽核單位」（:290）。本設計的脊柱就是把鏈頭 digest 綁進這個既有儀式——**不發明新公證人，月報就是公證人**。anchor 不需要簽章：簽章解決「誰產生的」，但單主機上私鑰就在 C 手裡；人類把一行 hash 抄進已發出的報告，提供的是「事後無法否認的見證」，這才是對 C 有效的性質。

---

## 2. 推薦設計（三層，各自獨立可落地；L0 是脊柱）

### L0 — Hash chain + 雙頻率 anchor（對 C/D 的偵測）

**DB 變更**（一個 alembic migration，趁 DB 可拋棄做）：

1. `audit_logs`、`policy_decisions`、`classification_events` 三表各加：
   `chain_seq BIGINT`（共用一條 sequence `chain_seq`，跨表全局遞增）、
   `prev_hash CHAR(64)`、`row_hash CHAR(64)`。
2. 新表 `chain_head`（單列：`last_seq`, `last_hash`, `updated_at`）。
3. 新表 `audit_anchors`（append-only：`id, created_at, kind, max_seq, head_hash, note`）。

**鏈結由 DB trigger 做，不是 app 做**——這是本設計與「在 log_audit_event 裡算 hash」的分水嶺：

- `BEFORE INSERT` trigger（SECURITY DEFINER，owner = migration role）在同一 transaction 內：取 `chain_seq` 下一值、`SELECT ... FOR UPDATE` 鎖 `chain_head`、算 `row_hash = sha256(prev_hash ‖ 序列化欄位)`、UPDATE head。
- 理由：app 層算鏈可被任何拿到 csp_app 的人用裸 INSERT 繞過；trigger 層誰來 INSERT 都會鏈，且 csp_app **不是表 owner，停不掉 trigger**。`log_audit_event`（`app/services/audit_service.py:72`）一行不用改，fail-soft 語意（`:87-93`）原樣保留。
- 單鏈跨三表（而非每表一鏈）：驗證只需一條鏈，「刪掉整張表的某段」無所遁形。

**Anchor（兩個頻率，各防一層）**：

- **每晚，零人工**：host cron 跑 `docker exec csp python -m app.scripts.audit_anchor` → SECURITY DEFINER 函數寫一筆 `audit_anchors`，並把一行 `date | max_seq | head_hash` append 到主機上 DB 之外的純文字檔（bind-mount 出來的 `audit-anchors.log`）。**這個檔案防的是 B**：B 只有 DB 協議，碰不到主機檔案，所以他就算重算整條鏈也改不了昨晚已落檔的頭。
- **每月，五分鐘**：維運者跑 `audit_anchor --export`，把輸出那一行貼進月報、並把 `audit-anchors.log` 複製到自己工作站的另一台機器。**這一步防的是 C**：從 digest 離開 .15 那一刻起，C 在 .15 上做的任何歷史改寫都會在下一次驗證時對不上。月報本來就要做，邊際成本是貼一行字。

**驗證**：`audit_verify.py [--anchor N]` 從起點重算到指定 anchor 比對；兩個固定觸發點：①每月產月報時順手跑一次；②**任何備份還原後必跑**（這條直接回答 D：被調換/回捲的備份，鏈頭對不上最後一個 anchor；就算調換者放回較早的合法備份，`audit_anchors` 表本身與月報序列的 seq 單調性會露餡）。

### L1 — 權限分離（對 B 的**預防**，不只是偵測）

趁 DB 可拋棄，把稽核集合搬進獨立 schema `audit`：

1. 三表＋`chain_head`＋`audit_anchors` `SET SCHEMA audit`，owner = migration role（`csp`）。
2. `GRANT USAGE ON SCHEMA audit`、`GRANT SELECT, INSERT ON` 三事件表、`GRANT SELECT ON chain_head, audit_anchors`、`GRANT USAGE ON SEQUENCE` 給 `csp_app`。**UPDATE/DELETE/TRUNCATE 一律不給。**
3. `ALTER DEFAULT PRIVILEGES IN SCHEMA audit GRANT SELECT, INSERT ON TABLES TO csp_app`。
   ——這是為什麼要搬 schema 而不是只 REVOKE：0014 的 `:131-136` 會讓**未來任何建在 public 的新表自動 GRANT ALL 給 csp_app**，日後新稽核表會靜默退回裸奔；獨立 schema 讓預設權限各管各的，一次解決「未來洩漏」。
4. 加 `BEFORE UPDATE OR DELETE` trigger → RAISE EXCEPTION（owner 在 migration role，csp_app 無法 DROP）。這是 belt-and-suspenders：即使有人用 superuser 事後 GRANT UPDATE，trigger 仍擋；superuser 能 DROP TRIGGER——但那是 C 的領域，C 本來就靠 anchor 偵測。

**消滅唯一合法 UPDATE（`users.py:745-748`）——用「不留例外」的方式**：
不為它開 UPDATE 洞。改為 **soft-delete / tombstone user**：hard-delete 改成「刪除 PII 資產（api_keys、權限）＋user 列保留墓碑（username 雜湊化或標記 redacted）」。理由：①程式庫已有 soft-revoke 傳統（P1.3 unit_admin）；②audit_logs 本來就有 `actor_username` 去正規化快照＋`detail` 裡的 snapshot（`users.py:725-731`），actor FK 的剩餘價值很低；③任何「合法 UPDATE 例外」都會成為日後濫用的模板。若隱私政策真的連墓碑 username 都不能留，退一步用窄化 SECURITY DEFINER 函數 `redact_actor(user_id)`（只 NULL 該欄、本身落一筆稽核事件），**絕不發一般性 UPDATE grant**。

**Retention 語意（題述漏掉的硬問題）**：新增 SECURITY DEFINER `audit_prune(before_date)`：①先強制要求「剪除點之前的 anchor 已匯出」才執行；②刪除超過半年的列；③把 prune 事件本身作為一筆鏈上事件寫入。驗證端：genesis 前移——以最後一個已匯出 anchor 為新起點。**每月儀式於是固定成三件事：匯出 anchor →（滿半年後）prune → 驗證**，一支腳本跑完。

### L2 — boot-time ALTER 衝突的解法（治本，不是繞路）

衝突根源不是「csp_app 需要 ALTER 權限」，而是 **`_ensure_schema_backfills` 跑在 runtime engine 上**（`startup_migrations.py:63`）。解法約十行：

1. lifespan 裡用 `MIGRATION_DATABASE_URL` 建第二支 engine，把 `_ensure_schema_backfills` 整個改跑在 migration engine 上。於是**沒有任何表**需要 csp_app ownership 才能開機自我修復，CREATE INDEX 同理解決。
2. 刪掉 backfill 清單裡的 audit_logs 區塊（`startup_migrations.py:260-277`）——稽核表 DDL 從此只由 alembic（superuser）管，雙頭馬車消滅。
3. 非稽核表的 ownership **維持現狀不動**（0014 的轉移不 rollback）。它們不在防竄改周界內，動它只是增加 diff 與風險，對單人維運沒好處。這是有意的最小爆炸半徑。

### 應用層變更總清單

- 三個 SQLAlchemy model 加 `__table_args__ = {"schema": "audit"}`＋三個新欄位。
- `log_audit_event` 等寫入路徑：**不動**（trigger 鏈結）。
- 新增：`audit_anchor` / `audit_verify` 兩支管理腳本（走正式 DB 連線，不開新 API 端點——admin 匯出本來就該能看 digest，但 CLI 比端點少一個攻擊面）。
- `users.py` hard-delete 改 tombstone。
- compose：host cron 一行＋anchor log 的 bind-mount。

---

## 3. 防禦矩陣（誠實版）

| 攻擊者 | 防到什麼 | 防不到什麼 |
|---|---|---|
| A（app admin） | **完全預防**：無端點、無 DB 權限可改歷史 | 他做的每個真實操作都會如實落帳（本來就該如此） |
| B（csp_app 持有者） | **預防**：SELECT/INSERT/EXECUTE only，非 owner 停不了 trigger，鏈頭只有 owner 的 trigger 函數寫得了；經 DB 協議**無法改歷史，連重算重鏈都不行**（他寫不了 chain_head 也寫不了舊列）。裸 INSERT 假事件＝正常寫入路徑，無法區分（固有），靠 nginx log 交叉。INSERT 即鏈結，無「鏈結前窗口」 | 無 |
| C（主機 root） | **偵測**：每月 off-host anchor 貼進月報＋另一台機器副本後，任何改寫在下次驗證暴露；月報把 digest 送到**另一個人手上**，形成人證 | **無法預防、無法即時偵測**；偵測週期＝距上次匯出。這是單機拓撲的物理極限，誰聲稱能做得更好誰就在賣假安心感 |
| D（備份調換） | **偵測**：還原後必跑 verify，鏈頭對不上 anchor；回捲攻擊被 seq 單調性＋月報 digest 序列抓出 | 若調換發生在「從未匯出過任何 anchor」期間，無基準可比——所以上線第一天就要匯出第一個 anchor |

---

## 4. 成本（對「不要把系統越用越嚴格」的直接回答）

- **熱路徑延遲**：每筆稽核事件多一次 sha256（微秒級）＋同一 transaction 內對單列 `chain_head` 的 `FOR UPDATE`（持鎖亞毫秒）。**+0.2~0.5ms/筆**。稽核是動作粒度（登入、受控讀取、管理動作），不是 token 粒度；3000 使用者峰值估每小時數千筆，單列鎖綽綽有餘。題述「序列化寫入是真成本」在此負載下不成立；真到瓶頸的逃生艙是每表獨立鏈＋anchor 時合併，**現在不預先做**。
- **維運者每週工時：0**。每晚 anchor 是 cron。
- **每月：約 5 分鐘**——跑一支腳本（匯出 anchor＋驗證＋滿半年後 prune），把一行 hash 貼進本來就要做的月報，順手把 anchor log 複製到另一台機器。
- **放生一個月會怎樣：什麼都不會壞**。寫入永不因帳本機制失敗（沿用既有 fail-soft）；只是對 C 的鑑識水平退回「最後一次匯出的 anchor 日期」，on-host anchor 照樣每晚累積。**退化是安靜且單向的，沒有任何東西會因為維運者忘記而爆炸**——這是與舊平台「限制不斷複利」最關鍵的差別。
- **明確說出不方便的點**：日後任何新稽核表要記得建在 `audit` schema；hard-delete 使用者不再是真正抹除（改 tombstone）；這兩點是唯一新增的「心智規則」。

---

## 5. 80% 便宜版（若主設計被判太重）

只做：**鏈欄位＋在 `log_audit_event` 裡 in-app 算鏈＋每晚 cron 落 on-host anchor 檔＋每月貼月報＋verify 腳本**。不動 schema/role/ownership，不修 boot ALTER，不消 UPDATE 路徑。

- 工作量：約一個工作天、一支 migration、零權限手術。
- 防禦：對 C/D 與主設計**相同**（anchor 機制不變）；對 B 降為「偵測」——B 可改歷史並重算重鏈，但**任何已匯出 anchor 之前**的歷史一樣動不了（on-host 檔案他碰不到）；盲區＝距上次 anchor 的事件（on-host ≤24h、off-host ≤1 個月）。
- 缺的：對 B 的預防、「新表自動 GRANT ALL」的未來洩漏、retention 語意。
- 適用判準：若擁有者評估 B 類攻擊者（憑證外洩但拿不到主機）機率遠低於 C，80% 版是合理取捨；否則 L1 那一層（搬 schema）是整份設計裡 CP 值最高的預防性投資，且只有趁 DB 可拋棄的現在才便宜。

---

## 6. 我不會做的事（含對題述中選項的否決）

1. **不做區塊鏈／外部時間戳／TSA**：氣隙內網沒有公證權威可連；月報＋稽核單位就是公證人，再疊一層密碼學只是給維運者加儀式。
2. **不做簽章金鑰託管／HSM**：金鑰在主機上＝C 可以偽造簽章；金鑰放別處＝每次 anchor 都要人工搬金鑰。見證性質由「人把 digest 抄進已發出的報告」提供，不需私鑰。
3. **不做雙人控制**：擁有者已拍板 R1 雙人流程退場，且一人維運下雙人制是擺設。
4. **不把 token_usage 納入鏈**：量大、價值密度低，鏈它是「安全複利」的典型陷阱；月報數字用與稽核事件計數交叉抽核即可。
5. **不用 chattr +i / immutable FS／WORM 花招保護 on-host anchor 檔**：root 全部繞得過，還會弄壞 logrotate，典型的「越用越嚴格」負資產。
6. **不拿走維運者的 superuser**：他必須能維護系統。本設計的目標是 tamper-**evidence**，不是 tamper-proof；假裝能對持主機的人防竄改，正是舊平台被拋棄的那種思維。
7. **不為 redaction 開 UPDATE 例外**：例外會變模板；用 tombstone 或窄化 SECURITY DEFINER 函數。
8. **不讓帳本硬化影響可用性**：稽核寫入維持 fail-soft（`audit_service.py:87`），鏈結失敗不得 cascade 成使用者 500。
9. **不預先做分片鏈／非同步鏈結**：負載不到，預先最佳化只會增加一人維運的認知負擔。

---

## 7. 實施順序（若採用）

1. M1：L2 的 engine 切換（先治本，獨立可驗：開機照舊、ownership 不再必要）。
2. M2：L1 搬 schema＋權限＋trigger＋tombstone 化 hard-delete。
3. M3：L0 鏈欄位＋鏈結 trigger＋anchor/prune/verify 腳本＋cron。
4. 上線第一天：匯出 genesis anchor 貼進當月月報。
（M1 與 M3 檔案集不相交，依審查閘門新制可平行；M2 碰 migration＋權限屬紅線級。）

---

## 8. 對題述前提的異議與補充

1. **「hash chain 不錨定就白搭」——同意，但要修正「錨定＝放到對方碰不到的地方」**。錨定的本質是「獨立見證」，不是「不可達」。一行貼進已發出月報的 digest，C 碰得到 .15 上的一切，卻碰不到**已經送出去的那張報表**。這使得最便宜的錨定恰好是既有流程，成本趨近零。
2. **「序列化寫入在 3000 使用者是真成本」——在本負載下不成立**。稽核是動作粒度不是請求粒度；單列鎖持鎖時間亞毫秒。數字見 §4。
3. **題述低估了 retention 的破壞力**：半年留存（SYSTEM-MAP.md:289）必然引入刪除，而「沒有語意的刪除」與竄改在鑑識上不可區分。任何候選設計若沒定義 prune 與鏈的共存方式，第一次清理就會製造一個永遠洗不清的疑點。本設計的 `audit_prune`＋genesis 前移是必要件，不是加分項。
4. 小更正：題述「owner 先 GRANT UPDATE 給自己」——owner 隱含全權限，連 GRANT 都不必；真正的關鍵能力是 DROP TRIGGER 與 ALTER TABLE。另 0014 行號應為 128-136（題述 129/132），不影響結論。
5. 舊版 migration 自稱 trigger「對任何 role 都成立」——同意題述判決為假，且補一刀：就算 ownership 不轉，superuser（`MIGRATION_DATABASE_URL` 就在同一份 `.env`）也繞得過。所以觸發器層永遠只是對 B 的預防，對 C 的保證只能來自 off-host anchor；兩層不可互相替代。
