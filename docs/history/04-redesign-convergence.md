# 時代 D：anila-redesign 收斂（本分支）

> 分支：`anila-redesign`（tip `24a0455`，領先 `origin/prod-intranet-card` 共 44 個 commit，尚未 push）
> 基線：`origin/prod-intranet-card`（v1.2.0 系）
> 期間：2026-07-02（單日一次性衝刺；範圍內 44 個 commit 日期全為 2026-07-02）
> 取證基準：git log（`origin/prod-intranet-card..HEAD`）為主，`docs/anila-redesign-docs/`、`docs/superpowers/plans/2026-07-02-anila-redesign-master.md`、`docs/audits/anila-redesign-docs-review-2026-07-02.md` 為輔。

## 概述

時代 D 是 ANILA 從「多個成熟子系統拼裝」收斂為「單一產品、以任務為入口、以 CSP 治理為底座」的一次結構性重構。它先立**憲法**（`docs/anila-redesign-docs/` 的 11+1 份設計文件，凍結為 `architecture-baseline-v0.2`），再依一份 Slice 0–9 主計畫逐片落地：目錄大搬遷（1103 檔純 `git mv`）→ god-module 拆分與模組邊界 lint → Task/分類/Trace 三支 P0 主脊椎 → Agent/Model/Service 三張 P1 註冊表 → Studio artifact 契約與 Shell 四入口 IA → 全繁體中文語言政策 lint → 官方藍視覺重設計。全程守一條紀律：**不弱化任何安全不變量（卡登 SSO、RS256 JWT/JWKS、revocation、CSRF、RLS、SSRF guard、單向分類閂鎖），改 schema 必附 `r1_` 命名空間的 Alembic migration，測試以「不新增紅字」為 gate**。因 ADR-0006 記載的佈局變更，本分支與 `main`／7 分支模型的 cherry-pick 互通已刻意中斷——這是一條全新基線，而非又一個 delta 分支。

## 里程碑敘事

### 1. 憲法與主計畫入庫（Slice 0）

`094b653` 一次帶入 17 個檔案、6437 行：doc `00-product-constitution.md`（產品憲法）加上 11 份夥伴文件（domain model、system architecture、CSP 治理控制面、model gateway、agent registry 與 runtime 協定、OpenWebUI agent 遷移、註冊式 GUI service 平台、機敏分類與 policy engine、API/event 契約、遷移與開發護欄、前端繁中語言政策），外加 `adr/ADR-template.md`、`codex-deep-audit-prompt.md` 與對碼複核報告。憲法定義了「唯一產品入口 = ANILA（任務中心／我的知識庫／產出中心／專案入口）；治理中心是 Admin-facing 控制面」、五級分類（無機密 < 營業秘密 < 機密 < 極機密 < 絕對機密）、以及 §5 功能准入合約與 §6 凍結清單。這批文件先經一輪對碼複核（344 條宣稱、85% 確認）並套用 6 個 blocking issue 修訂，才凍結為 `architecture-baseline-v0.2` freeze candidate。緊接著 `d1bd223` 落下 Slice 0–9 的主實作計畫，把重構切成可逐一驗收的片段，並釘死測試基線（backend pytest 315 passed / 45 failed / 12 errors，全為既有；ANILA_UI vitest 153 全過）作為 regression 判準。

### 2. §17.1 目錄大搬遷（Slice 1A）

`b5c5e32` 是本時代體感最大的一筆：**1103 個檔案變更、內容近乎零改寫（18 insertions / 328 deletions，其餘全是 rename）**，把舊的 `myCSPPlatform/`、`ANILA_UI/`、`ANILALM/`、`models/` 佈局搬成 `services/`、`apps/`、`packages/`、`infra/` 的目標樹。純搬移之後才動路徑：`ce7fe65` 以 file-relative 方式重錨 compose 並新增 repo 根 `compose.yaml` / `compose.dev.yaml`（`include:` 指向 `infra/compose/*`）作為 shim，保住「repo 根 `docker compose up -d`」與 `.env` 錨點；`a52cf5c` 改寫 Dockerfile COPY 路徑並補 root `.dockerignore`；`8e1f563` 修部署腳本路徑；`7cd2d10` 掃修全 repo 文件路徑並落下 **ADR-0006**。ADR-0006 記錄了搬遷偵察發現、§17.1 表上未載明的 6 項現場落差（D1–D6：過時的 `models/docker-compose.yml` 路徑、模型權重不搬、`init_db.py` 併入、certs 遷 `infra/nginx/certs/`、刪除已死的獨立 CSP 舊部署等）與 ALM 過渡處置，並明述其代價：**與 main／7 分支的 cherry-pick 互通中斷，內網離線 bundle 須以本分支重打包**。

### 3. CSP 骨架整理：god-module 拆分＋模組邊界（Slice 1B）

`7487cea` 先立 Slice 1B 計畫，隨後三筆行為零變更的拆分把三個 god-module 化整為零：`3372eee` 拆 `api/agents.py`（1384 行 → 7 檔 package）、`9f4a072` 拆 `api/auth.py`（837 行 → 7 檔，**卡登端點逐位元組相同**）、`e51b052` 拆 `services/proxy_service.py`（893 行 → 6 檔 + 薄 facade）。`3de7926` 建立 `app/modules/`（`tasks`／`policy`／`launch` 三個邊界骨架）、`ClassificationLevel` 契約，並接上 `lint-boundaries` CI gate——用 import-linter 禁止跨模組直接引用內部實作，把「Task/Policy/Launch 先在 CSP service 內以模組邊界隔離、不拆獨立服務」的 MVP 決策變成可強制執行的護欄。

### 4. P0 三支柱：Task 主脊椎、五級分類、Full Trace（Slice 2–4）

**Slice 2（Task）**：`3fae7e3` 以 `r1_0001` 落下 Task/Trace/Policy 六表 schema 基礎；`fa3eb60` 建 Task Service（`/api/tasks` + 十值狀態機 + snapshot 三規則）、`284117b` 建 Policy 決策記錄模組（規則引擎留待 Slice 3/6）、`38f0d7d` 讓對話首次送出即建 Task 並附 `X-ANILA-Task-Id`、`265d6ee` 讓 `/v1` chat 接受該 header 並對無 task_id 的舊流量標記 `legacy`（`r1_0002`）。至此「任務為入口」有了資料骨幹。

**Slice 3（分類）**：`3d01a02` 以 `r1_0003` 把 boolean latch 升級為五級分類 schema 並落下單向閂鎖核心；`646faea` 補降級流程／權責指派 API 與 runtime 閂鎖五級化；`baece9b` 出分類盤點報表與五級 level badge。此處採憲法拍板的**變體 A**：申請人≠核准人、系統內無自我核准碼路徑、核准權與平台角色脫鉤（無權責者 fail-closed 維持 pending）、backfill 以 floor 對映（`classified=true → 機密`）。

**Slice 4（Full Trace）**：`704d6e2` 落 trace ingest/查詢端點與 proxy 自發 spans；`eb0af95` 建 `anila_trace_sdk`、接上 Router dispatch spans 與 `anila.spans` SSE producer；`bf4a15d` 讓 agent 模板原生產出 full trace（`anila_agent/tracing.py`）；`3db8f6e` 把 ANILA UI 的 Trace Explorer（SpanTree）接上真資料。這一路把 ingest / SDK / Router / agent / Explorer 五個環節串成一條可追溯鏈，落實 ADR-0004「Full Trace 是正式 Agent 的最低要求」。

### 5. P1 三張註冊表：Agent、Model、Service（Slice 5–7）

**Slice 5（Agent Registry）**：`96836b2` 以 `r1_0004` 升級 agents 表（`runtime_type` 五值、`audit_level`、`classification_ceiling`、`approval_status` 七值 enum）並加上 **trace-test 審批閘門**——agent 未通過 trace-test 不得 approved；`49b0310` 讓治理 UI 的 Agent 精靈／審批走七狀態化；`652c617` 補 LangChain / custom-HTTP 的 trace adapter 範例與 CLI 註冊旗標。

**Slice 6（Model Gateway）**：`2317161` 以 `r1_0005` 強化 ModelEndpoint（protocol／api_key_secret_ref／classification_ceiling／owner_department 等）並做**旗標分域**——正式模型端點硬性 HTTPS + API Key，`ANILA_ALLOW_HTTP_ENDPOINT` 不再適用於 model 路徑（新增 agent 專用旗標）；`f1dea75` 讓治理 UI 出模型五態 health 與金鑰／上限欄位。此 slice 附 **ADR-0007**：因 doc 04 §2 與 §11 對 `allowed_task_types` 自相矛盾，該欄位暫緩、待規格拍板再以獨立 migration 補上。

**Slice 7（Service Registry）**：`7f3c8bb` 以 `r1_0006` 建 Service Registry、Launch Gateway 與 launch token 簽發／驗證、audit callback 收取端點；`50bc6b0` 在 shell 加專案入口、治理面原地升級 `platform_links`；`c1be616` 補上 7b 漏列的 Sidebar 專案入口進入點。隨後一筆安全修補值得單記：`20bcbe5` 以 `r1_0008` 把 audit-callback **綁定 Service Client、預設拒絕（fail-closed）**——原實作只驗「是一把合法 Service Client Token」卻未驗它是否屬於路徑上的 `service_id`，任何持任一把整合金鑰者都能污染他服務的稽核紀錄。**ADR-0008** 把這個弱點定性為 doc-07 schema 缺 client↔service 綁定欄位的文件層級缺口，決策為 admin-tier 專屬綁定、`db_editable_fields` 白名單不得覆寫此紅線，兩種拒絕都落稽核。此筆為審查發現並修復，是驗證機制起作用的實證。

### 6. Artifact 契約與 Shell 四入口、繁中政策（Slice 8–9）

**Slice 8（Artifact）**：`7600020` 以 `r1_0007` 建 Artifact Contract 四表 + API + 分類繼承 + export 閘；`51a5d55` 把 Studio 五類 job 從 process memory 改為 **persisted job store**（crash/restart 不丟 job metadata），並讓 CSP 回報 artifact、ALM 綁定 task。

**Slice 9（Shell IA + 繁中）**：`7f9e2ed` 落 ANILA Shell 四入口 IA（憲法 §2）；`77ac0d9` 建 zh-TW 語言政策 lint gate 並清零全 repo 大陸用語（core）；`fb2a41c` 起步把治理 UI 表層中文化（CSP 治理中心原為最大英文改造面）。

### 7. 乾淨部署 migration 實跑抓修

`f5c36b0` 是一筆務實的收尾修補：對乾淨 DB 實際跑 `alembic upgrade head` 時抓出 **2 個 P0 部署阻斷**——`r1_0005` / `r1_0006` 對全新資料庫的冪等性不足、與 `create_all` 之間存在欄位漂移——並補齊。這正是主計畫收尾 gate「migration up-down 測試（乾淨 DB upgrade + 逐級 downgrade）」的實跑產物，也印證「端到端實測、別只看綠燈」的紀律。

### 8. 官方藍視覺重設計（裁決 #1 / #3）

`c672780` 以**真分類浮水印**取代假的英文 `CONFIDENTIAL` 角標（裁決 #1）——依實際 `classification_level` 顯示真級別中文（機密／極機密／絕對機密），無機密不顯示，缺欄位回退「機密」floor，消除裝飾語彙與 Slice 3 真分類之間的語義混淆。`1214cd6` 是**去駭客風視覺重設計**（裁決 #3）：把治理中心從「終端機／碳黑＋霓虹綠」美學翻為官方藍 institutional console——重寫 `tokens.css`（淺色優先、官方藍 accent、冷灰中性、溫圓角）、字體從等寬翻為 sans（等寬只留給 ID/trace_id/數值等資料語境）、移除登入頁開機 log 動畫與 cosplay 元素，並把登入頁改為**卡登優先**（自然人憑證卡 hero，帳密／SSO 收底部），卡登安全路徑逐字保留只改版型。`6a6440a` 隨後還原淺色語意預設與 dev 代理。此milestone另有 doc `12-frontend-visual-redesign.md` 隨 `1214cd6` 入庫。

### 9. README 藝廊與全面重寫

`ea571c4` 加入重設計後的介面截圖藝廊（官方藍・卡登優先・真浮水印）。`24a0455` 是本時代最後一筆、也是今日的 README 艦隊：以 7 個 Opus max agent 對碼重寫全子專案與主 README（30 檔、2851 insertions / 1518 deletions），讓文件對齊搬遷後的 `services/`、`apps/`、`infra/` 佈局與新契約。

### 10. 驗證機制（baseline-vs-regression 紀律、airgap 不變量審查 PASS）

整個時代守兩條驗證紀律。其一是 **baseline-vs-regression**：搬遷前先釘死測試基線（315 passed / 45 failed / 12 errors，全為既有），之後每步以「不新增紅字」為 gate，並在複核時以 `git show` 對 `origin` 逐項驗證分支特定事實，把既有失敗與真 regression 分清（複核全程唯讀、未動分支、未碰運行中容器）。其二是 **airgap 不變量審查**：每份 ADR 都附「不弱化安全不變量（card SSO / JWT / CSRF / RLS / SSRF guard / 單向閂鎖）」檢核；主計畫的收尾 gate 明列 airgap-invariant-reviewer 對全 diff 的安全紅線審查。該審查的具體成效有實證——它（或同輪 commit security review）surfacing 了 audit-callback 跨服務污染弱點（`20bcbe5` / ADR-0008，淨強化為 fail-closed），乾淨部署 migration 實跑又抓修 2 個 P0（`f5c36b0`）；最終全 diff 對安全不變量的審查判為 PASS（無弱化、僅淨強化）。

## 完整 commit 對照表

| # | hash | 日期 | 摘要 |
|---|---|---|---|
| 1 | `094b653` | 2026-07-02 | docs：Slice 0 — architecture-baseline-v0.2 設計文件 + ADR 範本 + PR checklist |
| 2 | `d1bd223` | 2026-07-02 | docs：Slice 0–9 主實作計畫 |
| 3 | `b5c5e32` | 2026-07-02 | refactor：Slice 1A-1 — §17.1 目錄大搬遷（純 `git mv`，1103 檔、無內容改寫） |
| 4 | `a52cf5c` | 2026-07-02 | refactor：Slice 1A-3 — Dockerfile COPY 路徑改寫 + root `.dockerignore` |
| 5 | `ce7fe65` | 2026-07-02 | refactor：Slice 1A-2 — compose 路徑重錨（file-relative）+ root include shim |
| 6 | `8e1f563` | 2026-07-02 | refactor：Slice 1A-4 — 部署腳本路徑改寫 |
| 7 | `7cd2d10` | 2026-07-02 | docs：Slice 1A-7 — 全 repo 文件路徑掃修 + ADR-0006 |
| 8 | `7487cea` | 2026-07-02 | docs：Slice 1B 實作計畫（god-module 拆分／modules 邊界／contracts） |
| 9 | `3de7926` | 2026-07-02 | feat：Slice 1B-4 — modules 邊界骨架 + `ClassificationLevel` 契約 + lint-boundaries gate |
| 10 | `3372eee` | 2026-07-02 | refactor：Slice 1B-1 — `api/agents` god-module（1384L → 7 檔 package）拆分，行為零變更 |
| 11 | `9f4a072` | 2026-07-02 | refactor：Slice 1B-2 — `api/auth` god-module（837L → 7 檔）拆分，卡登端點逐位元組相同 |
| 12 | `e51b052` | 2026-07-02 | refactor：Slice 1B-3 — `services/proxy_service`（893L → 6 檔）拆分 + 薄 facade |
| 13 | `3fae7e3` | 2026-07-02 | feat：Slice 2a — Task/Trace/Policy 六表 schema 基礎（`r1_0001`） |
| 14 | `38f0d7d` | 2026-07-02 | feat：Slice 2b-D — 對話首次送出即建 Task 並附 `X-ANILA-Task-Id` |
| 15 | `284117b` | 2026-07-02 | feat：Slice 2b-B — Policy 模組（決策記錄面；規則引擎留 Slice 3/6） |
| 16 | `fa3eb60` | 2026-07-02 | feat：Slice 2b-A — Task Service 模組（`/api/tasks` + 十值狀態機 + snapshot 三規則） |
| 17 | `265d6ee` | 2026-07-02 | feat：Slice 2b-C — `/v1` chat 接受 `X-ANILA-Task-Id` + legacy 標記（`r1_0002`） |
| 18 | `3d01a02` | 2026-07-02 | feat：Slice 3a — 五級分類 schema 升級 + 單向閂鎖核心（`r1_0003`） |
| 19 | `baece9b` | 2026-07-02 | feat：Slice 3c — 分類盤點報表 + 五級 level badge |
| 20 | `646faea` | 2026-07-02 | feat：Slice 3b — 降級流程／權責指派 API + runtime 閂鎖五級化 |
| 21 | `3db8f6e` | 2026-07-02 | feat：Slice 4d — Trace Explorer（SpanTree 接真資料） |
| 22 | `bf4a15d` | 2026-07-02 | feat：Slice 4c — 模板 native Full Trace（`anila_agent/tracing.py`） |
| 23 | `704d6e2` | 2026-07-02 | feat：Slice 4a — trace ingest/查詢端點 + proxy 自發 spans |
| 24 | `eb0af95` | 2026-07-02 | feat：Slice 4b — `anila_trace_sdk` + Router dispatch spans + `anila.spans` SSE producer |
| 25 | `652c617` | 2026-07-02 | feat：Slice 5c — trace adapter 範例（LangChain／custom-HTTP）+ CLI 註冊旗標 |
| 26 | `96836b2` | 2026-07-02 | feat：Slice 5a — Agent Registry 升級（`r1_0004`）+ trace-test 審批閘門 |
| 27 | `49b0310` | 2026-07-02 | feat：Slice 5b — Agent 精靈／審批 UI 七狀態化 |
| 28 | `2317161` | 2026-07-02 | feat：Slice 6a — Model Gateway 強化（`r1_0005`）+ 旗標分域 + 部署接線 |
| 29 | `f1dea75` | 2026-07-02 | feat：Slice 6b — 模型治理五態 health + 金鑰／上限欄位 |
| 30 | `50bc6b0` | 2026-07-02 | feat：Slice 7b — 專案入口（shell）+ Service Registry 治理面（原地升級） |
| 31 | `7f3c8bb` | 2026-07-02 | feat：Slice 7a — Service Registry（`r1_0006`）+ Launch Gateway + audit callback |
| 32 | `c1be616` | 2026-07-02 | fix：Slice 7b 補遺 — Sidebar 專案入口進入點（`chat.jsx`，7b 漏列檔案） |
| 33 | `7600020` | 2026-07-02 | feat：Slice 8a — Artifact Contract（`r1_0007`）四表 + API + 分類繼承 + export 閘 |
| 34 | `51a5d55` | 2026-07-02 | feat：Slice 8b — persisted job store + CSP artifact 回報 + ALM task 綁定 |
| 35 | `20bcbe5` | 2026-07-02 | fix(security)：audit callback client↔service 綁定 fail-closed（`r1_0008`，審查發現） |
| 36 | `7f9e2ed` | 2026-07-02 | feat：Slice 9a — ANILA Shell 四入口 IA（憲法 §2） |
| 37 | `77ac0d9` | 2026-07-02 | feat(ci)：Slice 9b — zh-TW 語言政策 lint gate + 全 repo 大陸用語清零 |
| 38 | `fb2a41c` | 2026-07-02 | feat：Slice 9b 延伸 — 治理 UI 表層中文化（P-CSP 起步） |
| 39 | `f5c36b0` | 2026-07-02 | fix(migrations)：修乾淨 DB 部署阻斷 — `r1_0005`/`r1_0006` 冪等補齊 create_all 漂移欄 |
| 40 | `c672780` | 2026-07-02 | feat：真分類浮水印取代假 CONFIDENTIAL（裁決 #1） |
| 41 | `1214cd6` | 2026-07-02 | feat：去駭客風視覺重設計 — 官方藍 institutional console（裁決 #3；含 doc 12） |
| 42 | `6a6440a` | 2026-07-02 | fix：還原淺色語意預設 + dev 代理 |
| 43 | `ea571c4` | 2026-07-02 | docs：重設計介面截圖藝廊（官方藍・卡登優先・真浮水印） |
| 44 | `24a0455` | 2026-07-02 | docs：全子專案 + 主 README 對碼重寫（7 Opus max 艦隊） |

## 本時代結束時的系統樣貌

- **佈局**：舊 `myCSPPlatform/`＋`ANILA_UI/`＋`ANILALM/`＋`models/` 已收斂為 `services/`（csp、ingestion-worker、router…）、`apps/`（anila-shell、anilalm、csp-governance-ui）、`packages/`、`infra/`（compose、models、nginx、deployment、ci），repo 根有 `compose.yaml` / `compose.dev.yaml` shim。
- **契約**：8 支 `r1_` 命名空間 migration（`r1_0001`–`r1_0008`）落地 Task/Trace/Policy、五級分類、Agent/Model/Service 三張註冊表、Artifact 四表與 audit-callback 綁定；CSP 內以 `app/modules/{tasks,policy,launch}` 模組邊界隔離並受 lint-boundaries 強制。
- **產品**：ANILA Shell 露出四入口（任務中心／我的知識庫／產出中心／專案入口），治理中心退為 Admin-facing 控制面；正式 model/agent 走 CSP proxy、artifact 綁 task、正式任務有 trace_id。
- **安全**：五級單向閂鎖 + 變體 A 降級審批、正式模型硬 HTTPS+Key、audit-callback fail-closed 綁定；卡登 SSO / JWT / CSRF / RLS / SSRF guard 不變量全數保留。
- **視覺與語言**：官方藍 institutional console、卡登優先登入、真分類浮水印；全繁體中文（台灣用語）語言政策上 CI lint。
- **分支狀態**：`anila-redesign` 領先 `origin/prod-intranet-card` 44 個 commit、**尚未 push**；依 ADR-0006，本分支與 main／7 分支的 cherry-pick 互通刻意中斷——它是一條全新架構基線（`architecture-baseline-v0.2` freeze candidate），而非既有分支模型的又一個登入／部署 delta。

---

> **不確定與缺口標註**
> - 範圍內 44 個 commit 日期全為 `2026-07-02`；「單日衝刺」為 git 日期直述，非推斷。
> - 「11+1 份設計文件」= doc `00`（憲法）+ doc `01`–`11`（11 份夥伴文件），皆於 Slice 0（`094b653`）入庫；doc `12`（視覺重設計）較晚隨 `1214cd6` 入庫，不計入 Slice 0 的「11+1」。
> - 「airgap 不變量審查 PASS」的最終 PASS 判定依主計畫收尾 gate 與各 ADR 憲法檢核推斷；有具體實證的是審查抓出並修復的 R-SEC audit-callback 綁定（`20bcbe5`／ADR-0008）與乾淨部署 2 個 migration P0（`f5c36b0`）。
