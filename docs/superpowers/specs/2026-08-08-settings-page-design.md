# 設定總覽頁 — 設計（Q41：提前到打包前，直接做到可編輯）

> 擁有者 2026-08-08 裁決（Q41）：設定頁先於 wheelhouse／打包；範圍直接做到可編輯；
> 與九段第 6 段並行。後續三裁決（同日）：30 顆歧義變數一起審、
> `CARD_DEV_SKIP_NONCE_BINDING` 本包補防守（紅線雙票）、零讀取點死變數本包清掉。
> 盤點依據：2026-08-08 env 全盤點（692 行逐變數表；**刻意不進 repo**——
> 內含尚未修補的弱點位置，repo 是 PUBLIC；修補落地後隨 SDD 工作區存放）。

---

## 1. 問題與目標

部署包一旦進氣隙，改一顆環境變數＝改 `.env`＋recreate（運維動作），改錯了要重交付。
**設定頁決定「打包進去的值哪些能在氣隙內從畫面修正而且有效」。**

盤點戳破的第一件事：**folklore 的「41 顆」是 compose 鍵數，csp 實讀 95 顆**，
其中 **57 顆從未出現在 compose**——吃程式內預設值，今天的管理員不知道它們存在。
所以本頁的價值有兩層：**可見性**（95 顆全上頁，含那 57 顆隱形的）與
**可修性**（19 顆真的能從畫面改、下一個請求生效）。

## 2. 分類規則（硬的，逐顆有讀碼證據）

| 類 | 規則 | 數量 | 頁面行為 |
|---|---|---|---|
| **A 祕密** | key／token／password／帶憑證 DSN | 13 | 只顯示名稱＋「已設定／未設定」＋來源，**永不顯示值** |
| **SEC 安全類** | SSRF 白名單、信任錨、卡登、JWT 族 | 23 | 唯讀＋鎖標「安全類——本輪不可編輯」（`platform_settings` 無 CHECK/RLS，地基未補前不搬） |
| **B-可編輯** | boot 讀定，但值本身無安全語意、無跨行程消費、非基礎設施 DSN | ~30（實作時照登錄表定案） | **可編輯（擁有者 08-08 追加裁決）**：改的值存 DB，**開機時載入覆蓋 env**；畫面並列「現行生效值／待生效值」＋醒目「重啟後生效：`up -d <服務>`」提示 |
| **B-鎖定** | boot 讀定且踩任一排除（§3.2 具名理由） | ~10 | 顯示生效值＋預設值＋來源＋需重啟標記，無編輯控制項＋鎖定理由 |
| **C 執行期可調** | 每請求讀 DB（`platform_settings`） | **19** | 可編輯；改完**下一個請求生效** |

**B-可編輯的機制（擁有者選定：DB 開機覆蓋，不寫 `.env`）**：修改存 `platform_settings`
（同一張表、同命名空間規則）；csp 開機時（DB 可達之後、開始服務之前）載入 B 類覆蓋值
蓋過 `Settings` 凍結值。**理由**：寫 `.env` 需要把祕密檔 RW 掛進對外容器（隔離退化），
DB 覆蓋不碰 `.env`、不新增掛載、稽核白拿。**開機悖論排除**：連 DB 之前就要用的值
（DSN 族）天生不適用——它們本來就在 SEC/A。**載入失敗的誠實行為**：DB 可達但覆蓋載入
失敗→以 env 值開機＋大聲警告，頁面「來源」欄如實顯示 env（絕不能讓開機掛在設定表上，
也絕不能假裝覆蓋生效了）。

決定 B/C 的那一行是 `config.py:215` `settings = Settings()`——pydantic 在 import 當下讀完凍結。
所以「搬進 C」不是 UI 工，是**逐顆把消費端改成每請求讀 DB**（§4 範本）。

## 3. C 類名單（19 顆）與不搬裁定

### 3.1 搬（19）——每顆都要：點號命名空間 key、值域函式、round-trip 釘

| 組 | 變數 | 裁定理由 |
|---|---|---|
| 嚴格 C（本來就每請求讀 env） | `ANILA_ZH_NORMALIZE`・`ANILA_QUERY_EXPANSION`・`ANILA_ZIP_FILENAME_ENC` | 讀取模式已符合，搬遷成本最低 |
| 純數值調節鈕（7） | `ANILA_MESSAGE_MAX_SIBLINGS`・`ANILA_DEPARTMENT_MAX_DEPTH`・`ANILA_ATTACHMENT_BUDGET_RATIO`・`ANILA_ATTACHMENT_TOKEN_SAFETY`・`ANILA_DEFAULT_CONTEXT_WINDOW`・`ANILA_ATTACHMENT_MAX_STORED_TOKENS`・`ANILA_ACTION_MAX_BODY_CHARS` | 與門檻同種（無安全語意的數值鈕）；各自值域見 §6 |
| 速率／逾時（5） | `ANILA_ACTION_INVOKE_PER_MIN`・`LLM_TIMEOUT`・`EMBEDDING_TIMEOUT`・`PROXY_MAX_RETRIES`・`PROXY_RETRY_BASE_DELAY` | runbook 已叫操作者調前兩者；**值域下限是防呆生命線**（timeout 填 0＝從畫面把平台打掛，見 §6） |
| memory 檢索參數（4） | `MEMORY_RETRIEVE_TOP_K`・`MEMORY_RETRIEVE_MIN_COSINE`・`MEMORY_MAX_CHUNK_CHARS`・`MEMORY_HTTP_TIMEOUT` | `MIN_COSINE` 與已在 DB 的 `institutional_kb.score_threshold` 是同一種東西——結束「一個在 DB 一個在 env」的不一致 |

### 3.2 不開放編輯（B-鎖定／SEC／A；逐組具名理由，都是可逆裁定）

> 08-08 追加裁決後，「不搬 C」的組**預設落入 B-可編輯**（重啟生效）；
> 下列各組因具名理由**連 B-編輯都不開放**（B-鎖定），其餘 B 全數可編輯。

- **`MEMORY_LLM_MODEL`**：模型名不是數值鈕——換模型牽動 per-model 授權
  （「換嵌入模型就 403 且畫面無法補授權」是已知開放缺陷）＋Q35 前例
  （`ANILA_MODEL_FAST` 擁有者裁不接）。留 B，顯示。
- **SMTP 七連**：`SMTP_HOST` 是出向連線目標＝SSRF 鄰接面，且 Q3（relay）未定。留 B。
- **票期兩顆**（`ACCESS_TOKEN_EXPIRE_MINUTES`／`REFRESH_TOKEN_EXPIRE_DAYS`）：
  延長＝延長被竊 token 有效期，安全鄰接；無 CHECK/RLS 前不搬。歸 SEC 顯示。
- **OCR 六顆**（Tier-2）：主要消費者是 ingestion-worker（**另一行程，讀不到 csp DB**），
  搬了會造成「csp 預覽新值、worker 用舊 env」的分歧，而那個分歧目前是刻意設計。留 B，
  註記「搬遷需 worker 側設定讀取通道（另案）」。
- **`INGESTION_UPLOAD_DIR`／`REDIS_URL`**：檔案系統語意／跨服務基礎設施 DSN，
  執行期改＝事故製造機。留 B。
- **`ANILA_TRUSTED_HOSTS`**：本來就 SEC；且**已有 DB 表 `trusted_hosts` 做同一件事**——
  雙重來源要收斂是獨立 follow-up（入交接待辦），本包不碰。

## 4. 後端規格（照 `platform_settings` 範本，五條不變式全繼承）

- **表不變**：`platform_settings` 四欄已夠用（點號命名空間 key＋Text value）；
  **本包零 alembic migration**（列缺席＝用預設，寫入時才建列）。
- **設定登錄表**（新模組 `app/services/settings_registry.py` 之類）：每顆 C 設定一筆
  宣告（key、型別、值域函式、預設、對應舊 env 名、說明文字）。**值域函式寫入與解析共用
  一份**（不變式 2）；**壞值退回預設且 `calibrated`/`from_default` 誠實揭露**（不變式 4）。
- **讀取端改造**（本包的主要工程）：19 個消費點逐顆從「讀 `settings.X`／`os.environ`」
  改成 `get_<name>(db)`（每請求一次主鍵查詢，**禁快取**——不變式 1 的原話：
  「畫面上改了一個數字、後端還在讀舊值，是本平台可能出的最大的假控制項」）。
  **相容序**：DB 有列→用 DB；無列→用 env 值（若設）→否則程式預設。
  env 繼續有效直到管理員第一次從畫面改——氣隙升級不需要遷移手續。
- **端點**：
  - `GET /api/platform-settings/overview`（admin）：回全部 95 顆的
    `{key, 顯示值, 預設值, 來源(DB/env/預設), 類別(A/SEC/B/C), 需重啟?}`。
    **A 類值一律遮蔽為「已設定/未設定」——遮蔽發生在後端**，前端拿不到值。
  - `PUT /api/platform-settings/{key}`（admin，僅 C 類 key）：範本形——resolve 舊值→
    set→稽核（**同交易**，不變式 5）→回新 payload。非 C key 一律 400＋人話。
- **稽核**：沿用 `platform_setting_set` 事件（本包順手補上它缺席的測試斷言——帳本舊債）。

## 5. 前端規格（`apps/csp-governance-ui`）

- 新 view `SettingsOverviewView.vue`，路由照 `/users`（`meta.requiresAdmin`），
  導覽入 `AppSidebar.vue` 的 `adminItems`（兩位數編號＋文字，無 icon）。
- 版型照 `DepartmentsView.vue`（表格→編輯→PUT→重抓），**但補上它沒有的
  初載 try/catch＋錯誤 UI**（無聲空白是付過代價的形狀）。
- 依類分四區：C（可編輯）／B（唯讀＋需重啟標記）／SEC（唯讀＋鎖標）／A（名稱＋存在性）。
  每列都有「來源」欄（DB／env／程式預設）——**57 顆隱形變數在這裡第一次現身**。
- 顯示值一律來自後端回應，**不做樂觀更新**（Task 7 同款規則）。
- 錯誤 `detail` 原樣呈現（後端訊息含值域說明）。
- 測試工具鏈現實：`node --test`、無元件掛載——**編輯邏輯抽 `src/utils/`** 直測；
  B/SEC/A 區用 `runtimeConfigReadOnly.test.mjs` 的**原始碼正則護欄**釘
  「這一區沒有 save handler」；C 類真正的來回驗證落在後端 pytest（§6）。

## 6. 驗收（每顆 C 設定同一張門）

1. **內部值 round-trip 釘**（`6666fbc8` 教訓，逐字要求）：
   `PUT → DB 那一列 → GET → 真正抵達下游函式的那個參數`，至少下界／**內插中間值**（0.375 型）／上界三點。
   不是只測邊界拒絕。**這一段就是 41 顆要複製的那一段**——現在真的開始複製了，
   行為釘必須跟著複製，不是只抄 docstring。
2. **displayed == effective**（`2f657f60` 同款）；壞值退回預設時不得宣稱「有人設過」。
3. **值域是防呆生命線**：timeout 類下限 ≥1 秒、retries 0–10、ratio 0–1 閉區間、
   正整數類 ≥1——每顆的值域在登錄表宣告並在設計審查逐顆核。
   **從畫面把平台打掛必須是不可能的**，不是「不建議」。
4. **A 類遮蔽是後端行為**：測 overview 回應不含任何 A 值（全 A 名單掃過，不是抽查）。
5. **非 C key 的 PUT 必 400**：全類別掃過（A/SEC/B 各取全名單），不是抽一顆。
6. **B-可編輯的三態誠實釘**（08-08 追加機制的專屬門）：
   - `PUT` 後、重啟前：頁面必須**同時**顯示「現行生效值＝舊值」與「待生效值＝新值」，
     兩者可分辨——**絕不能在重啟前把待生效值顯示成生效值**（那正是本包要消滅的形狀）。
   - 模擬重啟驗證：pytest 內以「fresh Settings＋跑一次開機覆蓋載入」模擬 boot，
     斷言載入後生效值＝DB 存值（`PUT → DB 列 → 模擬 boot → 生效值` 的 round-trip）。
   - 覆蓋載入失敗路徑：DB 表壞掉時服務仍以 env 值開機＋警告，來源欄顯示 env
     ——突變「載入失敗靜默假裝成功」必須紅。
7. 驗收單必帶：「Look for the shape this package exists to eliminate, in the package's own work.」
   本包要消滅的形狀是**「畫面上看到的設定與實際生效的值不一致」**（含：改了沒生效、
   沒改卻顯示改了、**待生效被畫成已生效**、祕密上了畫面、死變數畫成活的）。
7. ⚠ shell 的 `mutation-check.mjs` 與本包無關（不碰 anila-shell）；
   governance-ui 無同款工具——**審查者自創突變**照專案常設要求。

## 7. 附帶工程（同包、各自成 commit）

- **死變數清理**：csp 的 `FLUX_BACKEND_URL`／`FLUX_MAX_CONCURRENT`／`FLUX_TIMEOUT_SECONDS`
  （全樹零讀取點）與 router 的 `MODEL` 自 compose／`.env.example` 移除＋歸檔註記；
  compose 有但 csp 不讀的 3 顆同步處理。設定頁**永不**顯示零讀取點變數。
- **卡登 dev 旁路防守**（🔴 紅線鄰接，雙票審）：`CARD_DEV_SKIP_NONCE_BINDING`
  目前程式讀得到、compose 沒宣告、startup_security 沒擋。加：**非 dev-card 模式下
  設了此旗標→開機即拒（fail-loud）**。比照 startup_security 既有 dev 預設值檢查的形。

## 8. 明確不做（本輪）

- SMTP／OCR／票期／`REDIS_URL`／`UPLOAD_DIR` 的搬遷（§3.2 各有理由，皆可逆）。
- `platform_settings` 的 CHECK/RLS 地基（搬安全類的前置，另案）。
- `ANILA_TRUSTED_HOSTS` 雙重來源收斂（follow-up，入交接）。
- 其他服務（router/worker/asr）的設定頁化——本輪只有 csp 行程讀得到的東西；
  Tier-3 純 compose 插值 8 顆不進頁（它們不進容器 env）。
- worker 側設定讀取通道（OCR 組的前置，另案）。

## 9. 開放問題（不擋開發，照保守假設走）

- §3.2 的四組不搬裁定都是**可逆**的（搬遷機制就緒後每顆只是登錄表加一筆＋改一個讀取點）；
  擁有者若要翻任何一組，成本是一顆一包的小事。
- B 類 40 顆的「需重啟」標記措辭（「改 `.env` 後 `up -d` 該服務」）由 Task 實作時
  對 runbook 用語，不另開題。
