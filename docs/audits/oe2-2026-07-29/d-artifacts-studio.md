# OE-2 領域 D:ARTIFACTS / STUDIO 對 SYSTEM-MAP 的規格符合性稽核

- 稽核基準:`SYSTEM-MAP.md`(repo 根,399 行,2026-07-29)——「這份取代先前所有規劃文件的權威地位」(L4)
- 稽核範圍:指派的 10 個檔案(見 §6 逐檔交代)
- 稽核日期:2026-07-29
- 唯讀稽核,未修改任何 repo 檔案

---

## 0. 結論摘要

**構造總數 52,分類:CAT-A 15 / CAT-B 14 / CAT-C 23。**

### 這份契約實際建了什麼(先量,再判)

| 面向 | 數量 | 出處 |
|---|---|---|
| 資料表(r1_0007) | **4 張、64 欄、10 索引** | `artifacts`(17 欄)、`artifact_jobs`(19 欄)、`export_records`(16 欄)、`artifact_versions`(12 欄) |
| 資料表(相鄰,r1_0001/r1_0003) | **2 張、28 欄** | `source_snapshots`(16 欄)、`citations`(12 欄) |
| Pydantic 契約型別 | **16 個**(3 個封閉 enum + 13 個 model) | `app/schemas/contracts/artifacts.py` |
| 狀態機 | **2 座** | `ArtifactJobStatus` 四值(6 條合法邊、終態零出邊)、`ArtifactStatus` 四值(**實際只有 completed 可達**) |
| HTTP 端點 | **6 個** | `/v1/artifact-jobs` ×2、`/v1/artifacts` ×1、`/v1/artifacts/{id}/versions`、`/v1/artifacts/{id}/exports`、`/api/artifacts` ×2 |
| Studio 端 | 1 個 Redis 持久 job store(17 欄投影)+ 1 條 fire-and-forget 回報通道 | `job_store.py`、`job_reporting.py`、`job_lifecycle.py` |

### SYSTEM-MAP 對 Studio 產出總共說了幾句話

全文對 Studio 產出的規範**只有兩處、共 8 行**:

- **L167**:`| **Studio 產出** | 暫存伺服器 | 設時限後自動刪(暫定 3 天) | 產它的人 |`
- **L354–361 §11**:輸入=知識庫不是對話;產出簡報/報告優先其餘次要;「能改」= 下載 `.pptx`、**不需要網頁編輯器**;沒有規定版型只要放 logo;**心智圖不是產出物**;可延後。
- 另 **L49**:`| Studio 產出 | — | ✓(可延後) |`

亦即規格要的是:**一個暫存目錄 + 擁有者 + 到期自動刪**。

### 兩項最重要的判讀

1. **契約蓋了 6 表 92 欄,規格只要「暫存 + 時限 + 產它的人」。** 落差不是細節多寡,而是**形狀相反**:規格要「會自己消失的暫存」,契約建的是「不會消失的永久登錄簿 + 不可變版本史 + 匯出永久帳」。三張 CSP 表(`artifacts` / `artifact_versions` / `export_records`)**完全沒有保留期或到期欄位**。
2. **規格唯一具體要求的那一項,反而沒做。** L167 的「設時限後自動刪(暫定 3 天)」在本域 **零實作**:`artifact_jobs.expires_at`(model L200)只被寫入(service L137),**沒有任何程式讀它**,也沒有任何 reaper;Studio 端只有各 pipeline 60 分鐘的 in-memory stale 逐出、與 Redis 7 天 TTL(config L78)。**沒有一處是 3 天。** 詳見 §4 缺口 G-1。

---

## 1. 主表 —— 逐構造判定

判定欄位說明:Q1 = SYSTEM-MAP 是否要求(逐字引用+行號,或「查無基礎(已搜:…)」);Q2 = 是否有持續成本;Q3 = 舊 doc 是否為僅存理由。
**CAT-A** = Q1 yes;**CAT-B** = Q1 no、Q2 no;**CAT-C** = Q1 no、Q2 yes、Q3 yes。

### 1.1 `app/models/artifact.py`

| # | 構造 | file:line | doc 出處 | Q1 證據 | Q2 成本 | 類 | 建議 |
|---|---|---|---|---|---|---|---|
| A1 | `artifacts` 表本體(擁有者 + 標題 + 建立時間) | artifact.py:59-72,76,105 | doc 01 Artifact | **有**。L167「**Studio 產出** \| 暫存伺服器 \| 設時限後自動刪(暫定 3 天) \| **產它的人** \|」——「產它的人」要求記得擁有者,「設時限自動刪」要求記得建立時間 | 需維護 | **A** | keep + 改引 L167;**但欄位由 17 縮到「owner / type / title / storage_ref / created_at / expires_at」六欄級** |
| A2a | `artifact_type` = `slides` / `report` | artifact.py:71;contracts:27-28 | doc 02/01 五值 | **有**。L357「**產出:簡報 / 報告優先**;其餘次要」 | — | **A** | keep + 改引 L357 |
| A2b | `artifact_type` = `mindmap` | artifact.py:71;contracts:29 | doc 02/01 | **反向明文**。L360「**心智圖不是產出物**,是讓使用者快速理解知識庫節點的**瀏覽方式**」 | 是:一整條 mindmap pipeline 掛在產出物語意上 | **C** | converge:心智圖不進 artifact 型別(瀏覽功能另放) |
| A2c | `artifact_type` = `infographic` / `datatable` | artifact.py:71;contracts:30-31 | doc 02/01 | **弱有**。L357「其餘次要」——未點名、亦未排除 | 低 | **B** | keep + mark-debt(次要,優先序在簡報/報告之後) |
| A3 | `Artifact.status` 四值 queued/generating/completed/failed | artifact.py:73-75;contracts:43-49 | doc 01 四值 | 查無基礎(已搜:狀態、進度、queued、生成、job) | 低:**service.py:210 寫死 `"completed"`,四值中三值不可達**,但仍佔 API 契約欄位 | **B** | keep + mark-debt(死枚舉,收斂時一併降為布林或刪) |
| A4 | **binding 規則:必綁 `source_task_id` 或 `source_snapshot_id`,否則 422** | artifact.py:79-87;service.py:202-206;api:165-184 | constitution §6 / doc 01 | 查無基礎(已搜:task、任務、快照、來源、依據 → SYSTEM-MAP 全文**沒有 Task 這個概念**)。且**與 L356 相斥**:「**輸入:知識庫**(像 NotebookLM),不是對話」——Studio 的來源是 collection,不是 Task | **高**。Studio 端 `task_id`/`source_snapshot_id` 是 ALM 選填的 passthrough(studio.py:453、job_lifecycle.py:127-128);ALM 沒先建 Task 時,`POST /v1/artifacts` 一律 422,而回報是 fire-and-forget 吞錯 → **整條產出登錄靜默失效**。同時強迫未來任何產出流程先造一個 Task | **C ★** | **converge**:改綁 `collection_id`(L356 的來源),移除 task/snapshot 強制 |
| A5 | `job_id` 純字串參照 + 刻意不掛 FK(避免雙向環) | artifact.py:88-89;migration:20-23 | doc 02 | 查無基礎(隨 A11) | 隨 A11 | **C** | converge(附隨 `artifact_jobs`) |
| A6 | `artifact_versions` 表 + `current_version` + 版本遞增 + 唯一約束 | artifact.py:90-91,121-148;service.py:233-239 | doc 01 ArtifactVersion | 查無基礎(已搜:版本、修訂、歷史、version)。**且與 L358 反向**:「**「能改」= 下載成 `.pptx` 用 PowerPoint 自己改** → **不需要網頁編輯器**」——平台內不編輯,就沒有伺服器端版本史要留 | **高**:每次註冊必寫一列版本、每版同步分類、多一張表與一組唯一約束 | **C ★** | **converge**:單一 `storage_ref` 取代版本表 |
| A7 | `trace_id` 欄(artifacts:92 / artifact_jobs:192 / export_records:236)+ 2 條 trace 索引 | artifact.py:92,192,236;migration:97,227 | doc 02 | **反向明文**。L211「→ 一張表加幾個索引。**不需要 span 樹、parent 關係、trace id。**」;L399「需求是兩句話,做出來的是一套分散式追蹤治理協定」 | 是:3 欄 + 2 索引 + Studio 端 span 發射器佈線(job_lifecycle.py 第 3 項關注點) | **C ★** | **converge**:刪欄與索引 |
| A8 | `metadata_json` | artifact.py:93 | doc 01 | 查無基礎(已搜:metadata、中繼) | 低(自由格式,無流程) | **B** | keep + mark-debt |
| A9a | `classification_level` 欄(artifacts / artifact_versions / export_records) | artifact.py:96-97,144-145,238-239 | doc 08 §5 | **有**。L227「密等在**專案啟動時就標好了**,平台主要是**記錄**它」;L238「\| **落稽核** \| **不用** \| **要** \| **要** \| **要** \|」 | 需維護 | **A** | keep + 改引 L227/L232-238。⚠ 值域五級 → 四級由 **OE-3** 處理,本稽核不重複 |
| A9b | 分類三顆衛星欄 `classification_latched_at` / `_source` / `_event_id` | artifact.py:98-104,240-246;source_snapshot.py:73-79 | doc 08 §5 | 間接。無逐字文;由 policy 核心寫入(`modules/policy/service.py:322-324`),支撐 L275「記什麼 \| …**改密等**…」的可追溯性 | 低:寫入集中在 policy 核心一處,資源端只被動存 | **B** | keep + mark-debt(改密等稽核可只靠 `classification_events` 表,衛星欄是去正規化便利) |
| A10a | `ArtifactVersion.storage_ref`(產出檔案位址) | artifact.py:136 | doc 01 | **有**(概念層)。L167「暫存**伺服器**」——產出必須有一個伺服器上的位址 | 需維護 | **A** | keep + 改引 L167;但欄位應搬回 `artifacts` 本表(隨 A6 收斂) |
| A10b | `content_hash` / `file_refs` | artifact.py:137-138 | doc 01 | 查無基礎(已搜:雜湊、完整性、checksum) | 低 | **B** | keep + mark-debt |
| A10c | `citation_map` | artifact.py:139 | doc 01 | 查無基礎(已搜:引用、出處、來源 → 全文 0 命中「引用」) | 是:依賴整張 `citations` 表(B5) | **C** | converge(附隨 B5) |
| A10d | `generated_by_model_id` / `_agent_id` / `_studio_job_id` | artifact.py:140-142 | doc 01 | 查無基礎。⚠ 注意 L210「要記的欄位:時間、使用者、部門、**模型**、**agent**、token 數」是**用量表**的欄位,不是產出物的;§7 明言「一張表加幾個索引」 | 是:三欄要跟 model/agent registry 對齊 | **C** | converge(用量歸屬走 usage 表,不在產出物上複製) |
| A10e | 每版 `classification_level` + `sync_version_level()` | artifact.py:143-145;service.py:285-292 | doc 08 §5 | 查無基礎。且 **`sync_version_level` 全 repo 零呼叫者**(僅在 `__init__.py:32,46` re-export)= 死碼 | 是:版本表在,就得同步 | **C** | converge(附隨 A6);死碼即刻可刪 |
| A11 | **`artifact_jobs` 表(19 欄 + 4 索引)** | artifact.py:151-200;migration:182-227 | doc 02 ArtifactJob 逐欄、doc 02 §8 | 查無基礎(已搜:job、工作、進度、重啟、restart、持久 → 全文 0 命中)。唯一理由是 doc 02 §8「Studio restart 後 job 不應丟失」,SYSTEM-MAP 從未提出此需求 | **最高**:19 欄 + 2 個端點 + 狀態機 + Studio 端回報器 + **與 Redis job store 重複記帳(同一份 job 存兩份)** | **C ★★** | **converge**:整表移除;job 狀態留在 Studio 進程/Redis 即可 |
| A11a | ├ `progress` / `message` / `result_metadata` / `artifact_files` / `error` / `params_digest` | artifact.py:185-191 | doc 02 逐欄 | 查無基礎 | 隨 A11 | **C** | converge |
| A11b | ├ `requester_employee_id`(員編字串留存) | artifact.py:171 | doc 02 | 弱有。L91「**憑證裡沒有單位** —— 只有**員工編號**、姓名、email」 | 低 | **B** | keep + mark-debt(若 A11 收斂,搬到 `artifacts.owner`) |
| A11c | ├ `collection_id` | artifact.py:172 | doc 02 | **有**。L356「**輸入:知識庫**(像 NotebookLM),不是對話」——這是本契約中**唯一**與規格來源模型對齊的欄位 | 需維護 | **A** | **keep + 升為主來源欄**(取代 A4 的 task/snapshot binding) |
| A11d | └ `expires_at` | artifact.py:200;service.py:137 | doc 02 | **有(意圖)**。L167「**設時限後自動刪(暫定 3 天)**」 | 是:**有欄無人讀** —— 全 repo 只有寫入點,無 reaper | **A** | keep,但**必須搬到 `artifacts` 表並補上真正的刪除任務**(見缺口 G-1) |
| A12a | `export_records` 「記一筆匯出」核心(誰、何時、哪個 artifact) | artifact.py:203-224,247 | doc 01 ExportRecord | **有**(間接)。L275「記什麼 \| 讀取受控文件/對話、上傳、刪除、改密等、**匯出**、列印、分享…」;L238 落稽核門檻 | 需維護 | **A** | keep + 改引 L275;⚠ 但正解可能是寫進統一稽核帳(L280「append-only 的稽核帳」),而非本域自建第二本帳 |
| A12b | `target_space` / `target_classification_floor` | artifact.py:226-227;contracts:140-141 | doc 08 §10 | 查無基礎(已搜:目的地、空間、下限、target)。SYSTEM-MAP 的匯出規則是**密等門檻**(L241-242「可以做 = 密等 ≤ 營業秘密」),**不是目的地空間比較** | 是:必填欄(contracts:140),每次匯出都要有人決定填什麼 | **C ★** | converge(隨 E10 判定式) |
| A12c | `decision` 欄(恆為 `"allow"`) | artifact.py:234-235;service.py:277 | doc 01 | 查無基礎。deny 依設計不落本表 → 此欄永遠單值 | 低 | **B** | keep + mark-debt(死欄) |
| A12d | `policy_decision_id` 掛回 append-only 裁決帳 | artifact.py:230-233 | doc 01 | 間接。L280「**append-only 的稽核帳因此是必要的,不是過度設計**」 | 低 | **B** | keep + 改引 L280 |
| A12e | `export_records` 四共通分類欄 | artifact.py:237-246 | doc 08 §5 | 見 A9a/A9b(等級有基礎、衛星欄間接) | 低 | **B** | keep + mark-debt |
| A13 | `ExportRecord.trace_id` | artifact.py:236 | doc 01 | **反向明文**(同 A7,L211) | 隨 A7 | **C** | converge |

### 1.2 `app/models/source_snapshot.py`

> ⚠ **跨領域**:`source_snapshots` 由 `modules/tasks/service.py:184` 建立,`citations` 亦被 policy / launch / proxy 參照。以下判定是**規格基礎判定**,收斂執行必須與 tasks/proxy 領域合議,不可單邊拆。

| # | 構造 | file:line | doc 出處 | Q1 證據 | Q2 成本 | 類 | 建議 |
|---|---|---|---|---|---|---|---|
| B1 | `source_snapshots` 表(16 欄)= Task 來源的不可變快照 | source_snapshot.py:43-88 | doc 01 §4-5 | 查無基礎(已搜:快照、snapshot、不可變、凍結 → 全文 0 命中「快照」) | 是:16 欄 + 一整套不可變語意 + artifact binding 依賴它 | **C** | converge(跨領域合議) |
| B2 | 規則 1:回答/artifact/launch **必**指向 snapshot 或明確宣告無來源 | source_snapshot.py:4-6;→ A4 | doc 01 拍板 | 查無基礎(同 A4) | 高(同 A4) | **C ★** | converge(同 A4) |
| B3 | 規則 2:Citation 只指 snapshot 內 chunk、`document_id` 刻意不掛 live FK | source_snapshot.py:7-9,92,101-102 | doc 01 | 查無基礎 | 低(在 Citation 存在的前提下這是**好**設計) | **B** | keep + mark-debt(若 B5 收斂則一併消失) |
| B4 | 規則 3:snapshot 分類 = 所有來源取 max | source_snapshot.py:10-11,69-71 | doc 01 + doc 08 | **有**(間接)。L227「密等…平台主要是**記錄**它」+ 單向閂鎖為硬需求(見 E5) | 需維護 | **A** | keep + 改引 L227 |
| B5 | `citations` 表(12 欄:chunk_id/quote_preview/page/score/`span_start`/`span_end`/`used_by` 三值) | source_snapshot.py:91-118 | doc 01 §4-5 | 查無基礎(已搜:引用、出處、來源、依據、節點 → 「引用」全文 0 命中) | 是:12 欄 + 與 chunk 儲存綁死;`span_start`/`span_end` 是字元位移級精度 | **C** | converge(跨領域合議;若 ANILALM 要顯示出處,重新以最小形狀提案) |
| B6 | `origin` 五值 / `source_scope` 五值 enum | source_snapshot.py:53-58 | doc 01 | 查無基礎 | 隨 B1 | **C** | converge |
| B7 | `document_versions` 版本指紋 / `content_hash` / `payload_ref` | source_snapshot.py:63,66,68 | doc 01 | 查無基礎 | 隨 B1 | **C** | converge |

### 1.3 `app/modules/artifacts/__init__.py`

| # | 構造 | file:line | doc 出處 | Q1 證據 | Q2 成本 | 類 | 建議 |
|---|---|---|---|---|---|---|---|
| C1 | independence 契約:本 package **不得** import `modules.tasks`/`policy`/`launch`/`app.api`;外界只能從 package 根 import | `__init__.py`:11-17;service.py:9-15 | doc(module 邊界) | 查無基礎(已搜:模組、邊界、獨立、分層 → 無架構分層要求)。SYSTEM-MAP L13-15 只說「任何需要專人照顧的機制…都是負債」 | 是:**限制未來設計** —— 為守這條規則,單向閂鎖被迫外移到 `app/api/artifacts.py` 當 orchestrator(api:9-13)。且**無任何測試強制**,純靠 docstring 自律 | **C** | converge:降級為註解建議,或補真正的 import linter;一人維運不該養無工具支撐的分層儀式 |
| C2 | 11 個函式的公開 re-export 面 | `__init__.py`:22-48 | — | 隨底層構造 | 隨底層 | — | 隨 A/B 收斂同步縮減 |

### 1.4 `app/modules/artifacts/service.py`

| # | 構造 | file:line | doc 出處 | Q1 證據 | Q2 成本 | 類 | 建議 |
|---|---|---|---|---|---|---|---|
| D1 | **job 狀態機** `_LEGAL_JOB_TRANSITIONS`(4 態、6 邊、終態零出邊 → 409) | service.py:53-69,162-167 | doc 02 四值 | 查無基礎(已搜:狀態、狀態機、轉移) | 是:Studio 每次轉移都得配合;**且終態零出邊使「重報同一終態」被 409 拒絕**,與 fire-and-forget 重試語意衝突(reporter 只重試傳輸/5xx,409 被吞) | **C ★** | converge(隨 A11)。⚠ 與 **OE-1**(agent 七態機收斂為 registered/approved/disabled)同型病灶 |
| D2 | `register_job` 冪等 upsert(on `job_id`) | service.py:119-148 | doc 02 | 查無基礎(冪等本身是好工程,但服務於無基礎的表) | 隨 A11 | **C** | converge(附隨) |
| D3 | `inherited_level`(讀 task/snapshot 取 max) | service.py:91-114 | doc 08 §5 | 部分:取 max 有基礎(B4/E5),**但來源選 task/snapshot 無基礎**(A4) | 隨 A4 | **C** | converge:改讀 collection 分類 |
| D4 | `create_artifact` binding fail-closed | service.py:202-206 | constitution §6 | 同 A4 | 同 A4 | **C ★** | converge |
| D5 | `create_version` 版本遞增 | service.py:225-257 | doc 01 | 同 A6 | 同 A6 | **C** | converge |
| D6 | `record_export` | service.py:260-282 | doc 01 + doc 08 §10 | 見 A12a(核心有基礎)/A12b(形狀無基礎) | 中 | **A**(核心)/ **C**(目的地模型) | 保留「記一筆匯出」,拆掉 target_space 模型 |
| D7 | `sync_version_level`(**零呼叫者,死碼**) | service.py:285-292 | doc 08 §5 | 查無基礎 | 是:公開 API 面上的死函式 | **C** | converge:即刻刪除 |
| D8 | `resolve_owner`:員編騎 `users.username` 解析,查不到仍留員編字串 | service.py:74-88 | doc(卡登身分) | 弱有。L91「憑證裡沒有單位 —— 只有**員工編號**、姓名、email」 | 低。⚠ 副作用:`owner_user_id=None` 的無主列可被建立,與 L167「產它的人」語意衝突 | **B** | keep + mark-debt(無主列應拒絕或補救) |
| D9 | **owner-scope 讀取閘**(`ensure_artifact_access` / `list_artifacts` owner 分支) | service.py:297-322,338-349 | doc(治理讀面) | **有**。L167「…\| **產它的人** \|」——只有產它的人看得到 | 需維護 | **A ★** | **keep + 改引 L167**(本域與規格對齊最好的構造) |
| D10 | `is_admin` 全看 bypass | service.py:315,318,331,338 | doc | 查無基礎。L167 說「產它的人」(未列 admin);L73 明訂單位管理員「❌ 看對話明文」(產出非對話,但精神相近) | 低(單一分支) | **B** | keep + mark-debt(擁有者裁決題:admin 該不該看得到別人的產出) |
| D11 | `classification_level` 查詢過濾參數 | service.py:328,354-355;api:422 | doc | 查無基礎 | 低 | **B** | keep + mark-debt |
| D12 | `_is_owner_of` 經 task 申請人認定擁有權 | service.py:301-311 | doc 01 | 隨 A4(Task 概念無基礎) | 隨 A4 | **C** | converge |

### 1.5 `app/api/artifacts.py`

| # | 構造 | file:line | doc 出處 | Q1 證據 | Q2 成本 | 類 | 建議 |
|---|---|---|---|---|---|---|---|
| E1 | **`require_service_caller`**:`/v1` 寫入面僅收 service token;帶使用者憑證 → 403、匿名 → 401 | api:101-123 | doc 10 §12 | **有**(間接 + 專案安全紅線)。L349「\| agent/模型權限、**跨使用者資料存取** \| A01 存取控制失效 \|」;L346「\| 對 agent 的身分靠純文字標頭 \| A07 身分驗證失效 \|」;L338「**不得有 OWASP Top 10 高風險項**」 | 需維護 | **A** | keep + 改引 L338/L346/L349。**安全構造,不得因 doc 引用而收斂**;若端點本身收斂則隨之移除 |
| E2 | legacy 靜態 token 以 `hmac.compare_digest` 常數時間比對 | api:93-97 | — | 專案安全紅線(常數時間比較) | 低 | **A**(比較方式)/ **B**(靜態 token 本身) | keep;mark-debt:靜態共用 token 正是 L146-148 點名的反模式,長期應換 §4 L150 的「短效簽章 token」 |
| E3 | `require_export_caller` 雙軌(user JWT 或 service token) | api:126-152 | doc 09 | 隨 E10 端點 | 中 | **C** | converge(附隨匯出端點形狀改變) |
| E4 | `_resolve_binding` 存在性 404 | api:165-184 | doc 01 | 隨 A4 | 隨 A4 | **C** | converge |
| E5 | **`_latch_inheritance`**:分類單向閂鎖(只升不降,經 policy 核心寫 ClassificationEvent) | api:187-217 | doc 08 §2/§5 | **有**。L227「密等在**專案啟動時就標好了**,平台主要是**記錄**它」;L189「⚠ **對話中途升密** → 之前萃取的記憶要撤回」(規格預設「升密」是既成事實)。**校準明列:單向升級閂鎖是硬規格需求,即使帶 doc 08 引用** | 需維護 | **A ★** | **keep + 改引 L189/L227**;來源改為 collection(隨 A4) |
| E6 | `POST /v1/artifact-jobs` | api:223-238 | doc 02/09 | 查無基礎(隨 A11) | 是 | **C** | converge |
| E7 | `PATCH /v1/artifact-jobs/{job_id}` | api:241-256 | doc 02/09 | 查無基礎(隨 A11/D1) | 是 | **C** | converge |
| E8 | `POST /v1/artifacts`(產出登錄) | api:259-310 | doc 01/09 | **有**(核心):L167 要記擁有者與時限。**無基礎**(形狀):binding 驗證、繼承閂鎖來源、首版建立 | 需維護 | **A**(端點)/ **C**(形狀) | keep 端點 + 大幅簡化 body |
| E9 | `POST /v1/artifacts/{id}/versions` | api:313-346 | doc 01/09 | 查無基礎;**與 L358「不需要網頁編輯器」反向** | 是 | **C ★** | converge:整個端點移除 |
| E10 | **匯出判定式** `allow if target_floor >= artifact.level` | api:359-372(判定在 :367) | doc 08 §10 | 查無基礎。SYSTEM-MAP 的規則完全不同:L241-242「**兩條簡單的線:** 可以做 = 密等 ≤ **營業秘密**;要落稽核 = 密等 ≥ **營業秘密**」;L235「\| 匯出 \| 可以 \| **可以** \| 不可 \| 不可 \|」。且 L53 顯示 ANILALM 側 `匯出` 是「—」,Studio 產出的「能改」在 L358 只是**下載 .pptx** | **高**:每次匯出都要呼叫端提供 `target_classification_floor`(必填);判定軸與規格不同軸 | **C ★** | **converge**:改為 L241-242 的兩條線。⚠ 與 **OE-4** 高度相鄰(OE-4 改的是門檻,本項改的是**判定式本身的輸入模型**),兩者應合併執行 |
| E11 | deny → 403 + 一筆 deny `PolicyDecision`,**不落** allow 匯出列 | api:374-394 | doc 00 §6 | **有**(間接)。L275「記什麼 \| …匯出…」;L280「**append-only 的稽核帳因此是必要的,不是過度設計。** 威脅模型包含特權內部人」 | 低 | **A** | keep + 改引 L275/L280 |
| E12 | `GET /api/artifacts` + `GET /api/artifacts/{id}`(治理讀面) | api:416-453 | doc 09 | **有**。L167「\| 產它的人 \|」——產它的人要看得到自己的產出 | 需維護 | **A** | keep + 改引 L167。⚠ 缺口:讀取未落稽核(見 G-4) |
| E13 | `_INHERIT_REASON = "source_selected"` | api:71 | doc 08 §6 reason 七值 | 查無基礎(reason 值域屬 policy 域) | 低 | **B** | keep + mark-debt(值域由 policy 域統一處理) |

### 1.6 `app/schemas/contracts/artifacts.py`

| # | 構造 | file:line | doc 出處 | Q1 證據 | Q2 成本 | 類 | 建議 |
|---|---|---|---|---|---|---|---|
| F1 | `ArtifactType` / `ArtifactJobStatus` / `ArtifactStatus` 三個封閉 enum | contracts:24-49 | doc 02/01 逐字 | 見 A2a/A2b/A2c、A3、D1 | — | 混合 | 見對應列 |
| F2 | `_require_requester` 驗證:`requester_user_id` 或 `employee_id` 至少一 | contracts:76-82 | doc 02 | **有**(間接)。L167「產它的人」+ L210「要記的欄位:時間、**使用者**、部門…」——不得有無主產出 | 低 | **A** | keep + 改引 L167/L210 |
| F3 | `progress` `ge=0 le=100` | contracts:71,89 | doc 02 | 查無基礎(隨 A11) | 隨 A11 | **C** | converge |
| F4 | `ArtifactExportIn.target_classification_floor` **必填** | contracts:133-144 | doc 08 §10 | 查無基礎(同 A12b/E10) | 是:必填 → 呼叫端一定要決定填什麼 | **C ★** | converge |
| F5 | `ArtifactDetailOut` 巢狀回 versions + exports | contracts:252-256 | doc 09 | 隨 A6/A12 | 低 | **B** | keep + mark-debt(隨版本表收斂而縮) |
| F6 | 13 個 request/response model 的整體規模 | contracts:55-257 | doc 09 | 隨底層構造 | 中(契約面要跟 6 表同步演進) | — | 隨收斂縮至 ~5 個 |

### 1.7 `migrations/versions/r1_0007_artifact_contract.py`

| # | 構造 | file:line | doc 出處 | Q1 證據 | Q2 成本 | 類 | 建議 |
|---|---|---|---|---|---|---|---|
| G1 | 4 表 / 64 欄 / 10 索引的建置 | migration:46-227 | doc 01/02/08 | 逐表判定見 §1.1 | **收斂本身有成本**:需新 down-revision migration 落實移除(不可回頭改 r1_0007,權威文件已入庫 `faa2ac3`) | 隨表 | converge 時開新 revision;`.15` 資料可刪(L323「**沒有知識庫,全部可刪** → 資料庫可以砍掉重來」)降低遷移風險 |
| G2 | FK 環避免的建表順序(artifacts → versions → exports → jobs) | migration:20-23,181 | 技術限制 | 純技術,無規格面 | 低 | **B** | keep(若 A11 收斂,環本身消失) |
| G3 | `_UNCLASSIFIED = "無機密"` server_default | migration:43,81,119,166 | doc 08 | 與 L232「無機密 / 營業秘密 / 密 / 機密」相容 | 低 | **B** | keep;**OE-3** 對齊四級時此預設值不變、無需改 |
| G4 | 5 條 trace/job 相關索引(`ix_artifacts_trace_id`、`ix_artifact_jobs_*`) | migration:96-97,222-227 | doc 02 | 隨 A7/A11(L211 反向明文) | 是 | **C** | converge |

### 1.8 `services/anila-studio/app/services/job_store.py`

| # | 構造 | file:line | doc 出處 | Q1 證據 | Q2 成本 | 類 | 建議 |
|---|---|---|---|---|---|---|---|
| H1 | **Redis 持久 job store 整體**(restart 存活) | job_store.py:1-31,106-188 | doc 02 §8 failure model、doc 02 §1 topology | 查無基礎(已搜:重啟、restart、job、Redis、佇列 → 全文 0 命中)。唯一理由是 doc 02「Studio restart → job 不應丟失」 | 是:Redis 依賴 + 每次狀態轉移一次寫入 + **與 CSP `artifact_jobs` 表對同一份 job 重複記帳** | **C ★** | converge:**兩份持久 job 記錄至少砍一份**。若擁有者仍要 restart 存活,留 Redis 這份(便宜、就地),砍 CSP 那份(A11) |
| H2 | `PersistedJob` 的 CSP 面欄位(`task_id` / `source_snapshot_id` / `classification_level` / `artifact_id` / `trace_id`) | job_store.py:77-82 | doc 02 | 查無基礎(隨 A4/A7/A11) | 是:17 欄投影要與各 pipeline `to_status()` 同步 | **C** | converge(附隨) |
| H3 | best-effort 語意:`put_quietly` 吞例外、store 未啟動時 no-op、`from_json` 忽略未知鍵 | job_store.py:26-29,101-103,167-176 | — | 查無基礎(工程品質選擇) | 低,且**降低**維運風險(Redis 故障不會炸生成) | **B** | keep(若 H1 保留);符合 L13-15「一個人維運」精神 |
| H4 | TTL 7 天 | job_store.py:18-19,151;config.py:78 | doc 02 | **與規格分歧**。L167「設時限後自動刪(**暫定 3 天**)」 | 低,但方向錯 | **B** | keep + mark-debt:數值對齊 3 天(或由擁有者裁定) |
| H5 | 模組級 singleton + lifespan start/stop | job_store.py:49-51,123-139,179-188 | — | 查無基礎(工程結構) | 低 | **B** | keep(隨 H1) |

### 1.9 `services/anila-studio/app/config.py`

| # | 構造 | file:line | doc 出處 | Q1 證據 | Q2 成本 | 類 | 建議 |
|---|---|---|---|---|---|---|---|
| I1 | `ARTIFACTS_DIR`(產出落地根目錄) | config.py:59-62 | doc(Slice 8b) | **有**。L167「暫存**伺服器**」 | 需維護 | **A** | keep + 改引 L167;**必須配一個 3 天保留的清理任務**(見 G-1) |
| I2 | `JOB_STORE_KEY_PREFIX` / `JOB_STORE_TTL_SECONDS` | config.py:70-78 | doc 02 §1/§8 | 查無基礎(隨 H1);TTL 值與 L167 分歧(H4) | 隨 H1 | **C** | converge(附隨 H1) |
| I3 | `STUDIO_ARTIFACT_REPORTING`(向 CSP 回報 job/artifact/span 的總開關) | config.py:80-86 | doc 02/09/10 | 查無基礎(隨 A11/A7) | 是:整條 fire-and-forget 回報通道(`job_reporting.py` + `job_lifecycle.py` 三大關注點之二) | **C ★** | converge:回報通道隨 CSP `artifact_jobs` 一起收斂,只留「產出登錄」一次 POST |
| I4 | `CSP_SERVICE_TOKEN` 預設空字串(無硬編碼祕密) | config.py:26-27 | — | **專案安全紅線**(祕密零硬編碼) | — | **A** | keep(已符合;PUBLIC repo 無外洩) |

---

## 2. CAT-C 收斂候選排序(依「拆掉省下的維運面積 ÷ 拆解風險」)

| 序 | 構造 | 一句話理由 | 影響面 |
|---|---|---|---|
| 1 | `artifact_jobs` 表 + 2 端點 + 狀態機 + Studio 回報通道(A11/D1/E6/E7/I3) | 規格從沒要 job 持久化;且同一份 job 已在 Redis 存一份 —— **重複記帳** | 19 欄 + 2 端點 + 1 狀態機 + 1 回報器 |
| 2 | binding 規則「必綁 task/snapshot」(A4/B2/D4/E4) | 規格說 Studio 輸入是**知識庫**(L356),不是 Task;現況等於「ALM 不先建 Task,產出登錄就靜默失效」 | 1 條硬 422 + 跨 3 檔 |
| 3 | `artifact_versions` 表 + versions 端點(A6/D5/E9/A10e) | L358 明言不需要網頁編輯器 → 伺服器端沒有版本史要留 | 12 欄 + 1 表 + 1 端點 + 1 死函式 |
| 4 | 匯出判定式 target_floor 模型(E10/A12b/F4) | 規格的匯出規則是密等兩條線(L241-242),不是目的地空間比較 | 判定式 + 2 欄 + 1 必填欄;**與 OE-4 合併執行** |
| 5 | `trace_id` 三欄 + 5 索引(A7/A13/G4) | L211 逐字寫「**不需要…trace id**」——本域是該句話的直接違例 | 3 欄 + 2 索引 |
| 6 | `citations` 表 + `citation_map`(B5/A10c) | 「引用」在 SYSTEM-MAP 全文 0 命中;字元位移級精度無人要求 | 12 欄 + 1 表(**跨領域**) |
| 7 | `source_snapshots` 表(B1/B6/B7) | 「快照」全文 0 命中;僅為支撐 A4 的 binding 而存在 | 16 欄 + 1 表(**跨領域**) |
| 8 | `mindmap` 作為 artifact 型別(A2b) | L360 逐字說心智圖**不是產出物** | 1 個 enum 值 + 一條 pipeline 的定位 |
| 9 | Redis job store 的 CSP 面欄位(H1/H2/I2) | 若第 1 項採「留 Redis、砍 CSP 表」,則跨面欄位一併消失 | 5 欄投影 |
| 10 | module independence 契約(C1) | 無工具強制的分層儀式,對一人維運是純負債(L13-15) | docstring 規則 + 1 個被迫外移的 orchestrator |

---

## 3. 已在別處處置、本稽核不重複的相鄰觀察

- **OE-1(agent 七態機 → 三態)**:本域 `ArtifactJobStatus` 四態機(D1)是同型病灶,且**終態零出邊使冪等重報被 409 拒絕**,建議與 OE-1 同批處理。
- **OE-3(五級 → 四級)**:本域 `ClassificationLevel` 只是消費端(`contracts/classification.py` 定義五級含「極機密/絕對機密」)。**衝擊點:`tests/test_artifact_contract.py` 有 5 處硬編碼「極機密」(L249,251,256,302,305),OE-3 對齊四級後這些測試會直接紅。**
- **OE-4(門檻兩條線)**:本域匯出判定式(E10)不只是門檻不同,是**判定軸不同**(目的地空間下限 vs 密等門檻)。**衝擊點:`test_export_allow_records_decision_and_row`(L312-329)以「artifact=機密、floor=機密 → allow」為斷言,而規格 L235 明訂機密「不可」匯出 —— 該測試在對齊後語意必須翻轉。**

---

## 4. 反向發現:規格有要求、但本域沒做(缺口)

> 這些不是過度設計,是**欠缺**。放進報告是因為「converge 判決」不能只砍不補。

| # | 缺口 | 規格依據 | 現況 |
|---|---|---|---|
| **G-1** | **產出到期自動刪完全未實作** | L167「設時限後自動刪(**暫定 3 天**)」 | CSP 三表(`artifacts` / `artifact_versions` / `export_records`)**無任何到期欄位**;`artifact_jobs.expires_at`(model:200)只寫不讀(service.py:137 寫入,全 repo 無讀取點、無 reaper);Studio 端只有各 pipeline 60 分鐘 in-memory stale 逐出、Redis 7 天 TTL(config:78)。**沒有一處是 3 天。** 收斂後應保留的最小形狀就是「`expires_at` + 一個刪檔任務」 |
| **G-2** | **讀取受控產出未落稽核** | L255「2. **每次讀取落稽核**」;L275「記什麼 \| **讀取受控文件**/對話…」;L238「落稽核 = 密等 ≥ 營業秘密」 | `GET /api/artifacts/{id}`(api:436-453)讀出 `機密` 級 artifact **不寫任何稽核列**;本域唯一的稽核寫入是匯出時的 `PolicyDecision`(api:374-389) |
| **G-3** | **產出未套浮水印** | L254「1. **全頁浮水印** —— 印「本文件屬營業秘密 · 讀取者 · 時間」」 | 本檔案集內無浮水印相關構造(可能在其他域/前端;僅記錄,不主張本域缺陷) |
| **G-4** | **admin 全看與「產它的人」不一致** | L167「\| 產它的人 \|」 | `list_artifacts` / `ensure_artifact_access` 對 admin tier 全開(service.py:315,318,338)。屬擁有者裁決題,非缺陷 |

---

## 5. 測試面:`tests/test_artifact_contract.py` 鎖住了哪些「規格上不存在」的構造

全檔 25 個測試、7 個測試類。**其中 18 個測試鎖定的是規格查無基礎的構造**——這代表收斂時測試會成為阻力,必須同批修改。

| 測試(class::test) | 行 | 鎖住的構造 | 該構造分類 |
|---|---|---|---|
| `TestJobContract::test_register_job_idempotent_upsert` | 101-114 | `artifact_jobs` 表 + 冪等 upsert | **C**(A11/D2) |
| `TestJobContract::test_register_job_resolves_employee_id` | 116-126 | 員編解析 + jobs 表 | B/**C** |
| `TestJobContract::test_job_missing_requester_422` | 128-131 | `_require_requester` | **A**(F2) |
| `TestJobContract::test_job_status_transitions` | 133-147 | 四態狀態機 + progress | **C**(D1/F3) |
| `TestJobContract::test_illegal_transition_from_terminal_409` | 149-158 | **終態零出邊 → 409** | **C**(D1) |
| `TestJobContract::test_unknown_status_value_422` | 160-166 | 封閉 enum 值域 | **C**(D1) |
| `TestJobContract::test_patch_unknown_job_404` | 168-172 | jobs 表查無語意 | **C**(A11) |
| `TestServiceTokenOnly::*`(4 個) | 178-203 | service-token-only 閘(403/401) | **A**(E1)— 保留 |
| `TestBindingRule::test_artifact_requires_task_or_snapshot_422` | 210-214 | **binding 硬 422** | **C ★**(A4) |
| `TestBindingRule::test_artifact_unknown_task_404` | 216-219 | `_resolve_binding` | **C**(E4) |
| `TestClassificationInheritance::test_task_level_wins_when_explicit_lower` | 226-243 | 從 **Task** 繼承分類 + ClassificationEvent | **A**(閂鎖 E5)/ **C**(來源=Task,A4) |
| `TestClassificationInheritance::test_explicit_higher_is_one_way` | 245-256 | 單向不降級 + 首版同步 | **A**(E5)。⚠ 用「極機密」——**OE-3 後會紅** |
| `TestClassificationInheritance::test_snapshot_only_binding_inherits` | 258-268 | 從 **SourceSnapshot** 繼承 | **C**(B1/A4) |
| `TestVersioning::test_add_version_increments` | 275-289 | 版本遞增 + `current_version` | **C ★**(A6/E9) |
| `TestVersioning::test_version_reinherits_higher_explicit` | 291-305 | 版本端點 + 再閂鎖 | **C**(E9)。⚠ 用「極機密」——**OE-3 後會紅** |
| `TestExportGate::test_export_allow_records_decision_and_row` | 312-329 | **target_floor >= level 判定式** | **C ★**(E10)。⚠ 斷言「artifact=機密 + floor=機密 → allow」**與規格 L235「機密不可匯出」直接相反,OE-4 後語意必須翻轉** |
| `TestExportGate::test_export_deny_403_no_row` | 331-348 | deny 不落 allow 列 + deny 必附原因 | **A**(E11);但案例用「營業秘密 → deny」,規格 L236「營業秘密**可以**分享/匯出」——**理由對、案例錯** |
| `TestExportGate::test_export_via_user_jwt_allow` | 350-361 | 雙軌認證匯出 | **C**(E3) |
| `TestGovernanceReads::test_owner_lists_and_reads_detail` | 372-381 | **owner-scope 讀取** | **A ★**(D9)— 保留 |
| `TestGovernanceReads::test_foreign_user_403_and_not_listed` | 383-390 | 非擁有者 403 且不列出 | **A ★**(D9)— 保留 |
| `TestGovernanceReads::test_admin_sees_all` | 392-399 | **admin 全看 bypass** | **B**(D10)— 規格無基礎,擁有者裁決 |
| `TestGovernanceReads::test_filter_by_artifact_type` | 401-409 | type 過濾 | **B** |

補充:測試檔頂端 `SVC_TOKEN = "svc-slice8a-test-token"`(L43)是 monkeypatch 的測試夾具值(L47-52,function-scoped),**非真實祕密、無外洩**。

---

## 6. 逐檔交代(含「無可稽核內容」聲明)

| 檔案 | 是否有可稽核構造 | 說明 |
|---|---|---|
| `services/csp/app/models/artifact.py` | ✅ 有(19 構造) | 契約主體;4 表中的 3 表定義於此 |
| `services/csp/app/models/source_snapshot.py` | ✅ 有(7 構造) | **跨領域**:`modules/tasks/service.py:184` 亦為寫入者,收斂需合議 |
| `services/csp/app/modules/artifacts/__init__.py` | ✅ 有(2 構造) | 主要是 independence 契約(C1)與 11 函式 re-export 面;非空檔 |
| `services/csp/app/modules/artifacts/service.py` | ✅ 有(12 構造) | 含一個死函式(D7) |
| `services/csp/app/api/artifacts.py` | ✅ 有(13 構造) | 6 端點 + 3 個認證/閘門構造 |
| `services/csp/app/schemas/contracts/artifacts.py` | ✅ 有(6 構造群) | 16 型別;多數為上述構造的形狀鏡射 |
| `services/csp/tests/test_artifact_contract.py` | ✅ 有(見 §5) | 25 測試,18 個鎖規格外構造 |
| `services/csp/migrations/versions/r1_0007_artifact_contract.py` | ✅ 有(4 構造群) | 與 models 一一對應;收斂需新 down-revision |
| `services/anila-studio/app/services/job_store.py` | ✅ 有(5 構造) | 全檔服務於「restart 不丟 job」這個規格外目標 |
| `services/anila-studio/app/config.py` | ⚠ **部分** | 本域相關:I1–I4。**以下設定不是 doc 衍生的 artifact 構造、不屬本域爭點,未予分類**:`APP_NAME`/`APP_VERSION`/`LOG_LEVEL`(L18-20)、`CSP_BASE_URL`(L24)、`FLUX_BACKEND_URL`(L30)、`RENDERER_BASE_URL`(L33)、`REDIS_URL`/`REDIS_REVOCATION_CHANNEL`(L36-37)、`JWT_*`(L40-43)、`INTERNAL_*_TIMEOUT*`(L46-54)、`FLUX_CACHE_DIR`(L57)、`JWKS_REFRESH_SECONDS`/`REVOCATION_CACHE_TTL_SECONDS`(L66-68)——分屬登入/生圖/撤銷快取等其他領域 |

**沒有任何一個指派檔案是「零可稽核構造」。**

---

## 7. 執行注意事項(給收斂者)

1. **不要單邊拆 `source_snapshots` / `citations`。** 寫入者在 tasks 域(`modules/tasks/service.py:184`),讀取者散在 policy / launch / proxy / classification_inventory。本報告只給規格基礎判定。
2. **收斂順序必須是「先補 G-1 到期刪除,再拆表」。** 否則會拆掉唯一帶 `expires_at` 的表(`artifact_jobs`),把規格唯一的具體要求連根拔掉。
3. **E10(匯出判定式)與 OE-4 合併執行。** 分開做會做兩次:OE-4 改門檻值,本項改判定軸;先改值再改軸等於白改一次。
4. **`.15` 資料可刪**(L323「沒有知識庫,全部可刪 → 資料庫可以砍掉重來」),遷移風險低;但 r1_0007 已入庫,收斂需開新 revision 而非改舊檔。
5. **E1(service-token 閘)、E5(單向閂鎖)、D9(owner-scope)三者不得因「帶 doc 引用」而被誤判為過度設計** —— 這正是本專案交接時記錄過的判讀錯誤模式。
