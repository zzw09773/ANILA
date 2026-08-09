# 設定總覽頁 — 實作計畫

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development。
> 規格權威＝`docs/superpowers/specs/2026-08-08-settings-page-design.md`（含 08-08 三項追加裁決）。
> 盤點權威＝env 盤點報告（692 行，**不在 repo**；隨 SDD 工作區存放，路徑見帳本首行注記）。

**Goal:** 管理員在治理中心看到 csp 全部 95 顆設定的真相（值、預設、來源、生效條件），
其中 19 顆改了下一個請求生效、約 30 顆改了重啟生效——而且**畫面說的永遠等於實際發生的**。

**Tech:** FastAPI＋SQLAlchemy（沿用 `platform_settings` 表，**零 migration**）、
Vue 3（governance-ui，node --test）、pytest。

## Global Constraints

- 分支基底 `restart/from-redesign`；工作 worktree `wt-settings-page`（分支 `wt/settings-page`）。
- **本包零 alembic migration**（列缺席＝預設，寫入才建列）。若任何實作者認為需要 schema
  改動：STOP 回報，migration 編號由指揮官分配（下一個是 `r1_0035`），嚴禁自派。
- **絕不對活體 DB 跑 alembic；不碰 compose project `anila-restart`**。
- `platform_settings` 五條不變式全繼承（設計 §4）：讀不快取／值域函數兩端共用一份／
  一次解析拿兩個答案／壞值退回預設且誠實揭露／set 只 flush 不 commit（稽核同交易）。
- **A 類遮蔽發生在後端**；SEC 類永不可編輯；死變數永不上頁。
- 祕密零外洩（PUBLIC repo）；不得在容器內 `env`/`printenv`。
- 卡登旁路防守（Task 7）＝紅線鄰接：**雙票審**（跨家票可用時必用；sol 預計 08-09 09:43 回）。
- 每個 Task 結束 commit（英文）；驗收另派 fresh agent，驗收單必帶：
  「Look for the shape this package exists to eliminate, in the package's own work.」
  本包要消滅的形狀＝**「畫面上看到的設定與實際生效的值不一致」**
  （改了沒生效／沒改卻顯示改了／待生效畫成已生效／祕密上畫面／死變數畫成活的）。
- 常設要求（earned）：驗收自創突變；值域測試要掃**內插值**不是只掃邊界；
  任何「全類別」斷言用完整名單迭代不抽查。

## File Structure（預估，派工前逐錨點驗證——上一計畫三次栽在引用不存在）

| 檔案 | 責任 |
|---|---|
| `services/csp/app/services/settings_registry.py` | **新** — 唯一的設定宣告表（key、類別、型別、值域、預設、env 名、說明） |
| `services/csp/app/models/platform_setting.py` | 泛化 getter/setter（沿用門檻範本形；門檻既有函式不動） |
| `services/csp/app/api/platform_settings.py` | **新** — overview GET＋PUT（admin router） |
| `services/csp/app/config.py` | B 類開機覆蓋載入 hook（`:215` 凍結點之後、服務前） |
| 19 個 C 類消費點 | 逐顆改每請求讀 DB（盤點報告 §2 有讀取點 file:line） |
| `apps/csp-governance-ui/src/views/SettingsOverviewView.vue` | **新** — 四區三態頁 |
| `apps/csp-governance-ui/src/api/platformSettings.js` | **新** — API 模組（照 departments.js 慣例） |
| `infra/compose/platform.yml`＋`.env.example` | 死變數移除（FLUX_*×3、router MODEL、csp 不讀的 3 顆） |
| `services/csp/app/services/startup_security.py` | 卡登旁路 fail-loud 守衛 |

## Tasks（每個派工前展開＋commit；壓縮段落絕不直接派）

## Task 1：設定登錄表＋泛化存取層（展開 2026-08-08）

   **Files:**
   - New: `services/csp/app/services/settings_registry.py` — 唯一宣告點
   - Modify: `services/csp/app/models/platform_setting.py` — 泛化 getter/setter
     （**門檻既有函式一行不動**——它們是已關板的範本，泛化層是新函式不是改寫）
   - Test: `services/csp/tests/test_settings_registry.py`（新）

   **登錄表條目形**（dataclass／NamedTuple，欄位全必填）：
   `key`（點號命名空間：`limits.*` 數值鈕、`proxy.*` 速率逾時、`memory.*`、`intl.*` 語系類；
   命名規則＝env 名去 `ANILA_` 前綴、小寫、按消費模組分群——**登錄表是拼法的唯一權威**，
   審查驗一致性）／`env_name`（回退鏈用）／`setting_class`（`C`｜`B_EDIT`｜`B_LOCKED`｜`SEC`｜`A`，
   **全 95 顆都宣告**，不只可編輯的——overview 端點靠它分區）／`value_type`／
   `domain_fn`（**寫入與解析共用同一個函式物件**，不變式 2）／`default`／
   `restart_required: bool`／`description`（繁中，上畫面）／`locked_reason`（B_LOCKED/SEC 必填）。

   **名單來源**：C 19 顆＝設計 §3.1 逐字；B_EDIT/B_LOCKED 分界＝設計 §3.2 具名排除；
   每顆的 env 名與讀取點對 `.superpowers/sdd/2026-08-08-settings-page/env-recon.md` §2 逐一核。
   **死變數（FLUX_*×3 等）不入登錄表**——它們在 Task 7 從 compose 消失，頁面永不認識它們。

   **泛化存取層**（照 `platform_setting.py` 門檻三函式的形逐一對應）：
   `get_setting(db, key) -> value`（回退鏈 DB 列→env（若設）→default；每次主鍵查詢，禁快取）／
   `resolve_setting(db, key) -> (value, source)`（source ∈ db/env/default，一次解析拿兩個答案）／
   `set_setting(db, key, raw, user_id)`（domain_fn 驗過才寫、只 flush 不 commit）。
   壞值（DB 列存在但 domain_fn 拒絕）→ 退回下一層回退＋`source` 如實＋warning log。

   - [ ] Step 1 失敗測試：登錄表完整性（95 顆全宣告零孤兒——對 env-recon 名單集合運算；
     每顆 default 過自己的 domain_fn；C/B_EDIT 顆顆有 restart_required 正確值；
     B_LOCKED/SEC 顆顆有 locked_reason）；值域掃描**含內插值**（0.375 型）；
     回退鏈三態（DB 有列／無列有 env／全無）；壞 DB 值退回且 source 誠實。
   - [ ] Step 2–5：跑失敗 → 實作 → 跑通過 → commit
   （`feat(csp): every setting the platform reads, declared in one place`）
## Task 2：C 類消費端改造（a：嚴格 3＋數值鈕 7）（展開 2026-08-08）

**十顆**（key 以登錄表 `settings_registry.py` 為準）：嚴格 3＝`ANILA_ZH_NORMALIZE`／
`ANILA_QUERY_EXPANSION`／`ANILA_ZIP_FILENAME_ENC`；數值鈕 7＝`ANILA_MESSAGE_MAX_SIBLINGS`／
`ANILA_DEPARTMENT_MAX_DEPTH`／`ANILA_ATTACHMENT_BUDGET_RATIO`／`ANILA_ATTACHMENT_TOKEN_SAFETY`／
`ANILA_DEFAULT_CONTEXT_WINDOW`／`ANILA_ATTACHMENT_MAX_STORED_TOKENS`／`ANILA_ACTION_MAX_BODS_CHARS`
（⚠ 最後一顆的正確拼法以登錄表為準——別抄這行，抄登錄表）。

**方法（每顆同一套）**：
1. 讀取點改 `get_setting(db, key)`（每請求；讀取點 file:line 以
   `.superpowers/sdd/2026-08-08-settings-page/env-recon.md` §2 為準，動手前逐顆重驗——
   Task 1 之後行號可能漂）。呼叫點沒有 db session 的要沿現有依賴鏈拿，**不得**自開 session。
2. 該顆的 `restart_required` 翻成 False 的宣告**此刻才成真**——登錄表若已是 False 不動，
   若展開時發現登錄表與事實不符，STOP 回報（那是 Task 1 的回歸，不是你的鍋）。
3. **service 層 round-trip 釘**（HTTP 層等 Task 5）：`set_setting → DB 列 → get_setting →
   真正抵達下游函式的那個參數`，下界／**內插值**（0.375 型）／上界三點。
4. 舊 env 讀取路徑保留為回退鏈的一環（DB 無列→env→預設），**不是刪掉 env**。

- [ ] Step 1 失敗測試（十顆 round-trip＋回退鏈行為各一）
- [ ] Step 2–5：跑失敗 → 實作 → 跑通過 → commit
  （`feat(csp): ten knobs take effect on the next request, not the next restart`）
## Task 3：C 類消費端改造（b：速率逾時 5＋memory 4）（展開 2026-08-09）

**九顆**（key 拼法以登錄表為準）：proxy 組 5＝`ANILA_ACTION_INVOKE_PER_MIN`／`LLM_TIMEOUT`／
`EMBEDDING_TIMEOUT`／`PROXY_MAX_RETRIES`／`PROXY_RETRY_BASE_DELAY`；memory 組 4＝
`MEMORY_RETRIEVE_TOP_K`／`MEMORY_RETRIEVE_MIN_COSINE`／`MEMORY_MAX_CHUNK_CHARS`／`MEMORY_HTTP_TIMEOUT`。

**方法同 Task 2**（讀取點改 `get_setting(db, key)`、env 留回退鏈、動手前對 env-recon §2
重驗行號、沒 session 就沿現有依賴鏈穿、真的改不動就 STOP 別假裝），外加本組的三個特別點：
1. **memory 組是模組層讀取**（recon 組 C 明寫）——改造必須殺掉模組層捕捉，
   突變「把某顆改回模組層常數」必死。
2. **timeout 兩顆可能織進 httpx client 的建構**——若 client 是池化/模組層單例，
   per-request 值要下沉到**呼叫時參數**（httpx 允許 per-request timeout override），
   不是重建 client；哪一層生效要在報告寫清楚並被釘住。
3. **值域下限是命門**：timeout 拒 0 與過小值、retries 0–10、`MIN_COSINE` 0–1——
   Task 1 的 domain_fn 已宣告，本任務加**行為級**釘（set 一個下限外的值→被拒→
   生效值仍是舊的，而不是半套用）。

**常設規則（吃過兩次虧）**：行為測試的值必須 ≠ 場上每一個預設值（登錄表／config／env），
否則凍結與活讀分不出來。round-trip 三點照舊（下界／內插／上界）。

- [ ] Step 1 失敗測試（九顆 round-trip＋回退鏈＋上列三特別點）
- [ ] Step 2–5：跑失敗 → 實作 → 跑通過 → commit
  （`feat(csp): nine operational knobs go live-read, with floors that keep the platform up`）
## Task 4：B 類開機覆蓋（展開 2026-08-09）

**機制（設計 §2 追加裁決）**：csp 開機時（DB 可達之後、開始服務之前——實務上是 app lifespan
啟動段）讀 `platform_settings` 的 B_EDIT 類覆蓋值，套到凍結的 `Settings` 物件上。
pydantic v2 的凍結模型怎麼寫入（`object.__setattr__`／`model_copy`／wrapper）是實作判斷，
但**套用後所有既有讀取點看到的必須是覆蓋值**——不是第二份平行狀態。

**要件：**
1. **套用紀錄**：hook 留一份「哪些 key 套了什麼值」的紀錄（模組層唯讀 snapshot），
   Task 5 的 overview 靠它把「來源」欄寫成 db-boot——沒有紀錄，來源欄就得用猜的。
2. **載入失敗誠實三件組**（設計 §6.6）：DB 可達但讀取炸→以 env 值開機＋大聲警告＋
   snapshot 記「載入失敗」讓來源欄如實；**開機絕不能因設定表掛掉**；
   突變「載入失敗靜默假裝成功」必紅。
3. **收掉 Task 1 的 C1 遞延**：逐顆裁「這顆 B_EDIT 在 hook 套用前就被消費了嗎」——
   會的（已知嫌疑：`PYTHONUNBUFFERED`／`STATIC_DIR`／`DEBUG`／`ANILA_HOST`，以真碼為準）
   **降級 B_LOCKED＋locked_reason「開機序早於覆蓋載入」**（登錄表改動，屬本任務 SCOPE，
   逐顆在報告揭露）。降級後 B_EDIT 名單重算，census 測試同步更新。
4. **模擬 boot 測試工具**：fresh Settings＋跑一次 hook（對測試 DB）→ 斷言
   `PUT → DB 列 → 模擬 boot → 生效值` round-trip；至少三顆代表性 B_EDIT 各測
   （值 ≠ 場上每個預設——常設規則）。
5. worker／其他行程不載覆蓋（本輪範圍外，設計 §3.2 已鎖相關顆）——hook 只進 csp。

- [ ] Step 1 失敗測試（round-trip×3、失敗三件組、snapshot 誠實、降級顆的 census）
- [ ] Step 2–5：跑失敗 → 實作 → 跑通過 → commit
  （`feat(csp): boot picks up what the admin saved, and says so honestly`）
## Task 5：Overview＋PUT 端點（展開 2026-08-09）

**Files:** New `services/csp/app/api/platform_settings.py`（admin router，照
`institutional_kb.py` 的 `Depends(require_admin)` 形）＋ 新測試檔；main.py 掛 router。

**GET `/api/platform-settings/overview`**——全登錄表（96 條含門檻別名）payload，每列：
`key`／`class`／`description`／`restart_required`／`locked_reason`（有則帶）／
**`effective`＝現在真正生效的值**（C 類走 `get_setting`；B 類走 `getattr(settings,…)`——
**不是** snapshot 的 applied：`max(15,…)` 樓地板那型會讓兩者不同，Task 4 帳本明記）／
`stored`＝DB 列原值（無列＝null）／`pending`＝B_EDIT 且 stored≠本次開機套用值時為 stored
（＝「重啟後生效」的預告）／`source`∈ db｜db-boot｜env｜default（B 類靠 Task 4 的
`boot_override_snapshot()`；`load_failed=True` 時 B 類 source 一律如實回 env/default 並帶
`boot_override_load_failed: true` 頂層旗標——畫面要說「這次開機沒載入覆蓋」）。
**A 類**：`effective`／`stored` 一律 null，改帶 `is_set: bool`——**遮蔽在後端**，
測試掃全 13 顆名單斷言 payload 任何角落不含其值（用植入的 sentinel 值驗，不是抽查）。

**PUT `/api/platform-settings/{key}`**——只收 C 與 B_EDIT；B_LOCKED／SEC／A → 400
帶人話（含 locked_reason）；驗值走登錄表 domain_fn；稽核 `platform_setting_set`
**同交易**（flush 不 commit 的既有形），**並補上這個事件的測試斷言**（帳本舊債，
Task 4 之前就欠）。回應＝該列的 overview payload（改完立刻能看到 pending/effective 分態）。

**一次收掉的遞延（各一條測試）：**
1. **孤兒欄位掃描**（Task 2 carry）：`app/` 不得再出現那七顆 `settings.<FIELD>` 讀取
   （registry 有 env_name 對照，掃描照 Task 3 的 construction-pattern 經驗寫，零手抄）。
2. **降級七顆的自救路徑**（Task 4 carry）：`locked_reason` 補「怎麼改」（compose 鍵名）——
   登錄表文字修改屬本任務 SCOPE。
3. seed.* 三顆的 no-op 語意：description 補一句「僅首次開機生效」型說明（以真碼為準）。

**測試．常設規則全套用**：值 ≠ 場上每個預設；非法 key 的 PUT 全類別完整名單迭代；
A 類 sentinel 掃描全名單；`effective≠stored≠pending` 三態各有分辨測試
（ALERT_CHECK_INTERVAL 那顆樓地板案例直接入測）。

- [ ] Step 1 失敗測試 → Step 2–5：實作 → 通過 → commit
  （`feat(csp): one endpoint that tells the whole truth about every setting`）
## Task 6：前端四區三態頁（展開 2026-08-09）

**契約來源＝`task-5-report.md` 的 payload schema**（143 條後端測試釘著）：
`GET /api/platform-settings/overview` → `{total, boot_override_load_failed,
boot_override_failure_reason, boot_override_applied_count, items[96]}`；
`PUT /api/platform-settings/{key}` 回同型單列。**別抄本段，抄報告。**

**Files:**
- New: `apps/csp-governance-ui/src/views/SettingsOverviewView.vue`（路由照 `/users` 的
  `meta.requiresAdmin`；導覽入 `AppSidebar.vue` 的 `adminItems`，兩位數編號＋文字）
- New: `apps/csp-governance-ui/src/api/platformSettings.js`（照 departments.js 慣例）
- New: `apps/csp-governance-ui/src/utils/settingsView.js`——**分區／分態／顯示字串邏輯全抽這裡**
  （`node --test` 無法掛載元件，行為測試全打這個模組；.vue 只留樣板與呼叫）
- Test: `apps/csp-governance-ui/tests/settingsOverview.test.mjs`（新，node --test）

**版面（四區依 `class`）**：C（可編輯，改完下一請求生效）／B_EDIT（可編輯，儲存後顯示
「已儲存，**重啟後生效**：`docker compose up -d csp`」＋ pending 值並列）／
B_LOCKED＋SEC（唯讀＋`locked_reason` 全文——降級七顆的 compose 鍵指引要看得到）／
A（名稱＋`is_set`，永無值）。每列：`effective`／`stored`／`pending`／`default`／`source`。
頂部：`boot_override_load_failed=true` → 大字 banner「這次開機沒有載入覆蓋（原因）」。

**硬規則（全部有前科）：**
1. 顯示值一律來自後端回應——**不做樂觀更新**；PUT 後用回應那列**整列**取代（後端已釘
   回應＝事實）。2. `pending` 存在時必須與 `effective` **可分辨並列**（「待生效畫成已生效」
   是本計畫的殺形）。3. 錯誤 `detail` 原樣呈現（含值域說明與 locked_reason）。
4. 初載失敗要有錯誤 UI（DepartmentsView 沒有——**別抄它這點**，其餘照抄）。
5. 唯讀區用 runtimeConfigReadOnly 樣板的**原始碼 regex 護欄**釘「無 save handler」。
6. 測試值 ≠ 場上每個預設（三度前科）；分區測試全 96 列驅動（fixture 抄 task-5-report
   的 schema，各 class 至少兩列真實 key）。

- [ ] Step 1 失敗測試（utils 層：分區、三態分辨、banner、pending 字串、來源欄）＋
  regex 護欄（唯讀區無 handler、無樂觀更新樣式）
- [ ] Step 2–5：跑失敗 → 實作 → `npm run test` 通過 → commit
  （`feat(governance-ui): every setting on one page, telling only the truth`）
## Task 7：死變數清理＋卡登旁路防守（🔴 後者紅線雙票）（展開 2026-08-09）

**A. 死變數清理**：csp 的 `FLUX_BACKEND_URL`／`FLUX_MAX_CONCURRENT`／`FLUX_TIMEOUT_SECONDS`
與 router 的 `MODEL` 自 `infra/compose/platform.yml` 與 `.env.example` 移除＋原位歸檔註記
（一行：哪天 anila-studio 進 csp 映像再加回）；compose 有但 csp 不讀的 3 顆
（名單照 env-recon §0／§2.6，動手前重驗 grep 仍為零讀取點）同步處理。
測試：compose/.env.example 零出現（pattern 掃描）；登錄表本來就不認識它們（既有 census 頂著）。

**B. 卡登旁路防守（紅線鄰接：只加拒絕、不碰驗章邏輯）**：
`CARD_DEV_SKIP_NONCE_BINDING` 在**非 dev-card 模式**下被設（任何會讓消費端啟用的值）→
`startup_security` 開機即拒、錯誤訊息人話（照既有 dev 預設值檢查的形）。
1. **⚠ 命門：真值判定必須與消費端共用同一個函式。** `card_auth.py:121` 讀旗標**不 strip**
   （Task 2 M2 修過顯示端、消費端語意未動）——守衛若自寫解析，「` true `」會守衛放行、
   消費端啟用，旁路照開。抽出消費端的判定為單一函式，守衛與消費端都呼叫它；
   突變「守衛換成自己的解析」必死。
2. 「dev-card 模式」的定義以真碼為準（mock 讀卡機／dev CA 的既有旗標鏈，查
   `card_auth`／`startup_security` 現行判定），報告寫明依據 file:line。
3. 測試：非 dev 模式×消費端會啟用的每一種值形（含不 strip 語意的邊界形）→ 拒；
   dev 模式→放；未設→放；**紅線反向釘**：守衛存在不得影響 dev 模式下旗標的既有行為
   （驗章邏輯 diff 必須零觸碰——`card_auth.py` 只有抽函式的搬移，無語意變更）。

- [ ] Step 1 失敗測試 → Step 2–5：實作 → 通過 → commit
  （A、B 各一 commit：`chore(compose): retire the knobs nothing reads` ／
  `fix(csp): the card dev bypass cannot ride into a real boot`）
## Task 8：文件收尾

runbook（B 類重啟措辭對齊）、HANDOFF 長期照顧（登錄表是唯一宣告點／
   雙層優先序 DB>env 的除錯指南／TRUSTED_HOSTS 雙源收斂 follow-up）、
   設計文件與 OWNER-QUESTIONS 對齊。
## Task 9：最終全分支審查

（最強模型＋跨家第二票；sol 08-09 起可用）→
   `finishing-a-development-branch`。

## 驗收（另派 fresh agent，不自驗）

| # | 不變式 | 怎麼證明活著 |
|---|---|---|
| 1 | 顯示值＝生效值（C 即時、B 分態） | round-trip 釘＋「待生效畫成已生效」突變必紅 |
| 2 | A 類值永不出後端 | overview 全 A 名單掃描，遮蔽在後端測 |
| 3 | 收得下的＝算得出的 | 值域函式兩端共用，複製一份的突變必紅 |
| 4 | 從畫面打不掛平台 | timeout/retry/ratio 值域下限逐顆釘 |
| 5 | 覆蓋載入失敗不騙人 | env 開機＋警告＋來源如實，三件一組 |
| 6 | 非可編輯 key 的 PUT 必拒 | A/SEC/B-locked 全名單迭代 |
| 7 | 死變數不復活 | compose/頁面雙邊零出現 |
