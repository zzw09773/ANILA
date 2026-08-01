> ⚠ **2026-08-01 P2.1**：agent 派工身分已改為平台現簽的 5 分鐘 JWT（JWKS 驗簽；
> 開發者不領 `csk-`／`CSP_SERVICE_TOKEN`）。下文保留當時紀錄，**勿依此做現行接入**；
> 現行上手見 `docs/guides/developer-guide.md` 與治理中心「接入驗簽 · 三級制」。

# OE-2 §A — TRACES / TASKS 領域規格符合性稽核

- 稽核基準:`SYSTEM-MAP.md`(repo root,399 行,2026-07-29)—— 唯一規格權威。
- 稽核範圍:交辦 FILE SET 十檔(全數在 `services/csp/`)。
- 稽核方法:每個構件先在 **規格原文**(grep zh-TW 詞彙與同義詞)找依據,再看成本,最後看是否只剩舊 doc 撐著。
  **不採信程式碼自己的 docstring / doc 引用**當依據。
- 已在他處裁定、本報告**不重複列為發現**:OE-1(agent 七態機 + trace-test 硬閘 + full_trace 核准阻擋)、OE-3(五級→四級)、OE-4(外流門檻兩條線)。相鄰觀察另列末節。

---

## 0. 結論摘要

| 分類 | 數量 |
|---|---|
| CAT-A(規格要求 / 專案安全紅線;保留並改引 SYSTEM-MAP) | **2**(皆為「條件式」:守衛本身必留,但其保護對象是 CAT-C) |
| CAT-B(無規格依據、無持續成本;保留並記為技術債) | **1** |
| CAT-C(無規格依據、有持續成本、只剩舊 doc 撐著;收斂候選) | **50** |
| 無獨立可稽核構件(明列,不沉默) | 1 檔區塊(`app/modules/tasks/__init__.py` 的 re-export 段) |

50 個 CAT-C **不是 50 個獨立決策**。它們聚成 **6 個收斂決策單元**(見 §4)。

---

## 1. 領域主判(DOMAIN LEAD):span / trace / task 基礎設施有沒有第二個依據?

### 1.1 否定側(規格原文,逐字)

- **SYSTEM-MAP.md:210–211**(§7 用量與配額)
  > 要記的欄位:**時間、使用者、部門、模型、agent、token 數**。
  > → 一張表加幾個索引。**不需要 span 樹、parent 關係、trace id。**

- **SYSTEM-MAP.md:387**(§13 標題句)
  > 以下在現行程式碼裡存在,但**不對應上述任何需求**:

- **SYSTEM-MAP.md:393**(§13 清單項)
  > - agent 必須回傳 6 種 span 且單一根節點才准核准(trace-test)

- **SYSTEM-MAP.md:396–399**(§13 結語)
  > 使用者原話:「我就是想要紀錄 Agent 用量也想要可以被派工,但我不知道為什麼給你們做一做就這麼奇怪。」
  > 需求是兩句話,做出來的是一套分散式追蹤治理協定。

第 211 行是**逐項點名**:`span 樹`(= `trace_spans.parent_span_id` 重建的樹)、`parent 關係`(= `parent_span_id` 欄)、`trace id`(= `tasks.trace_id` / `trace_spans.trace_id`)。這是全份規格中對單一子系統最直接的否定。

### 1.2 主張側:逐一檢查是否有**另一個**依據(全部不成立)

| 候選依據 | 規格原文(逐字 + 行號) | 是否構成 span/trace/task 的依據 |
|---|---|---|
| §8 稽核「記什麼」 | **275**:`| 記什麼 | 讀取受控文件/對話、上傳、刪除、改密等、匯出、列印、分享、登入登出、呼叫模型/agent、管理動作 |` | **否。** 清單是**動作事件**層級,不是 span 樹。這條由 `app/models/audit_log.py`(actor/action/resource/status/created_at)承載,不是 `trace_spans`。`trace_spans` 既不在稽核查詢面,也沒有任何防竄改機制。 |
| §8 稽核 append-only | **280**:`> ⚠ **append-only 的稽核帳因此是必要的,不是過度設計。** 威脅模型包含特權內部人。` | **否。** 這句是替**稽核帳**背書,不是替 `trace_spans` 背書。`trace_spans` 允許被 admin 直接改/刪(無 append-only 保護),把它當稽核依據反而不成立。 |
| §8 稽核誰查/匯出 | **277**:`| 誰查 | admin 在後台查;**要能匯出給稽核單位**;未來可能上資安中心 SOC |` | **否,且反證。** 現行 `GET /api/traces/{trace_id}` 只能**單筆 by id** 讀,沒有 admin 查詢面、沒有匯出。若 span 真是稽核物件,它連規格的稽核操作面都不滿足。 |
| §9 告警 p95 | **305**:`| 回應時間 p95 超過門檻(門檻先量一週再定;agent 本來就久,要跟直答分開算) |` | **否。** `token_usage.request_duration_ms` 已存在且已寫入(`app/services/proxy/usage.py:55`、`app/services/usage_writer.py:63`),`usage_service.py:631` 已 `func.avg(...request_duration_ms)`;「agent 跟直答分開算」由 `caller_agent_id`(`usage_service.py:531–547`)達成。不需要 span。 |
| §9 告警 agent 派工失敗 | **303**:`| MLSteam 上某個 agent 派工失敗 |` | **否。** 全 repo 的 alert 程式碼**不讀** `TaskRun` / `trace_spans`(grep `app/services/alert*.py`、`app/models/alert.py`:零命中)。TaskRun.status=failed 不是任何告警的來源。 |
| §3 使用者要看到哪個 agent | **116**:`- **使用者要看得到是哪個 agent 在回答**,而且能自己改派` | **否。** 這要的是「回答者身分」一個欄位,不是 span 樹。唯一消費 span 樹的 UI 是 **dev-only 除錯面板**:`apps/anila-shell/src/spanTree.jsx:1–6`「Use case: development only — toggle via a dev flag so users in production don't get a debug panel they can't action on.」 |
| §7 報表要能回答部門用了哪些 agent | **216**:`| 也要掛在 agent 上嗎? | 要,兩邊都算 —— 報表要能回答「部門用了哪些 agent」 |` | **否。** 由 `token_usage.caller_agent_id` × `department_id` 達成(`usage_service.get_top_agents`)。與 task / span 無關。 |
| §4 身分要能被證明 | **150**:`**正解:短效簽章 token。** CSP 派工時簽一個 5 分鐘有效的 JWT,內含 {user_id, department, agent_id}` | **否(而且是替代方案)。** 規格要的是**簽章身分**,不是 task/trace 標頭。現行 `X-ANILA-Task-Id` / `X-ANILA-Trace-Id` 是純文字標頭,正是 **146** 行點名的問題型態。 |

**主判:沒有第二個依據。** 規格中沒有任何一節替 span 樹 / parent 關係 / trace id / Task 主脊椎背書;
搜過的詞:`span`、`trace`、`task`、`任務`、`派工`、`工作單`、`重跑`、`handoff`、`狀態機`、`policy`、`政策`、`快照`、`snapshot`、`引用`、`出處`、`citation`、`觀測`、`監控`、`診斷`、`除錯`、`冪等`。
其中 **`task` / `任務` / `狀態機` / `快照` / `引用` / `觀測` 在 399 行規格中出現次數為 0。**

### 1.3 領域主判之二:用量歸屬需不需要 task 維度?

**不需要。** 規格 **210** 行逐字列出用量要記的欄位:「**時間、使用者、部門、模型、agent、token 數**」—— 六項,**沒有 task**;
下一行(**211**)緊接著說「一張表加幾個索引」。

程式碼側的實證:
- `r1_0002` 加的 `token_usage.task_id` **是唯寫欄位** —— `app/services/usage_service.py` 全檔**零次**讀取 `TokenUsage.task_id`;分組維度只有 `model / user / department`(`usage_service.py:225–230`),另有 agent 專用聚合(`:531–547`)。
- 也就是說:規格要的六個維度 `token_usage` 在 `r1_0001` **之前就已經全部具備**(`user_id` / `department_id` / `model_id` / `caller_agent_id` / `total_tokens` / `request_timestamp`)。task 維度是**額外**的,且沒有任何報表使用它。

---

## 2. 構件總表

> 「rides with」= 本身不是獨立決策,隨父構件存亡。
> Q1 欄:規格原文逐字引用 + 行號,或「無依據(已搜:…)」。

### 2.1 `services/csp/app/models/trace_span.py`

| # | 構件 | file:line | doc ref | Q1 證據 | Q2 持續成本 | 分類 | 建議動作 |
|---|---|---|---|---|---|---|---|
| TS-01 | `trace_spans` 表(TraceSpan model) | `app/models/trace_span.py:51–58`;DDL `migrations/versions/r1_0001_task_trace_foundation.py:201–224` | doc 01 §7 / doc 05 §6 | **否定**:SYSTEM-MAP:211「→ 一張表加幾個索引。**不需要 span 樹、parent 關係、trace id。**」 | 每次 task-linked 代理呼叫寫 2 列(§2.3 PS-01);表無保留期政策(§8:271–277 只替稽核帳定半年) | **CAT-C** | converge |
| TS-02 | `span_id` + `parent_span_id`(span 樹重建) | `:62–63` | doc 01 §7 | **否定**:SYSTEM-MAP:211 逐字點名「span 樹、parent 關係」 | 樹狀維護、單根驗證邏輯(OE-1 已裁的 trace-test 即建於此) | **CAT-C** | converge(全份規格最直接的否定對象) |
| TS-03 | `trace_id` 欄 + `ix_trace_spans_trace_id` | `:61`;`r1_0001:221` | doc 01 §7 | **否定**:SYSTEM-MAP:211「不需要 … trace id」 | 索引維護;與 `tasks.trace_id`(TK-12)雙寫 | **CAT-C** | converge |
| TS-04 | `(trace_id, span_id)` 唯一索引(冪等 ingest 基礎) | `:55–58`;`r1_0001:223–224` | doc 05 §6 | 無依據(已搜:`冪等`、`idempotent`、`span`) | rides with TS-01 | **CAT-C**(rides TS-01) | converge |
| TS-05 | `task_id` FK → tasks,`ondelete=SET NULL` | `:66–69` | doc 01 §3 | 無依據(已搜:`task`、`任務` —— 規格 0 次) | rides with TS-01 + TK-01 | **CAT-C**(rides) | converge |
| TS-06 | `span_type` 開放 String(承載 13 必備型別) | `:71`(語彙定義見 TC-01) | doc 05 §6 | **否定**:SYSTEM-MAP:393「agent 必須回傳 6 種 span 且單一根節點才准核准(trace-test)」列於 387「不對應上述任何需求」之下 | agent 開發者必須照抄 13 型別語彙(`packages/anila-agent/anila_agent/tracing.py`,448 行範本) | **CAT-C** | converge |
| TS-07 | `status` 三值 ok/error/cancelled | `:76–77` | doc 01 | 無依據(已搜:`span`、`狀態`) | rides with TS-01(`cancelled` 從未被任何生產者寫出) | **CAT-C**(rides) | converge |
| TS-08 | `producer` 五值 router/proxy/agent/model/studio | `:80`;enum `app/schemas/contracts/traces.py:52–60` | doc 02 | 無依據(已搜:`producer`、`來源`、`腳色`) | rides;`model` / `studio` 兩值**從未被寫出**(唯二生產者是 ingest 端與 proxy) | **CAT-C**(rides) | converge |
| TS-09 | `attributes` JSONB | `:78` | doc 09 | 無依據(已搜:`attributes`、`屬性`) | rides;⚠ 可承載對話內容 → 與 §2:73「❌ 看對話明文」形成未被治理的外洩面 | **CAT-C**(rides) | converge |
| TS-10 | `classification_level`(NOT NULL,預設 `無機密`) | `:81–82`;`r1_0001:216` | doc 08 §5 | **無依據且與規格衝突**:§8:227「密等在**專案啟動時就標好了**,平台主要是**記錄**它」要求**如實記錄**;§8:268「**UI 上的字必須誠實**」。本欄**兩個生產者都不寫**(`api/traces.py:144–156` 未帶、`proxy/spans.py:88–100` 未帶)→ 恆為 `無機密` | 一個永遠宣稱「無機密」的欄位,對可能含密內容的 span 是**不誠實標記**,不是無害死欄 | **CAT-C** | converge(隨表移除;若表因故保留,此欄必須移除或真的寫入) |

### 2.2 `services/csp/app/api/traces.py`

| # | 構件 | file:line | doc ref | Q1 證據 | Q2 持續成本 | 分類 | 建議動作 |
|---|---|---|---|---|---|---|---|
| TR-01 | `POST /v1/traces/{trace_id}/spans`(data-plane 收攏端點) | `app/api/traces.py:100–173` | doc 05 §6/§13、doc 09 §10、doc 03 §P0 | **否定**:SYSTEM-MAP:211;端點路徑之爭本身即 doc-only 產物(`:8–12` 自述以 doc 09 收斂) | 一個掛在 nginx `/v1` 直通路徑上的**外部可寫入端點**(agent key / service token / JWT / sk- 皆可寫),即上線前源碼掃描面(§10:338「源碼掃描 + 弱點測試」)的一項純負債 | **CAT-C** | converge(移除端點 = 直接縮小攻擊面) |
| TR-02 | `_ingest_producer`:producer 一律由伺服器依呼叫者身分指派,**不收 client 自報**;匿名 fail-closed 401 | `:70–97` | — | **專案安全紅線**(fail-closed 身分判定、不信任 client 宣稱);規格側佐證 §4:146「**現況:可以被偽造。**」、§10:349「agent/模型權限、跨使用者資料存取 → A01 存取控制失效」 | 只要 TR-01 存在,本守衛就必須存在 | **CAT-A(條件式)** | keep + re-cite;**但它不是保留 TR-01 的理由** —— TR-01 若收斂,本守衛隨之消失,不得單獨保留一個沒有守衛的端點 |
| TR-03 | `_MAX_SPANS_PER_BATCH = 256` + 413 | `:49`、`:109–116` | doc 05 §6 | 無依據(已搜:`批次`(唯一命中 95 行=批次核准,無關)、`上限`) | rides with TR-01 | **CAT-C**(rides) | converge |
| TR-04 | body/path `trace_id` 一致性驗證 → 422 | `:118–129` | 契約自定 | 無依據(已搜:`trace`) | rides with TR-01 | **CAT-C**(rides) | converge |
| TR-05 | 每 span 一個 savepoint 的 upsert-ignore 冪等 | `:157–164` | doc 05 §6 | 無依據(已搜:`冪等`) | rides;每 span 一次 `begin_nested` = 每批最多 256 次 savepoint | **CAT-C**(rides) | converge |
| TR-06 | `GET /api/traces/{trace_id}`(control-plane 讀) | `:176–218` | doc 09 §10 | **無依據**(已搜:`trace`、`觀測`、`診斷`);唯一 UI 消費者是 dev-only 面板 `apps/anila-shell/src/spanTree.jsx:1–6` | 一個正式使用者永遠看不到的端點,卻要維護排序、存取控制與 404/403 語意 | **CAT-C** | converge |
| TR-07 | 讀取存取控制:有 task → `ensure_task_access`;無 task 的 trace → 僅 admin/owner | `:189–199` | doc 03 | **專案安全紅線**(跨使用者資料存取);規格佐證 §2:73「❌ 看對話明文」、§10:349「A01 存取控制失效」 | 只要 TR-06 存在就必須存在 | **CAT-A(條件式)** | keep + re-cite;同 TR-02,不構成保留 TR-06 的理由 |

### 2.3 `services/csp/app/services/proxy/spans.py`

| # | 構件 | file:line | doc ref | Q1 證據 | Q2 持續成本 | 分類 | 建議動作 |
|---|---|---|---|---|---|---|---|
| PS-01 | `record_proxy_dispatch`:**每一次** task-linked 代理呼叫寫入 parent + child 兩列 span | `app/services/proxy/spans.py:114–173`;接線點 `app/services/proxy/service.py:106` | doc 05 §6 / doc 10 §6 P0 | **否定**:SYSTEM-MAP:211;無任何規格條文要求 proxy 自產 span | **本域最高持續成本**:每則使用者訊息 2 個額外 DB 連線 + 2 次 commit(`emit_span` 每次自開短命 session,`:85`、`:102`),3000 人規模的熱路徑;且無保留期政策 → 表無限成長,而 §9:291「⚠ **資料庫沒有備份**」尚未解決 | **CAT-C** | converge(收斂效益最大的單一項) |
| PS-02 | proxy 概念名 → doc 05 §6 逐字 span type 對映常數(`proxy.dispatch→agent.run.finished` 等) | `:43–45` | doc 05 §6 | 無依據;且為**削足適履**產物 —— 檔頭 `:16–20` 自承「required-13 全是『agent run 內部』語彙,proxy 沒有原生型別」 | 語意失真(proxy 呼叫被貼成 `agent.run.finished`),後續讀者必被誤導 | **CAT-C** | converge |
| PS-03 | `emit_span` 獨立 session + log-not-raise 韌性姿態 | `:60–111` | doc 05 §6 | 無依據 | rides with PS-01 | **CAT-C**(rides) | converge |
| PS-04 | `_naive_utc` 時間正規化(**兩處重複實作**) | `:48–57` 與 `app/api/traces.py:52–57` | — | 無依據 | rides;重複實作是既有小技術債 | **CAT-C**(rides) | converge |

### 2.4 `services/csp/app/schemas/contracts/traces.py`

| # | 構件 | file:line | doc ref | Q1 證據 | Q2 持續成本 | 分類 | 建議動作 |
|---|---|---|---|---|---|---|---|
| TC-01 | `REQUIRED_AGENT_SPAN_TYPES` 13 值元組 | `app/schemas/contracts/traces.py:27–41` | doc 05 §6 | **否定**:SYSTEM-MAP:393(6 種 span 的 trace-test 列於 387「不對應上述任何需求」);13 型別語彙在規格中**零次**出現 | 每一位 agent 開發者的必修課(範本 `packages/anila-agent/anila_agent/tracing.py:1–19` 自稱「範本必備 … L3 approval blocker」);亦是 OE-1 `full_trace` 檢查的資料來源(`app/api/agents/health.py:320–325`) | **CAT-C** | converge(與 OE-1 同批;OE-1 移除硬閘後本常數再無消費者) |
| TC-02 | `SpanStatus` 三值 enum | `:44–49` | doc 01 | 無依據 | rides with TS-01 | **CAT-C**(rides) | converge |
| TC-03 | `SpanProducer` 五值 enum | `:52–60` | doc 02 | 無依據 | rides;`MODEL` / `STUDIO` 兩值無任何寫入者 | **CAT-C**(rides) | converge |
| TC-04 | `TraceSpanIn` ingest 契約(9 欄) | `:63–76` | doc 09 span event schema | 無依據 | ⚠ **跨 repo 凍結契約**:`packages/anila-agent/anila_agent/tracing.py:6–9` 標為 FROZEN wire contract;收斂須同步 `packages/anila-agent`、`packages/anila-core`(`test_router_trace_spans.py`) | **CAT-C** | converge(需跨 package 協調,見 §5 風險) |
| TC-05 | `TraceSpanOut` 讀出契約 | `:79–97` | doc 09 | 無依據 | rides with TR-06 | **CAT-C**(rides) | converge |

### 2.5 `services/csp/app/models/task.py`

| # | 構件 | file:line | doc ref | Q1 證據 | Q2 持續成本 | 分類 | 建議動作 |
|---|---|---|---|---|---|---|---|
| TK-01 | `tasks` 表(「新系統主脊椎」) | `app/models/task.py:52–55`;DDL `r1_0001:57–97` | doc 01 §3 | **無依據**:`task` / `任務` 在 399 行規格中出現 **0 次**(已搜:`task`、`任務`、`工作單`、`派工`);`派工`(105/114/138/303 行)全部指「把請求送到 MLSteam 上的 agent」這個**動作**,不是一個被持久化的任務實體 | **每則 ANILA 使用者訊息**都建一列 Task + 一列 SourceSnapshot + 一列 audit(`apps/anila-shell/README.md:38`「首次送訊息時 createTaskForConversation() → POST /api/tasks」);3000 人規模的熱路徑 | **CAT-C** | converge |
| TK-02 | `task_type` 八值 enum | `:61`;`app/schemas/contracts/tasks.py:21–31` | doc 01 | 無依據(已搜:`task_type`、`任務`、`分類`);規格無任何任務型態語彙 | 前端必須為每則訊息挑一個值(anila-shell 固定 `"query"`、anilalm 固定 `"generate_artifact"` → 八值中六個從未被使用) | **CAT-C** | converge |
| TK-03 | `status` 十值狀態機 + 合法轉移表 + running 快轉鏈 | `:77–78`;enum `contracts/tasks.py:34–46`;轉移表 `app/modules/tasks/service.py:52–89` | doc 01 | **無依據**(已搜:`狀態機`、`任務`、`policy_checking`、`source_resolving`);規格 0 次命中 | **高成本**:`start_task_run` 沿快轉鏈逐跳 `transition_task`,**每跳一次 commit**(`service.py:292–299` × `:272`)→ 首次派發最多 4 次額外 UPDATE+commit;終態任務再派發 → 409(`task_link.py:175–180`),使用者端要處理一個規格沒要求的錯誤態 | **CAT-C** | converge(本域收斂效益第二大) |
| TK-04 | `source_scope` 五值 enum | `:80–81`;`contracts/tasks.py:49–57` | doc 01 | 無依據(已搜:`來源`(僅 189/370 行,無關)、`scope`) | rides with TK-01;`create_task` 有一條「宣告矛盾」驗證(`service.py:154–158`)只為維護本欄一致性 | **CAT-C** | converge |
| TK-05 | `selected_collection_ids`(JSON,NOT NULL) | `:82` | doc 01 | **無依據且與規格反向**:§4:142「⚠ **collection ID 設定在 MLSteam 那邊的 `.env`,不是在 CSP 綁定**」 | 在 CSP 端維護一份 agent 路徑上規格明說**不該由 CSP 綁定**的 collection 清單 | **CAT-C** | converge(ANILALM Studio 若需要 collection 清單,應直接放進 Studio job body,不經 Task) |
| TK-06 | `selected_service_id` | `:83` | doc 01 | 無依據(已搜:`registered_service`、`服務`) | rides;消費者在 registered-services 域(`app/api/services.py:361–362`) | **CAT-C**(跨域,見 §6) | converge(與 launch/services 域協調) |
| TK-07 | `requested_output_type` 七值 enum | `:86`;`contracts/tasks.py:60–69` | doc 01 | **部分規格存在、現有形狀被規格直接否定**:§11:357「**產出:簡報 / 報告優先**;其餘次要」給了 slides/report 的依據,但 §11:360 逐字寫「**心智圖不是產出物**,是讓使用者快速理解知識庫節點的**瀏覽方式**」→ 枚舉值 `MINDMAP`(`contracts/tasks.py:66`)**與規格矛盾** | 一個規格說不是產出物的產出型態,留在 API 契約裡就會有人去實作它 | **CAT-C** | converge;產出型態若要保留,應落在 Studio job 上(§11),且**必須移除 `MINDMAP`** |
| TK-08 | `source_snapshot_id` / `policy_decision_id`:**刻意不掛 DB FK** 的指標欄 | `:88–89`;理由見 `:13–16` | doc 01 | 無依據 | **正是 §0:14 點名的負債型態**:「任何需要專人照顧的機制(守衛、儀式、多步驟部署)都是負債」—— 參照完整性改由 service 層人工維護,一個人維運時無人會發現它漂掉 | **CAT-C** | converge |
| TK-09 | `tasks.legacy_runtime_call` | `:90–91` | doc 10 | 無依據(已搜:`legacy`、`舊流量`) | rides;`tasks` 表上此欄從未被任何寫入者設為 true(僅 `token_usage` 的同名欄有寫入者) | **CAT-C**(rides) | converge |
| TK-10 | `tasks.classification_level` = max(宣告, snapshot 導出) | `:93–94`;計算 `modules/tasks/service.py:160–163` | doc 08 §5 | **機制有規格依據、落點沒有**:§8:227「密等在**專案啟動時就標好了**,平台主要是**記錄**它」—— 規格記錄的對象是內容資源(對話/文件/collection),不是一個規格中不存在的 task 實體 | rides with TK-01 | **CAT-C**(rides;分級**機制**本身屬 CAT-A,由分級域持有) | converge(隨脊椎;分級落點改回內容資源) |
| TK-11 | classification 三共通欄(`classification_latched_at` / `_source` / `_event_id`)在 `tasks` 與 `task_runs` 上 | `:96–102`、`:159–165` | doc 08 §5 | 同 TK-10。**注意校準**:單向升密閂鎖本身是硬規格要求(§5:189「對話中途升密 → 之前萃取的記憶要撤回」),但那是**對話**上的閂鎖;寫入者是通用資源映射表 `app/modules/policy/service.py:175`(`"task_run": TaskRun`) | rides with TK-01 / TK-14 | **CAT-C**(rides;閂鎖機制本體 = CAT-A,分級域) | converge(僅移除 task/task_run 這兩個落點,**不得**動閂鎖機制本身) |
| TK-12 | `tasks.trace_id`(NOT NULL、unique、建立即產生) | `:104–105`;`r1_0001:97` | doc 01 驗收 2 / doc 02 | **否定**:SYSTEM-MAP:211「不需要 … **trace id**」 | 每個 task 一個 uuid + 唯一索引;經 `X-ANILA-Trace-Id` 外送給 agent(`test_proxy_task_wiring.py:302`) | **CAT-C** | converge。⚠ `token_usage.trace_id` 是 `r1_0001` **之前**就存在的欄位(client 標頭寫入),不屬本次收斂範圍 |
| TK-13 | `conversation_id` FK SET NULL —— 「Conversation 降級為互動容器」 | `:71–74`;宣告見檔頭 `:4–5` | doc 01 拍板 | **無依據且與規格心智模型相反**:§1:46「| 對話管理 | ✓ | ✓ |」、§5:166「| **對話** | ANILA 與 ANILALM **各自獨立** | 使用者自己管 | 只有自己(+ 分享) |」—— 規格中對話是使用者面的第一級物件,其上沒有任何更高的實體 | 把使用者心中的主體(對話)降級為某個看不見實體的附屬,任何未來 UI/報表都得先繞過這層 | **CAT-C** | converge |
| TK-14 | `task_runs` 表 | `:125–133`;DDL `r1_0001:100–122` | doc 01 §3 | 無依據(同 TK-01) | 每次派發一列 + 起訖各一次 commit(`task_link.py:186–196`、`finalize_task_run:217–240`) | **CAT-C** | converge |
| TK-15 | `run_sequence` + `uq_task_runs_task_sequence`(重跑/handoff 各佔一筆) | `:130–131`、`:140` | doc 01 | 無依據(已搜:`重跑`、`handoff` —— 規格 0 次) | 每次開 run 先做一次 max(run_sequence) 查詢(`service.py:301–307`) | **CAT-C** | converge |
| TK-16 | `dispatch_target` 四值 enum | `:142`;`contracts/tasks.py:82–88` | doc 01 run_type | 無依據 | rides;`studio` / `service` 兩值在 `/v1` 熱路徑從未寫出 | **CAT-C**(rides) | converge |
| TK-17 | `TaskRun.status` 五值 enum + 終態限制 | `:144–145`;`contracts/tasks.py:72–79`;`service.py:339–340` | doc 01 | 無依據 | rides | **CAT-C**(rides) | converge |
| TK-18 | `usage_record_id` FK → `token_usage.id`(run → usage 反向指標) | `:150–153`;`r1_0001:117–118` | doc 02 | **否定**:SYSTEM-MAP:210–211 列出用量六欄後說「一張表加幾個索引」;run↔usage 雙向連結不在其中 | 在**全系統寫入量最高的表**上多一條 FK;且實務上從未被回填(`finish_task_run` 的 `usage_record_id` 在 proxy 路徑沒有傳入 —— usage 是非同步佇列寫入,run 收攏時還拿不到 id) | **CAT-C** | converge |
| TK-19 | `TaskRun.error` JSON | `:155` | doc 01 | 無依據 | rides | **CAT-C**(rides) | converge |

### 2.6 `services/csp/app/schemas/contracts/tasks.py`

(TaskType / TaskStatus / SourceScope / RequestedOutputType / TaskRunStatus / DispatchTarget 已列於 §2.5 對應列。)

| # | 構件 | file:line | doc ref | Q1 證據 | Q2 持續成本 | 分類 | 建議動作 |
|---|---|---|---|---|---|---|---|
| TC2-01 | `TaskCreate` / `TaskOut` / `TaskRunOut` API 契約 | `app/schemas/contracts/tasks.py:109–163` | doc 09 §Task API | 無依據(已搜:`task`、`任務`) | rides with TK-01;`TaskOut` 已是前端型別來源(`apps/anilalm/src/api/tasks.ts`、`apps/anila-shell/src/runtime/tasks.js`)→ 收斂要動兩個前端 | **CAT-C**(rides) | converge |
| TC2-02 | `SnapshotOrigin` 五值 enum | `:91–98` | doc 01 §4 | **無依據**(已搜:`快照`、`snapshot` —— 規格 0 次) | 跨域(SourceSnapshot 域) | **CAT-C(暫定,跨域)** | 交 snapshot 域裁定;本報告提供 Q1 證據 |
| TC2-03 | `CitationUsedBy` 三值 enum | `:101–106` | doc 01 §5 | **無依據**(已搜:`引用`、`出處`、`citation` —— 規格 0 次) | 跨域(Citation 域) | **CAT-C(暫定,跨域)** | 交對應域裁定 |
| TC2-04 | `SourceSnapshotIn` / `SourceSnapshotOut` / `CitationOut` 契約 | `:166–222` | doc 01 §4–5 | 同上,零命中 | 跨域 | **CAT-C(暫定,跨域)** | 交對應域裁定 |

### 2.7 `services/csp/app/modules/tasks/__init__.py`

| # | 構件 | file:line | doc ref | Q1 證據 | Q2 持續成本 | 分類 | 建議動作 |
|---|---|---|---|---|---|---|---|
| MT-01 | 模組邊界規則(「其他程式碼只能從 package 根 import」「不得 import `app.api`」) | `app/modules/tasks/__init__.py:9–13` | doc 02 §1 / doc 10 §4 | 無依據(已搜:`模組`、`邊界`、`架構`) | 一條**沒有自動化檢查**、只靠人記得的架構規則,且已迫使 `app/services/proxy/task_link.py:136`、`:217` 改用 call-time lazy import 迴避循環 —— §0:14「需要專人照顧的機制…都是負債」 | **CAT-C**(rides TK-01) | converge(隨模組) |
| MT-02 | 公開介面 re-export(七函式 + router) | `:17–37` | doc 10 §4 Slice 2 | — | — | **無獨立可稽核構件**:純 re-export,語意完全來自 `service.py` / `router.py`(不在本 FILE SET) | 不適用 |

### 2.8 `services/csp/migrations/versions/r1_0001_task_trace_foundation.py`

| # | 構件 | file:line | doc ref | Q1 證據 | Q2 持續成本 | 分類 | 建議動作 |
|---|---|---|---|---|---|---|---|
| MG-01 | 本域三表 DDL(`tasks` / `task_runs` / `trace_spans` + 8 索引) | `:57–122`、`:200–224` | doc 01 / 05 §6 / 09 | 見 TK-01 / TK-14 / TS-01 | 見對應列 | **CAT-C**(= TK-01 / TK-14 / TS-01) | converge。⚠ **migration 是不可變歷史** —— 收斂做法是**新增**一支 drop migration,絕不編輯 `r1_0001` |
| MG-01b | 跨域三表 DDL(`source_snapshots` / `citations` / `policy_decisions`) | `:125–198` | doc 01 §4–5、doc 03 §5 | **無依據**(已搜:`快照`/`snapshot`/`引用`/`出處`/`citation`/`政策`/`policy` —— 全部 0 次)。⚠ 相關佐證:§8:280 的 append-only 背書指向**稽核帳**;現行 `policy_decisions`(actor/action/resource/decision/created_at)與 `audit_logs`(`app/models/audit_log.py`)欄位高度重疊,是**同一規格需求的兩套帳** | 跨域 | **CAT-C(暫定,跨域)** | 交 policy / snapshot 域裁定;本報告提供 Q1 證據與雙帳觀察 |

### 2.9 `services/csp/migrations/versions/r1_0002_usage_task_link.py`

| # | 構件 | file:line | doc ref | Q1 證據 | Q2 持續成本 | 分類 | 建議動作 |
|---|---|---|---|---|---|---|---|
| MG-02 | `token_usage.task_id` + FK(SET NULL)+ partial index | `r1_0002:28–32`、`:41–54` | doc 04 AC10 | **否定 + 實證唯寫**:SYSTEM-MAP:210「要記的欄位:**時間、使用者、部門、模型、agent、token 數**」(六項無 task)、211「一張表加幾個索引」;`app/services/usage_service.py` **零次**讀取 `TokenUsage.task_id`,分組維度僅 model/user/department(`:225–230`) | 在全系統寫入量最高的表上多一個 FK + 一個 partial index,只寫不讀;FK 也讓刪除 task 必須走 SET NULL 路徑 | **CAT-C** | converge(新增 drop migration) |
| MG-03 | `token_usage.legacy_runtime_call`(Boolean NOT NULL,server_default false) | `r1_0002:33–40` | doc 10 Slice 2 | **無依據**(已搜:`legacy`、`舊流量`) | 近乎為零:一個 boolean、無索引、無查詢者;task 收斂後恆為 true(或恆 false),但不影響任何行為 | **CAT-B** | keep + mark-debt(可與 MG-02 同一支 drop migration 一起清掉,但不值得單獨排工) |

---

## 3. 測試鎖定的「規格無依據」構件

FILE SET 內的測試:`services/csp/tests/test_proxy_task_wiring.py`(682 行,15 個測試)。
它**整檔**是 CAT-C 構件的回歸鎖。若 D2(Task 主脊椎)收斂,本檔需要重寫而非微調。

| 測試 | 鎖住的構件 | 對應構件 # | 收斂後處置 |
|---|---|---|---|
| `TestUserCallerWithTask::test_model_target_creates_and_finishes_task_run`(`:213–252`) | `X-ANILA-Task-Id` 入向契約、`PolicyDecision(action="task.run")` 每次派發一列、TaskRun 起訖、`usage.task_id`、usage `trace_id` 回落到 `task.trace_id` | TK-01/03/14、MG-01b、MG-02、TK-12 | **刪除** |
| `::test_agent_stream_forwards_task_and_trace_headers`(`:277–316`) | 外送 agent 帶 `X-ANILA-Task-Id` + `X-ANILA-Trace-Id` | TK-01、TK-12 | **刪除**;⚠ 這兩個純文字標頭正是 §4:146 點名的可偽造型態,規格的正解是 §4:150 的 5 分鐘簽章 JWT |
| `::test_upstream_failure_marks_run_failed`(`:318–341`) | TaskRun 失敗態 + `error` JSON | TK-14/17/19 | **刪除** |
| `TestLegacyNoTaskHeader::test_no_header_marks_legacy_and_creates_no_run`(`:348–370`) | `token_usage.legacy_runtime_call=true` 語意 | MG-03 | 改寫(降級為「無 task 時流量不受影響」的普通回歸) |
| `TestTaskAccessControl`(`:377–436`,3 測試) | task 未知→404 / 他人→403 / 格式錯→400 | TK-01 + TR-07 家族 | **刪除**;但 403「不可見即不存在」的語意應在其他資源的存取控制測試中續存 |
| `TestServiceTokenCaller`(`:477–574`,4 測試) | service token fail-closed 401、缺員編 fail-closed 403、task.requester 必須等於轉發身分 | TK-01(對象)+ **fail-closed 身分解析(紅線)** | **改寫,不可直接刪**:`_resolve_acting_user`(`app/services/proxy/task_link.py`)的 fail-closed 姿態對應 §4:154「身分能偽造,用量歸屬就能偽造,那份給長官的部門月報表就不可信」→ **CAT-A 相鄰**。收斂 task 時必須把這三個 fail-closed 斷言搬到用量歸屬的測試檔,不能隨 task 一起消失 |
| `TestHeaderBuilders::test_model_gateway_headers_never_carry_task_headers` / `test_model_gateway_headers_have_no_task_or_trace_surface` / `test_proxy_stream_model_destination_drops_task_headers`(`:254–275`、`:593–632`) | 模型 gateway 出向標頭 = Bearer + `X-ANILA-User-Id` **僅此兩者** | — | **保留、改寫斷言基礎**:出向 `.12` 標頭最小化本身值得續守(§1 環境事實 + 最小權限),但斷言不應再以「task/trace 標頭」表述;改為正面表列允許的標頭集合 |
| `TestUsageTaskLinkColumns::test_enqueued_payload_flushes_into_token_usage_columns`(`:639–682`) | `token_usage.task_id` / `legacy_runtime_call` 真的寫進欄位 | MG-02、MG-03 | **刪除**(隨 MG-02) |

**本 FILE SET 外、但同樣鎖住本域 CAT-C 構件的測試**(交接給收斂執行者,勿遺漏):
`services/csp/tests/test_trace_endpoints.py`、`tests/test_task_trace_schema.py`、`tests/test_agent_registry_upgrade.py`(trace-test,OE-1 已裁)、`packages/anila-agent/tests/test_tracing.py`、`packages/anila-core/tests/test_router_trace_spans.py`、`apps/anila-shell/src/__tests__/{traces,spanTree,traceExplorer,tasks}.test.*`。

---

## 4. 收斂決策單元(50 個 CAT-C = 6 個決策)

| # | 決策單元 | 含哪些構件 | 收益 | 依賴 |
|---|---|---|---|---|
| **D1** | **span 子系統整體下架** | TS-01…TS-10、TR-01…TR-06、PS-01…PS-04、TC-01…TC-05 | 移除熱路徑每則訊息 2 次額外 commit;移除一個外部可寫 `/v1` 端點;移除一張無保留期、無備份的成長表;agent 開發者不再需要實作 13 型別 span | 應在 **OE-1 之後**做(OE-1 移除 trace-test 硬閘後,`trace_spans` 只剩 dev-only 面板一個消費者) |
| **D2** | **Task 主脊椎收斂** | TK-01…TK-19、TC2-01、MT-01、MG-01 | 移除每則訊息的 Task+Snapshot+audit 三列寫入、狀態機最多 4 次快轉 commit、409 錯誤態;把「對話」還原成使用者面第一級物件 | 影響 `apps/anila-shell`、`apps/anilalm`、`app/api/services.py`(launch)、`app/api/artifacts.py`;需與 §6 相鄰域協調 |
| **D3** | **usage 的 task 維度** | MG-02(+ MG-03 順帶) | 讓 `token_usage` 回到規格 §7:210–211 的「六欄 + 索引」形狀 | 隨 D2;必須用**新** migration drop,不可改 `r1_0002` |
| **D4** | **分級在 task/task_run 的落點** | TK-10、TK-11 | 隨 D2 消失 | ⚠ **僅移除落點**,分級閂鎖機制本體是 CAT-A(§5:189),由分級域持有,不得動 |
| **D5** | **跨域三表** | MG-01b、TC2-02…TC2-04、TK-06 | — | 交 policy / snapshot / launch 域;本報告只提供 Q1 證據 |
| **D6** | **必須存活的守衛** | TR-02、TR-07、`_resolve_acting_user` fail-closed(測試節) | — | 這三項是 CAT-A(條件式):**它們不能作為保留 D1/D2 的理由**,但收斂時也不能讓它們「隨宿主一起消失」而在替代路徑上留缺口 |

---

## 5. 收斂風險(交接給執行者)

1. **跨 repo 凍結契約**:`TraceSpanIn` 的 wire contract 被 `packages/anila-agent/anila_agent/tracing.py`(448 行)標為 FROZEN,且被宣傳為「官方 agent starter 範本必備」。若已有內網 agent 照抄,收斂 D1 要同步下架 SDK 發送器並公告,否則 agent 會對著 404 端點重試(所幸該發送器是 drop-and-log,不會弄掛 agent —— `tracing.py:12–14`)。
2. **前端三處**:`apps/anila-shell/src/runtime/tasks.js`(每則訊息建 task)、`apps/anila-shell/src/runtime/traces.js` + `spanTree.jsx`(dev-only 面板)、`apps/anilalm/src/api/tasks.ts`(Studio artifact task)。anila-shell 已有「task 建立失敗就以無任務模式照常聊天」的降級路徑(README:38),**收斂風險低**。
3. **launch / artifacts 域對 Task 的依賴**:`app/api/services.py:155–157`、`:361–362`、`app/api/artifacts.py:269`、`:288`。這兩個域需要的其實只是「這次產出的來源與申請人」,不是整條脊椎 —— 建議 D2 執行時一併提供最小替代欄位。
4. **migration 不可變**:`r1_0001` / `r1_0002` 已在本機從零跑到 `r1_0008` 驗過(CLAUDE.md §4)。收斂一律**新增** drop migration。

---

## 6. 相鄰觀察(不屬本域,或已在他處裁定,僅供交叉參考)

1. **同一規格需求的兩套帳**:`policy_decisions`(`r1_0001:175–198`)與 `audit_logs`(`app/models/audit_log.py`)欄位高度重疊(actor / action / resource / 結果 / created_at)。§8:271–280 只要求**一套** append-only 稽核帳。交 policy 域。
2. **稽核帳的防竄改尚未落地**:§8:278「| 防竄改 | **要** —— 明確是為了防止 admin 權限的人偷偷做假 |」。`audit_logs` 目前是普通表(無雜湊鏈、無 append-only 約束)。這是**規格要求但缺實作**(CAT-A 缺口),與本域的過度設計方向相反,建議一併回報給指揮官。
3. **OE-1 相鄰**:`app/api/agents/health.py:289–360` 的 trace-test 是 `trace_spans` 目前唯一的後端消費者。OE-1 移除它之後,`trace_spans` 的消費者只剩 dev-only 前端面板 —— **D1 的收斂成本會顯著下降,建議排在 OE-1 之後**。
4. **OE-3 相鄰**:`trace_spans` / `tasks` / `task_runs` 三表的 `classification_level` 都是 `String(20)` + `server_default '無機密'`。若 D1/D2 收斂,OE-3 的四級對齊就少三個落點要改。
5. **與本域無關但同檔發現**:`RequestedOutputType.MINDMAP`(`contracts/tasks.py:66`)與 §11:360「心智圖不是產出物」直接矛盾 —— 即使 Studio 產出型態日後改放在 Studio job 上,這個值也不該被搬過去。
