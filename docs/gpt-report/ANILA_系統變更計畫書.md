# ANILA 系統變更計畫書

## 主大腦、Agent Integration、Agentic 執行流程與 Ingestion 可靠性強化

| 文件欄位 | 內容 |
|---|---|
| 文件版本 | v1.0 |
| 日期 | 2026-07-10 |
| 程式基線 | `main@f661f5e8c9d2d70d4647ef3f67eb5b9624ffc93c` |
| 文件狀態 | 設計審查與派工草案 |
| 適用範圍 | `anila-core`、CSP、`anila-agent`、ANILA Shell、Ingestion |
| 文件依據 | 實際程式碼、README（僅供定位）、指定 Router 分析、產品北極星 |
| 規劃假設 | 2 週一個 Sprint；1 名技術負責人、3–4 名後端／Agent 工程師、1 名前端、1 名 QA/SRE 共用 |

> 本計畫主張「演進式收斂、契約先行、逐段切換」，不重寫整個平台。正式 Agent 仍可使用不同框架，透過 ANILA wire contract 與 CSP 治理接入；使用者只看見 ANILA，不建立 Agent 市集、MCP 市集或自由 swarm。

<!-- PAGEBREAK -->

# 0. 決策摘要

## 0.1 建議決策

ANILA 可以強化 `anila-core`，也可以實作附圖所示的 Agentic 執行流程，但前提不是增加一個前端步驟清單，而是先統一 Router、CSP、Agent 與 Shell 的控制契約與事件真值。

本計畫建議核准以下七項決策：

1. 將正式 Router 從字串型 `DISPATCH:<agent_id>:<query>` 演進為結構化 `RouteDecision`，舊 parser 僅作限期相容層。
2. 將 CSP 已有的正式 Agent Manifest、health、approval、classification ceiling 與 trace-test 狀態投影為 Router 的權威 registry read model。
3. 任何 Agent dispatch 必須同時通過 `CapabilityFilter` 與 `PolicyGate`，CSP 在實際轉送時再次執行權威檢查。
4. 建立 `AgentClient + StreamBridge + SessionEventStore`，以 `StepEvent` 作為附圖式執行畫面的唯一真值。
5. 將官方 `anila-agent` 從 RAG starter 升為至少 Silver 等級的正式整合套件；不要求 Agent 開發者更換框架。
6. 將 Ingestion 可靠性、分類傳播與搜尋 clearance gate 列為 P0，不把「功能很多」誤認為「可安全營運」。
7. Multi-Agent 最後才開啟，僅允許 Router 產生、PolicyGate 核准、步數與併發受限的受控計畫；v1 不提供使用者自行組 swarm。

## 0.2 現況判定

| 評估題目 | 現況判定 | 本計畫目標 |
|---|---|---|
| `anila-core` 能否強化 | 可以；已有 QueryEngine、Coordinator、lifecycle、approval、handoff、trace 等可重用能力，但正式 Router 尚未收斂到同一執行核心 | 不重寫，抽出共用 RouterRuntime 並讓正式流量逐步切換 |
| 能否做到附圖 Agentic 流程 | 可以；前端元件與 SSE 解析已有部分基礎，但後端缺統一、可回放的步驟事件 | 每個 UI 步驟都由真實 `StepEvent` 驅動，可 pause/resume/cancel/replay |
| `anila-agent` SDK 是否扎實 | 核心依賴、air-gap、policy、retrieval、memory、trace 基礎不差；但目前更像 RAG 應用樣板，尚非完整平台 SDK | 正式化 manifest、session、event、HITL、idempotency、classification 與 conformance |
| Ingestion 是否完善 | 解析、OCR、chunk、embedding、relation、preview/eval 很廣；交易一致性、冪等、終態、故障測試與分類治理不足 | 建立 outbox、租約式 job 狀態機、generation 原子切換、分類傳播、fault-injection gate |

## 0.3 不做事項

- 不以 Router 重寫為前提，也不在第一階段刪除 legacy 介面。
- 不建立 Agent marketplace、MCP marketplace 或一般使用者可配置的多 Agent swarm。
- 不照抄附圖中的動態 plugin discovery／安裝；v1 的附圖體驗定義為「已註冊 Agent、已核准工具與單 Agent 多工具的受控執行透明化」。
- 不將 raw chain-of-thought、完整工具參數、秘密或未遮罩的敏感資料送到 Shell。
- 不讓 Router 取代 CSP 的治理權威；Router gate 是早期攔截，CSP 仍是最終 enforcement point。
- 不在同一版本執行破壞性資料庫 migration；採 expand → backfill → switch → contract。
- 不把模型訓練、模型替換或全面文件重寫納入本次範圍。

# 1. 目標、成功定義與設計原則

## 1.1 產品與系統目標

1. 使用者以任務為中心，只接觸 ANILA；Router 負責選擇可用能力，CSP 負責治理與稽核。
2. 所有正式 Agent 執行都能以 `trace_id + task_id + invocation_id + step_id` 回溯。
3. 分類等級、scope、approval 與 Full Trace 需求從請求到 Agent、retrieval、artifact 全程傳播且 fail-closed。
4. 附圖式步驟不是前端動畫，而是可持久化、可重播、可取消、可驗證的執行事實。
5. Ingestion 從「接受上傳」到「可安全搜尋」具備一致的狀態機、分類與失敗復原。

## 1.2 設計不變式

- 每次 dispatch 都必須有通過 schema 驗證的 `RouteDecision`。
- 每次 dispatch 都必須有 `PolicyGateResult.allowed=true`；CSP 轉送時再次檢查。
- Router 永遠透過 CSP 呼叫正式 Agent，不將使用者 bearer token 直接轉交 Agent。
- 呼叫者提供的 system message 不得取代 Router 的內部控制提示。
- 未知契約版本、過期 registry snapshot、未知 Agent、分類不足、health 不合格一律不派工。
- 每個 Task、Invocation、Step 僅能進入一次終態；重試不得重複產生外部副作用。
- UI 只顯示安全摘要；完整輸入輸出僅進受控 Full Trace store。
- Ingestion 的文件分類不得在 chunk、relation、search 階段遺失或降低。
- 安全 gate 啟用後，回滾只可回到更保守的能力，不可回到無 gate dispatch。

# 2. 程式基線與主要缺口

## 2.1 主大腦存在兩套執行路徑

正式流量由 `packages/anila-core/src/anila_core/api/router_server.py` 建立 Router app。它要求模型輸出 `DISPATCH:` 字串，再由 regex 解析並轉送；相對地，`engine/query_engine.py` 已有 tool loop、approval、handoff、guardrails、lifecycle 與 session 能力，`coordinator/coordinator.py` 也有 worker orchestration，但兩者並未成為正式 Router 的共同執行核心。

這會造成四種漂移：

- 控制面漂移：正式 routing 判斷是字串 prompt protocol，而非型別化決策。
- 執行面漂移：QueryEngine 的生命週期、approval、handoff 與 Router 轉送各自演進。
- 狀態漂移：Router session 只保存有限訊息，與可 pause/resume 的引擎能力不一致。
- 稽核漂移：Router、Agent、UI 對「一步執行」沒有同一個事件定義。

另有立即風險：呼叫者只要自帶 system message，內部 routing system prompt 便可能不被注入；`anila_multi_turn` 缺乏可靠上限；registry 過期、未知 Agent 或 parser 被提示注入時，沒有統一的 fail-closed PolicyGate。

## 2.2 CSP 有正式資料，但 Router 只看 legacy 投影

`services/csp/app/models/agent.py` 與 `schemas/contracts/agents.py` 已承載 formal manifest、approval、health、classification ceiling、runtime 與 trace-test 等治理資料；目前 `/v1/agents` 與 `remote_agent_manifest.py` 使用的仍是較小 legacy 欄位集合。結果是 CSP 知道 Agent 是否真正可派工，Router 卻無法以這些資料先過濾候選。

## 2.3 官方 anila-agent 是好用 starter，尚非完整整合 SDK

正面基礎包括 pinned `openai-agents==0.17.5`、air-gap 約束、fail-closed policy、CSP HTTP retrieval、per-user memory 與 tracing hooks。主要缺口包括：

- service 只取最後一則 user message，未真正採用完整 history 與 `anila_session_id`。
- `ANILA_COLLECTION_ID` 對所有服務成為必要條件，使非 RAG Agent 也被 RAG starter 綁定。
- 沒有正式 manifest endpoint、標準 StepEvent stream、cancel/resume 與完整 HITL service contract。
- skills、MCP、triggers 是分離模組；未接入 runtime 的能力不應被 manifest 宣告。
- `runtime/runstate.py` 參照未實作的 `run_once_state()`。
- `PostgresSession` 未完整符合目前 pinned SDK 的 Session protocol。
- 現況測試在排除 9 個 Windows 路徑假設失敗後為 199 passed、1 live skipped；ruff 通過，但 strict mypy 尚有 24 個錯誤。

## 2.4 Agentic UI 元件存在，但沒有成為正式訊息流程

`apps/anila-shell/src/agentic.jsx`、`toolExecution.jsx` 與 `runtime/sse.js` 已有元件與部分 typed event parser；`app.jsx`、`chat.jsx` 尚未讓 send/edit/regenerate/continue 等路徑共用一套 execution reducer，也未完整接上 tool、todo、interrupt、resume、replay。因此目前即使後端有部分 event，畫面仍不能以真實狀態還原附圖流程。

## 2.5 Ingestion 功能面廣，可靠性與分類治理不足

現有程式已包含多格式 parser、OCR/VLM、hierarchical/semantic chunk、parent/leaf、halfvec 4000、vector/keyword/relation search、relation resolver、preview 與 evaluator。P0 缺口是：

- upload、文件資料、`ingestion_jobs` 與 Arq enqueue 不在同一個可靠提交協定；可出現文件已接受但永遠 pending/queued，甚至 worker 先於 job row 建立而完成。
- worker 靜態 `max_tries=3` 未真正採用 retryable/permanent 錯誤分類。
- parent 與 leaf 分批交易，重試不是 generation-level 冪等，可能留下部分資料或撞唯一鍵。
- 空 chunks 路徑可能更新文件為 indexed，卻沒有一致地將 job 設為 succeeded。
- timeout、cancel、同步 parse 阻塞 event loop 等情境可能留下 running；`indexed` 也混合了 parse/embed/enrich 等不同階段。
- migration 已加入五級分類欄位，但 worker 寫入 chunks 時未完整傳播；search 主要依 owner/bound collection ACL，尚缺依 caller clearance 與資料分類的權威 gate。
- 目前 ingestion 測試約 146 passed，偏單元與 helper，缺 PostgreSQL、Redis、worker crash、enqueue fault、timeout/cancel 的整合與故障注入。

## 2.6 實作 SSOT 與正式部署分支不同

本計畫的程式實作 SSOT 是 `main@f661f5e8c9d2d70d4647ef3f67eb5b9624ffc93c`。產品北極星所指的正式部署 evidence 基準包含 `origin/prod-intranet-card`；本次檢查顯示該分支與 `main` 為 12/11 commits divergence，不能假設主線變更可直接 fast-forward 到正式分支。

因此每個 Phase 必須同時產出 downstream port matrix：主線 commit、正式分支對應 commit、需保留的 SSO/card 差異、mixed-version 測試、部署前置檢查與 branch-specific rollback tag。正式分支採人工挑選與衝突審查，不以盲目 merge 取代驗收。

## 2.7 Full Trace 目前仍可能在部署層為 no-op

Router 程式只有在 `ANILA_TRACE_ENDPOINT` 設定時才輸出 CSP Full Trace；現有 compose 並未確認注入此設定。這代表只改 Router 程式不足以滿足北極星。計畫必須包含 compose/env、production startup guard、readiness、trace callback smoke test、RLS、分類、加密、retention、刪除與容量策略。正式 profile 宣告 Full Trace required 時，未設定或不可達必須 readiness failed，而不是靜默 no-op。

# 3. 目標架構

```text
ANILA Shell
    │  OpenAI-compatible SSE + ANILA StepEvent v1
    ▼
Router HTTP Adapter
    ▼
RouterRuntime
    ├─ RequestContextBuilder（identity/session/classification/history）
    ├─ RegistrySnapshot + CapabilityFilter
    ├─ Structured DecisionEngine → RouteDecision
    ├─ PolicyGate → PolicyGateResult
    ├─ ExecutionRuntime
    │    ├─ Direct Answer / QueryEngine
    │    ├─ AgentClient → CSP Proxy → Formal Agent
    │    └─ Controlled ExecutionPlan（預設關閉）
    ├─ StreamBridge → StepEvent
    └─ SessionEventStore + Full Trace
                │
                ├──────── CSP Agent Registry / Policy / Classification
                │
                └──────── Ingestion / Retrieval（分類傳播與 clearance gate）
```

## 3.1 控制面與資料面分工

| 元件 | 權責 | 不應負責 |
|---|---|---|
| Shell | 呈現任務、步驟、approval、artifact；送出取消/回答；依 cursor replay | 猜測執行狀態、解析 raw chain-of-thought、決定治理權限 |
| RouterRuntime | 正規化請求、選候選、結構化決策、執行計畫、事件橋接、session 協調 | 繞過 CSP 直連正式 Agent |
| CSP | identity、scope、classification、approval、health、trace-test、endpoint 與最終轉送 enforcement | 取代 Router 的任務規劃與使用者互動 |
| Agent | 在 manifest 允許範圍內執行能力、發送事件、保存或回復必要狀態 | 自行提升分類、擴大 scope、向 UI 洩漏秘密 |
| Ingestion | 可靠建立可檢索資料、傳播分類、提供受 clearance 限制的 retrieval | 將 indexed 當作所有 enrichment 都完成 |

# 4. 核心契約

所有契約都必須有 `schema_version`；canonical JSON Schema 由一個 package 管理，Core、CSP、Agent 與 Shell 用同一組 fixtures 做 contract test。

## 4.1 RouteDecision

```json
{
  "schema_version": "route-decision/v1",
  "decision_id": "rd_...",
  "route_type": "direct_answer|single_agent|clarify|deny|multi_agent_plan",
  "registry_snapshot_id": "rs_...",
  "required_capabilities": ["retrieval"],
  "candidate_agent_ids": ["agent-a"],
  "selected_agent_id": "agent-a",
  "reason_codes": ["CAPABILITY_MATCH"],
  "confidence": 0.97,
  "rewritten_query": "安全且最小必要的任務描述",
  "constraints": {"max_steps": 8, "timeout_ms": 120000},
  "execution_plan": null,
  "fallback": "clarify"
}
```

規則：routing model 只可在 deterministic filter 後的候選中選擇；invalid JSON、未知 Agent、低信心或 snapshot stale 必須 deny、clarify 或 replan，不得猜測派工。`reason_codes` 是可稽核摘要，不是 chain-of-thought。

## 4.2 PolicyGateResult

```json
{
  "schema_version": "policy-gate/v1",
  "allowed": true,
  "decision_id": "pd_...",
  "effective_classification": "...",
  "required_scopes": ["agent:invoke"],
  "obligations": ["FULL_TRACE", "REDACT_UI_SUMMARY"],
  "approval_required": false,
  "reason_codes": ["WITHIN_AGENT_CEILING"]
}
```

## 4.3 AgentManifest

必要欄位：`agent_id`、name/version、runtime/api version、task types、router description、input/output schema、capabilities、event protocols、required scopes、classification default/ceiling、Full Trace 要求、approval、health/readiness、trace-test timestamp、manifest revision、supports streaming/resume/cancel/idempotency。

清單分兩種投影：

- Shell 使用 `/v1/agents`：只回傳使用者可見且已授權的精簡資訊。
- Router internal registry view：回傳完整治理資訊、readiness 與 snapshot revision；只能由服務身分加使用者 context 取得。

## 4.4 Invocation Contract

必要 correlation headers：

```text
X-ANILA-Trace-Id
X-ANILA-Task-Id
X-ANILA-Invocation-Id
X-ANILA-Session-Id
X-ANILA-Classification
Idempotency-Key
X-CSP-Service-Token
```

Body 至少包含 `request_id`、完整 messages 或 typed input、classification、timeout/max_steps/allowed_tools、requested event protocol、registry snapshot 與 manifest revision。CSP 使用 Agent Integration Key 呼叫 Agent，不轉交 caller bearer token。

## 4.5 StepEvent

```json
{
  "schema_version": "step-event/v1",
  "event_id": "evt_...",
  "sequence": 42,
  "cursor": "...",
  "trace_id": "...",
  "task_id": "...",
  "session_id": "...",
  "invocation_id": "...",
  "run_id": "...",
  "step_id": "step_7",
  "parent_step_id": "step_4",
  "depends_on": ["step_6"],
  "kind": "skill|tool|command|retrieval|agent|model|approval|artifact",
  "status": "queued|running|blocked|completed|failed|cancelled",
  "safe_input_summary": "...",
  "safe_output_summary": "...",
  "agent_id": "...",
  "tool_name": "...",
  "started_at": "...",
  "completed_at": "...",
  "latency_ms": 381,
  "retry_count": 0,
  "error": null,
  "classification": "..."
}
```

`Todo` 表示預定計畫，`StepEvent` 表示實際執行，兩者不可混用。Shell 的「已完成 8」必須由終態事件計算，不由前端猜測。

## 4.6 統一錯誤

錯誤類別至少包含 `contract_error`、`authentication_error`、`authorization_error`、`policy_denied`、`classification_violation`、`agent_unavailable`、`timeout`、`stream_broken`、`cancelled`、`downstream_error`、`internal_error`。每筆都有 `code`、`retryable`、`safe_message`、`trace_id`、`invocation_id` 與可選 `step_id`。

# 5. 工作流與派工計畫

## WS0 — 保護網、基線與 Feature Flags

**目的：** 讓後續替換可 shadow、可比較、可回滾，不在第一個變更就改變正式行為。

**主要工作：**

- 建立 fake CSP、fake routing model、fake Bronze/Silver Agent 與 deterministic fixtures。
- 固定 routing、policy、stream、session 與 ingestion 的現況基線。
- 建立 flags：`ANILA_ROUTER_DECISION_V2`、`ANILA_POLICY_GATE_ENFORCE`、`ANILA_STREAM_BRIDGE_V1`、`ANILA_AGENT_EVENTS_V1`、`ANILA_SESSION_EVENT_STORE`、`ANILA_INGESTION_OUTBOX`、`ANILA_CONTROLLED_MULTI_AGENT`。
- 建立 routing eval dataset，涵蓋明確指派、能力歧義、分類不足、未知 Agent、prompt injection、stale registry、direct-answer 與 clarify。
- 將既有測試失敗、Windows 路徑問題、mypy/ruff 基線分類為 functional、platform、test-staleness，禁止新增未登錄偏差。

**驗收：** 新舊 Router 可同時決策但只有 legacy 回應；新決策有 trace 與差異報表；flags 全關時使用者行為不變；CI 能重現基線。

**回滾：** 關閉新 flags 即回到原正式路徑；保留 shadow telemetry，不修改資料語意。

## WS1 — Canonical Contract Package

**主要檔案：**

- 新增 `packages/anila-core/src/anila_core/router/decision.py`
- 新增 `packages/anila-core/src/anila_core/router/policy.py`
- 新增 `packages/anila-core/src/anila_core/agents/contracts.py`
- 擴充 `packages/anila-core/src/anila_core/api/events.py`
- 提供 CSP、`anila-agent`、Shell 共用 JSON Schema 與 fixtures

**工作：** 固定 RouteDecision、PolicyGateResult、AgentManifest、Invocation、StepEvent、AgentError；定義 version negotiation、unknown-field、unknown-version 與 legacy normalization；用 canonical enum 防止各服務複製漂移。

**驗收：** 四端對同一 fixture 結果一致；未知 major version fail-closed；已知 legacy 可經 adapter 正規化；schema compatibility 測試進 CI。

**相依：** WS0。**回滾：** 保留 v1 schema，不刪 legacy payload；新欄位採 additive。

## WS2 — CSP Registry、Policy 與治理 Read Model

**主要檔案：**

- `services/csp/app/models/agent.py`
- `services/csp/app/schemas/contracts/agents.py`
- `services/csp/app/api/agents/registration.py`
- `services/csp/app/api/agents/health.py`
- `services/csp/app/api/agents/approval.py`
- `services/csp/app/api/proxy.py`
- `services/csp/app/services/proxy/ceiling.py`
- `services/csp/app/services/proxy/headers.py`
- `services/csp/app/services/proxy/service.py`

**工作：**

- 產出 Router internal registry view 與 `registry_snapshot_id/manifest_revision`。
- 定義 `ready_for_dispatch = approved + health ready + manifest valid + Full Trace + trace-test passed + classification sufficient`。
- CSP dispatch 時重新驗證 permission、classification、approval、snapshot 與 endpoint，不信任 Router 單方面結果。
- legacy approved Agents 先產生 readiness gap report，再逐一升級；不直接一次 fail 掉全部現有 Agent。
- Shell 投影移除非必要 endpoint/治理細節；Router 投影由 service identity 取得。

**驗收：** 未核准、unhealthy、trace-test 過期、分類 ceiling 不足或 manifest invalid 的 Agent 零 dispatch；snapshot mismatch 回傳可 replan 的 409；legacy `/v1/agents` consumer 過渡期不壞。

**回滾：** internal view 可關閉，但 CSP 最終 policy enforcement 不回滾；安全 fallback 為 direct-answer/clarify/deny。

## WS3 — RouterRuntime、Structured Decision 與 PolicyGate

**主要檔案：**

- 重構 `packages/anila-core/src/anila_core/api/router_server.py` 為薄 HTTP adapter
- 擴充 `registry/remote_agent_manifest.py`
- 新增 `router/request_context.py`
- 新增 `router/candidate_filter.py`
- 新增 `router/decision_engine.py`
- 新增 `router/policy_gate.py`
- 新增 `router/execution_runtime.py`
- 重用 `engine/query_engine.py`、`engine/lifecycle.py`、`engine/approvals.py`、`coordinator/coordinator.py`

**工作：**

1. `RequestContextBuilder` 正規化 identity、history、session、classification、task 與安全限制。
2. CapabilityFilter 先以 task type、capability、scope、classification、health、approval 做 deterministic filter。
3. DecisionEngine 只在候選集合中產出 structured output；不支援 native JSON schema 的 provider 使用嚴格 parser。
4. PolicyGate 在 dispatch 前檢查 CSP policy obligations；每個 multi-step 也各自檢查。
5. direct-model、tool、retrieval 與 Agent 路徑都必須通過相應 policy；Agent 被拒絕時不得自動 fallback 到 direct model 來繞過限制。Router 不複製 CSP ACL，而是執行 CSP 回傳的權威決策與 obligations。
6. caller system message 僅作不信任 context；內部 routing control prompt 永遠存在。
7. `anila_multi_turn` 改為 server-side 上限，初期最多 3；`max_steps/timeout` 由伺服器 ceiling 限制。
8. direct answer 與 Agent dispatch 分開；routing model 不兼任最終回答與控制命令。
9. `DISPATCH:` 只在 shadow/legacy adapter 存在，不能成為正式 authority。

**驗收：** 100% Agent call 都有有效 RouteDecision 與 allow 的 PolicyGateResult；invalid JSON、prompt injection、unknown Agent、stale snapshot、caller system override 均造成零 downstream call；routing eval 高信心案例 top-1 ≥95%、false dispatch <1%、policy bypass = 0。

**回滾：** v2 先 shadow，再以 Agent/部門為單位 canary；故障時保留 PolicyGate，切至 legacy adapter 或 direct-answer-only。

## WS4 — AgentClient、StreamBridge 與 SessionEventStore

**主要檔案：**

- 收斂 `tools/dispatch_tool.py` 與 `tools/agent_as_tool.py`
- 新增 `agents/client.py`
- 新增 `agents/adapters/openai_compat.py`
- 新增 `agents/adapters/anila_events.py`
- 新增 `agents/conformance.py`
- 新增 `router/stream_bridge.py`
- 新增 `router/session_event_store.py`

**工作：**

- 所有正式 Agent 呼叫統一經 `AgentClient → CSP`。
- 依 manifest profile 選 Bronze legacy 或 Silver event adapter。
- StreamBridge 支援 named event、multiline data、heartbeat、`[DONE]`、malformed event、broken stream、timeout、cancel 與 backpressure。
- 把 StreamBridge 視為信任邊界：依來源角色 allowlist 事件，重新包裝 envelope，由 Router 產生或驗證 event_id/sequence；Agent 不得覆寫 classification、policy、owner、resume 或 Router/CSP 權威欄位。設定 payload 大小上限，未知或越權事件進 trace 後丟棄／終止，不原樣透傳。
- `SessionEventStore` 使用 append-only event、單調 sequence、cursor、owner binding、event dedup 與 idempotency；Router restart 後可 replay。
- 對 client disconnect 明確採 propagate cancel 或 detached mode，禁止留下未知狀態。
- Router 對外提供自己的 answer/resume/cancel；不假設所有遠端 Agent 都有相同 URL。

**驗收：** `[DONE]` 恰好一次；重連不重複執行；restart 可由 cursor replay；owner mismatch 403；duplicate invocation 不重複外部副作用；所有 stream/policy error 轉成 AgentError 與終態 StepEvent。

另須驗證惡意 Agent 偽造 `anila.*`、classification、resume、meta 或 sequence 時，不能改變 policy/session 狀態；即時 StepEvent 是 Full Trace 的安全投影，不建立第二份互相矛盾的稽核真值。

**回滾：** legacy SSE 經 adapter 正規化；Shell 對未知 event 忽略並保留最終文字；EventStore additive 保留。

## WS5 — 官方 anila-agent 升級為 Silver

**主要檔案：**

- `packages/anila-agent/anila_agent/serving/service_wrapper.py`
- `runtime/agent_factory.py`、`runtime/run.py`、`runtime/runstate.py`
- `memory/session.py`、`tracing.py`
- `skills/loader.py`、`runtime/mcp.py`、`triggers/runner.py`
- `pyproject.toml` 與 contract/conformance tests

**工作：**

- 提供 `GET /.well-known/anila-agent.json`、`/ready` 與標準 invoke/stream/cancel；只有 manifest 宣告 `supports_resume=true` 且通過 Gold conformance 時，才開啟 `/sessions/{id}/answer` 與 durable resume。
- 接受完整 messages、session/invocation/trace/task/classification/idempotency，而非只取最後一句。
- 實際建立與使用 session；實作 `run_once_state()` 與 paused run schema version。
- 修正 PostgresSession，使其符合 pinned SDK Session protocol。
- 將 SDK tool、agent、retrieval、approval、artifact、error 事件映射成 StepEvent。
- 只有 manifest 宣告 retrieval 的 Agent 才必須提供 `ANILA_COLLECTION_ID`。
- skills/MCP/triggers 只有接入 factory/config 且通過 conformance 才能宣告 capability；不建立市集。
- Silver readiness 缺 trace endpoint、formal manifest、trace context、classification handling 任一項即失敗。
- 修正 Windows path 測試、24 個 strict mypy 錯誤與所有 touched-code lint。

**等級：**

| 等級 | 用途 | 必要能力 |
|---|---|---|
| Bronze | dev/test | OpenAI-compatible request/response、基本 auth、基本 trace correlation |
| Silver | 正式 Agent 最低要求 | formal manifest、StepEvent、Full Trace、classification、idempotency、health/readiness |
| Gold | durable/HITL/高分類 Agent | Silver + resume、approval、cancel、durable run state、嚴格 conformance |

**驗收：** manifest conformance 全過；Silver 的完整 history/session/stream/cancel/idempotency E2E 全過；Gold 再要求 answer/resume/restart；非 RAG Agent 不設 collection 可啟動；PostgresSession protocol runtime test 通過；Full Trace event completeness 100%；全測試、ruff、strict mypy 通過。

**回滾：** Agent 逐一升級；CSP 可讓尚未升級者維持 Bronze dev-only；SDK downgrade 前 drain/expire paused runs。

## WS6 — Shell Agentic UI 正式接線

**主要檔案：**

- `apps/anila-shell/src/runtime/sse.js`
- `apps/anila-shell/src/app.jsx`
- `apps/anila-shell/src/chat.jsx`
- `apps/anila-shell/src/agentic.jsx`
- `apps/anila-shell/src/toolExecution.jsx`
- 建議新增 `runtime/agentRunReducer.js` 或 `useAgentRun.js`

**工作：**

- 建立單一 reducer：`stepsById`、`stepOrder`、`todos`、`pendingInterrupt`、`sessionId`、`cursor`、`runStatus`。
- send/edit/regenerate/continue/compare 共用同一套事件 callbacks。
- 接上 todo、tool start/finish、agent、retrieval、approval/interrupt、resumed、artifact 與 terminal event。
- MessageBubble 渲染 TodoChecklist、StepTimeline、ToolExecutionWidget、InterruptCard；parent/child 與 dependency 可收合。
- pending approval 時鎖住一般 composer，只允許回答、核准、拒絕或取消。
- conversation 保存 session_id/cursor；reload 後由 EventStore replay。
- 預設只顯示 safe summary，敏感參數與 raw reasoning 永不渲染。

**驗收：** 真實 skill → tool → agent → command → retrieval 流程可重現；pause/approve/deny/resume/cancel/reload replay E2E 全過；重複或亂序事件不產生重複卡片；完成數與狀態完全來自 backend event。

**回滾：** 未識別 event 忽略；保留文字串流；按 conversation/tenant 關閉 timeline。

## WS7 — Ingestion 可靠性、冪等與分類治理

**主要檔案：**

- `services/csp/app/api/ingestion/documents.py`
- `services/csp/app/api/ingestion/jobs.py`
- `services/csp/app/api/ingestion/search.py`
- `services/csp/app/models/ingestion.py`
- `services/csp/app/services/ingestion_queue.py`
- `services/ingestion-worker/src/ingestion_worker/handlers.py`
- `services/ingestion-worker/src/ingestion_worker/main.py`
- `services/ingestion-worker/src/ingestion_worker/parsers.py`
- `services/ingestion-worker/src/ingestion_worker/relations.py`
- `services/csp/migrations/versions/` 新增 additive migrations

**目標 read model：** 不再用單一 `document.status` 同時表示排程、可搜尋與 enrichment。Document 分為 `availability_status`（unavailable/searchable/deleting/deleted）、`processing_status`（idle/dispatch_pending/queued/running/retry_wait/cancel_requested/failed/cancelled）及 `enrichment_status`（not_requested/queued/running/complete/partial/failed/skipped），並保存 `active_generation`、`last_successful_job_id`、`ready_at`。既有 searchable 文件即使 reindex 失敗，仍可使用上一個成功 generation。

### WS7-A：Transactional Outbox

上傳 transaction 同時建立 document、job 與 outbox event，提交後由 dispatcher 送入 Arq，再以 `published_at/attempts/next_attempt_at` 記錄。API 不直接把「enqueue 成功」當作唯一可靠性來源。

同一機制要涵蓋 ingest/reindex、evaluator、relation reresolve、cancel command 與 generation cleanup。Arq 使用 deterministic job id；PostgreSQL outbox 是 durable intent SSOT，Redis 只是 delivery。正式切換前先啟用 dry-run/shadow relay 對帳；不可讓 legacy direct enqueue 與 outbox relay 同時真的派送同一工作。

dispatcher 以 `FOR UPDATE SKIP LOCKED` 支援多副本，具有 available_at、attempt_count、backoff、last_error 與 dead-letter；Redis 建議開 AOF `appendonly yes`／`appendfsync everysec` 以縮短 queue recovery，但 durability 不依賴 Redis。API 在 Redis 故障時仍可回 202，狀態必須是 `dispatch_pending`，且 document/job/outbox/audit 已在同一 transaction 提交。

驗收：enqueue 前後 crash、Redis unavailable、duplicate publish 都不會讓文件永久 pending；同一 outbox event 重送只會讓同一 job 被冪等 claim。

### WS7-B：租約式 Job 狀態機

建議 job 狀態：`accepted → queued → running → succeeded|failed|cancelled|timed_out`，另有 `lease_owner/lease_expires_at/heartbeat_at/attempt_no/error_code/retryable`。worker 以原子 claim 取得租約；reaper 回收過期 running。

錯誤分 transient（Redis/DB/model timeout/temporary service）與 permanent（unsupported format、invalid content、classification violation）。retry 使用資料驅動 backoff/jitter/max attempts，不再只靠固定 `max_tries=3`。

自動重試尚未耗盡時只能進 `retry_wait`，不可先發 terminal failed；新 attempt 清除 read model 的 `completed_at/error`，歷史保留在 attempt/stage。新增 `POST /api/ingestion/jobs/{job_id}:cancel`，queued 由 dispatcher/Arq abort，running 在 stage checkpoint 檢查 cancel；捕捉 `CancelledError`、timeout 與 process termination，靠 lease/watchdog 收斂。

### WS7-C：Generation-level 冪等與原子啟用

每次 reprocess 建立 `generation_id`。parse/chunk/embed 寫入 staging generation；全部必要資料成功後，在單一 transaction 將 generation 設為 active。唯一鍵包含 document、generation 與 chunk identity；重試採 upsert 或先清同 generation，不碰上一個 active generation。

embedding、OCR/VLM、blob checksum 與 relation LLM 在長交易外完成；publish transaction 一次寫 parents、leaves、images、counter delta、active pointer、required stages 與 job terminal。Search 只讀 active generation；失敗 generation 不可見。圖片也帶 generation，避免 reprocess 後舊圖殘留。

Blob 採 temp file + fsync + atomic replace、reference/tombstone 與延遲 GC；delete 先 cancel active job 再 tombstone，GC 前重新檢查 DB reference。建立 reconciler 對帳 pending-without-job、stale lease、chunk/image 重複、counter drift 與 orphan blob。

空內容必須有明確終態：可依政策標為 `failed:EMPTY_CONTENT` 或 `succeeded_with_warning`，但 document/job/stage 三者一致，不得 indexed 後 job 仍 running。

### WS7-D：Stage 與文件可用性分離

stage 至少為 `blob_verify/parse/caption/chunk/embed/publish_index/image_persist/relations_rule/relations_llm/relations_similarity/evaluate`，各有 pending/running/succeeded/failed/skipped/cancelled、required、attempt、progress 與 safe metrics。`search_ready` 僅表示 active generation 的 required stages 可檢索；relation/evaluation 可另行完成，避免一個 `indexed` 混合全部語意。

同步重 CPU parser 移至 thread/process executor 或獨立 worker pool；timeout/cancel 必須在 finally 寫入終態並停止後續 stage。

LLM relation 的外部 HTTP 呼叫不得包在 DB transaction 內，以免 120 秒級 timeout 長占 connection/lock；只將完成後的結果在短 transaction 中驗證與寫入。

Relation enrichment 不作為 P0 search-ready 的必要條件。`similarity_relations.py` 目前每次 ingest 都可能執行 collection-wide centroid self-join，成本近似 O(N²)，超過 `similarity_max_docs` 時直接返回也可能留下 stale edges。P1 應改為 collection-level debounce/dedupe job，提供獨立 enrichment 狀態；超過上限時明確清除、標 stale 或採 ANN prefilter，不可靜默保留舊關係。

### WS7-E：分類傳播與 Search Clearance Gate

- document 建立時固定 effective classification 與來源。
- chunk、parent、relation、image、artifact 必須繼承或提高，絕不可降低；DB constraint/trigger 或 service invariant 保護。
- worker insert/upsert 必須寫入 classification；舊資料 backfill 後才啟用 NOT NULL/constraint。
- search query 加入 `data_classification <= caller_clearance`，並同時保留 owner、collection binding、agent ceiling 與 scope gate。
- retrieval result 回傳有效分類，Router 以單向 classification latch 提升 task classification；不得降回。

**WS7 整體驗收：** 任何 accepted upload 最終都能到 terminal state；stuck nonterminal 超過 lease+grace = 0；worker crash/retry 不產生 duplicate active chunks；舊 active generation 在新 generation 失敗時仍可用；低 clearance 搜不到高分類 chunk；分類 propagation 完整率 100%。

**回滾：** 先 dual-write legacy status 與新 job/stage；outbox dispatcher 可關但資料保留；active generation pointer 可回前一版；clearance enforcement 不回滾為放行，只能 fail-closed。

## WS8 — Eval、Observability、SRE 與受控 Multi-Agent

**工作：**

- 建立 routing、policy、contract、stream、resume、Agent conformance、ingestion fault、UI E2E 八類 gate。
- 每次 invocation 保存 contract version、registry snapshot、route/policy decision、Agent revision、event completeness 與 terminal state。
- 建立 stuck task/job、event gap、classification denial、retry storm、agent readiness、outbox backlog 與 replay failure 告警。
- 補齊 `infra/compose/*` 與正式部署環境的 `ANILA_TRACE_ENDPOINT`、startup preflight、readiness 與 CSP callback smoke test；Full Trace required profile 不可 silent no-op。
- 定義 trace/event 的 RLS、classification、encryption、retention、deletion、容量預估與 archive policy；metrics 僅使用去識別／合成資料，不把機敏 prompt 寫入一般 telemetry。
- Multi-Agent 僅在 single-agent 穩定期後啟用；plan 有步數、深度、平行度、timeout 與 cost ceiling；只有 read-only step 可平行；每一步各自過 PolicyGate；分類只升不降。

**驗收：** required trace completeness 100%、policy bypass 0、session replay duplicate 0、ingestion classification propagation 100%；canary error rate不劣於 legacy 基線；受控 multi-agent flag 預設 off。

# 6. 實施階段與里程碑

以下工期是依文件首頁人力假設估算；若只有 2–3 名工程師，應維持相依順序並延長 Sprint，不應同時削弱契約、測試或治理範圍。

| 階段 | Sprint | 主要交付 | Go/No-Go 條件 |
|---|---:|---|---|
| Phase 0：保護網 | 1 | WS0、canonical schema 草案、feature flags、現況基線、routing eval v0 | flags off 行為不變；CI 可重現；風險有 owner |
| Phase 1：控制契約 | 2–3 | WS1、CSP internal registry、RouteDecision shadow、PolicyGate observe-only | 新舊 route 差異可量測；invalid decision 零 dispatch；legacy consumer 相容 |
| Phase 2：執行與事件 | 4–5 | AgentClient、StreamBridge、EventStore、官方 Agent Silver 最小垂直切片 | 單 Agent tool/retrieval/approval 全程 trace；restart/replay/cancel 通過 |
| Phase 3：Shell 真實流程 | 6 | 共用 reducer、Step timeline、approval/resume/cancel/replay | 附圖式 E2E 來自真實事件；無 raw reasoning/secret 洩漏 |
| Phase 4：Ingestion P0 | 6–7，可與 Phase 3 平行 | outbox、job lease、generation、stage、classification/search gate、fault tests | accepted 必終態；duplicate active chunk=0；clearance bypass=0 |
| Phase 5：正式切換 | 8 | Router authority canary、Agent 逐一升 Silver、SLO/告警、legacy 降級 | canary 不劣於基線；trace/policy/classification 指標達標 |
| Phase 6：受控編排 | 9，條件式 | 限制型 ExecutionPlan/Coordinator 接入 | single-agent 穩定至少一個 release；multi-agent 預設 off |

## 6.1 Critical Path

```text
WS0 → WS1 → WS2/WS3 shadow → WS4 → WS5 vertical slice → WS6 → 正式切換
                  └──────────────────────────────→ WS7 可受控平行
正式切換穩定後 → WS8 controlled multi-agent
```

不可倒置：UI 不早於 StepEvent；PolicyGate enforce 不早於 formal registry；resume UI 不早於 EventStore 與 Agent resume；multi-agent 不早於 single-agent 的 policy、cancel、idempotency、trace 完整。

# 7. 資料庫、API 與相容遷移

## 7.1 Expand → Backfill → Switch → Contract

1. **Expand：** 新增 manifest revision、event store、idempotency、run-state version、outbox、job lease、generation、stage、classification propagation 欄位；先 nullable 或有安全 default。
2. **Backfill：** 建立可重跑腳本，分批補舊 Agent readiness、舊 session/event 起始點、舊 ingestion classification 與 active generation。
3. **Dual-read/write：** Router 同時讀 legacy/formal registry；ingestion 同時更新 legacy document status 與新 job/stage；Shell 同時接受 text 與 StepEvent。
4. **Switch：** 依 Agent、tenant、部門或 collection canary；使用 metrics 驗證後擴大。
5. **Contract：** 至少跨一個穩定 release，再移除 `DISPATCH:` authority、legacy 欄位與舊 callback；資料刪除另立變更單。

### 7.1.1 建議 Ingestion Migration 批次

- `r1_0010_ingestion_reliability_foundation`：擴充 job attempt/lease/cancel/deadline/trace 欄位；新增 outbox、job stages、active-job partial unique index 與 transition constraints。
- `r1_0011_ingestion_generation_publish`：document active generation、chunk/image generation、generation-aware keys/index、blob reference/GC 欄位。
- `r1_0012_ingestion_classification_repair`：依 `max(collection, document)` backfill chunk/image 分類，加入 invariant audit query，再啟用 search clearance enforcement。

Backfill 將既有 indexed 資料設為 generation 1/searchable；逾期 pending/running 若 blob 完整則建立 recovery job/outbox，否則轉 stable failed；重算 document/chunk/bytes counters。舊資料無法合理重建的 stage 標為 `backfilled_unknown`，不得偽造執行歷史。

## 7.2 相容矩陣

| Producer | Consumer | 過渡策略 | 移除條件 |
|---|---|---|---|
| CSP legacy `/v1/agents` | Shell/舊 Router | 預設保持舊 response；internal contract 另路徑或 query version | 所有 consumer 通過 v2 contract |
| Router `DISPATCH:` | legacy path | 只保留 shadow/adapter，記錄使用量 | 連續一個 release 正式流量為 0 |
| Agent legacy SSE | StreamBridge | normalize 成 StepEvent + final text | 正式 Agent 全部至少 Silver |
| Router text SSE | 舊 Shell | StepEvent 旁路，不影響文字 | Shell 新版覆蓋率達標且可回退 |
| ingestion legacy status | API/UI | dual-write 新 job/stage | 舊 UI/API 不再依賴單一 indexed 語意 |
| 舊 chunks 無分類 | Search | 先 backfill、抽樣驗證，再開 clearance enforce | 未分類資料為 0，抽樣錯誤為 0 |

## 7.3 回滾原則

- 所有 migration 初期 additive；不以 rollback 刪資料。
- EventStore append-only；舊程式只需忽略未知 event。
- active generation pointer 可切回上一個成功 generation。
- Policy/classification enforcement 不可回滾成 allow-all；新 runtime 故障時回到 direct-answer/deny/legacy adapter + gate。
- SDK downgrade 前 drain 或 expire paused run，避免 RunState schema 不相容。

## 7.4 Downstream Branch Port Matrix

每個 release candidate 必須保存下表，不得只用主線測試結果代表正式部署：

| 欄位 | `main` | `origin/prod-intranet-card` |
|---|---|---|
| 基準 commit | 本文件列出的 SSOT 或其後核准 commit | 當次 port 前固定 commit |
| 移植方式 | 正常 PR/merge | 人工 cherry-pick/port，逐項處理 SSO/card 差異 |
| 必跑驗證 | 全套 contract/integration/E2E | 同套 gate + card/SSO/air-gap/deploy preflight |
| 產物 | 主線 image/schema revision | 獨立 image digest、migration revision、manifest revision |
| 回滾 | 前一主線 release tag | 前一個正式分支核准 tag，不引用主線 tag 代替 |

目前分支已有 12/11 divergence；第一個 Phase 0 交付應建立實際 commit mapping 與衝突清單。

# 8. 測試、品質門檻與驗證矩陣

## 8.1 現況基線

| 範圍 | 現況 | 計畫處理 |
|---|---|---|
| `anila-core` | 770 passed、6 failed、7 skipped；至少一個 hierarchical chunker 為功能失敗，其餘含 Windows/tool/test-staleness | Phase 0 分類並修正功能失敗；平台差異改為可攜測試 |
| `anila-agent` | 排除 9 個硬編碼 POSIX 路徑測試後 199 passed、1 live skipped；ruff pass；strict mypy 24 errors | 修 path 假設、Session/RunState、mypy；Silver 發布前全綠 |
| ingestion | 146 passed，偏單元/helper；ruff 尚有既有問題 | 增加 PostgreSQL/Redis/fault integration；touched code lint 為 0 |
| Shell | 已有 SSE/agentic/tool 元件單測 | 增加真實 streaming、亂序、重播、approval/cancel E2E |

## 8.2 必要測試層級

| 層級 | 最少案例 | Gate |
|---|---|---|
| Unit | schema validation、candidate filter、policy reason、state transition、classification latch、retry taxonomy | touched module 100% pass |
| Contract | Core/CSP/Agent/Shell 共用 fixtures；版本、unknown field、legacy adapter | 四端同結果；未知 major fail-closed |
| Integration | Router+CSP+fake Agent；PG SessionEventStore；CSP+Redis+worker | 每條正式路徑均可在 CI 重現 |
| Fault injection | enqueue 前後 crash、Redis/PG outage、worker kill、stream break、timeout、duplicate、out-of-order | 無 stuck、無 duplicate side effect、終態唯一 |
| Security | system prompt override、scope/classification bypass、cross-owner resume、secret redaction、SSRF/header forwarding | bypass = 0 |
| Routing eval | 正確 Agent、direct、clarify、deny、ambiguous、stale health | 高信心 top-1 ≥95%；false dispatch <1% |
| E2E | tool/retrieval/approval/resume/cancel/reload；ingestion upload→search | 真實事件可回溯；分類正確 |
| Performance/soak | registry/policy overhead、SSE backpressure、event replay、outbox backlog、large document | 不劣於核准基線；無長期資源洩漏 |

## 8.3 CI Release Gates

- 所有 functional failure = 0；已知 platform skip 必須有 issue、owner 與到期日。
- touched package ruff/lint = 0；新增程式 strict typing = 0；`anila-agent` Silver release 時 full strict mypy = 0。
- Contract、security、fault-injection、routing eval 與核心 E2E 為 blocking checks。
- migration upgrade、backfill rerun、mixed-version、rollback read compatibility 全通過。
- 產物含 SBOM/依賴鎖定；air-gap build 與 CSP trace endpoint 配置 smoke test 通過。

# 9. SLO、KPI 與告警

以下是首版目標，Phase 0 量測基線後由架構與 SRE 核准最終數字。

| 指標 | 目標 | 告警／阻斷 |
|---|---:|---|
| Policy/classification bypass | 0 | 任一筆立即阻斷擴量並啟動安全事件 |
| 必要 Full Trace 完整率 | 100% | <100% 停止新 Agent/Router 擴量 |
| 高信心 false dispatch | <1% | 超標回 shadow 或提高 clarify 門檻 |
| Routing eval top-1 | ≥95% | 未達不得成 authority |
| registry stale dispatch | 0 | 任一筆停止該 Agent 派工 |
| StepEvent sequence gap/duplicate side effect | 0 | gap 告警；副作用重複立即 rollback |
| Session cross-owner access | 0 | 任一筆安全事件 |
| Ingestion accepted→terminal | 100% | 超過 lease+grace 的非終態必告警 |
| Duplicate active chunks | 0 | 立即停止該 collection reprocess |
| 分類傳播完整率 | 100% | 任一漏標停止 search_ready |
| Outbox oldest unpublished age | 依基線核准 | 超過閾值停接大量上傳或降載 |
| Canary error rate | 不劣於 legacy 基線 | 超出 error budget 自動停止擴量 |

# 10. 上線、Canary 與回滾 Runbook

## 10.1 上線順序

1. 部署 additive DB schema 與 dual-read/write，驗證 backfill 可重跑。
2. 部署 CSP formal registry internal view，但 Router 仍只觀察。
3. 部署 Router v2 shadow 與 decision diff dashboard。
4. 部署 AgentClient/StreamBridge/EventStore，先接 fake Agent，再接一個低風險 Silver Agent。
5. 部署 Shell timeline，先對內部測試帳號開啟。
6. 依單 Agent → 單部門 → 10% → 50% → 100% 擴量；每一階段至少跨一個核准觀察窗。
7. Ingestion 依 collection 開 outbox/generation/classification gate，先新資料，再 backfill 舊資料。
8. 單 Agent 路徑穩定一個 release 後，才提出 controlled multi-agent Go/No-Go。

## 10.2 自動停止條件

- 任何 policy/classification bypass、cross-owner resume 或秘密外洩。
- duplicate external side effect、StepEvent 終態不一致或 replay 重新執行。
- canary error/latency 超過核准 error budget。
- Agent registry/readiness 漂移導致錯誤派工。
- Ingestion 出現新 stuck job、active generation 重複或低 clearance 可搜尋高分類資料。

## 10.3 回滾動作

- 關閉指定 tenant/Agent/collection flag，不做全系統盲目回滾。
- Router：v2 authority → shadow；保留 PolicyGate，切 legacy adapter 或 direct-answer-only。
- Agent：停止該 manifest revision，回上一個已核准 revision；paused run 先 drain/expire。
- Shell：隱藏 timeline，保留 final text 與 trace link。
- Ingestion：停 dispatcher/新 generation，active pointer 回上一成功 generation；outbox/job/event 資料保留待重送。

# 11. 安全、分類與稽核設計

## 11.1 分類傳播

`effective_classification = max(user request, conversation, attachment, retrieved source, generated artifact, policy obligation)`，整個 task 採單向 latch，只能升不能降。Router、CSP、Agent、Ingestion 與 artifact store 都要保存與驗證；UI 依 clearance 顯示 safe summary。

## 11.2 Full Trace

正式 Agent 至少記錄 route/policy decision、registry snapshot、invocation headers 摘要、每個 StepEvent、tool/retrieval/artifact 引用、terminal/error 與 approval。完整 payload 依分類隔離、加密、留存與授權；Shell 不直接取得原始 trace payload。

## 11.3 Prompt 與工具安全

- Router internal control prompt 與 caller context 分離；caller system message 永不覆寫控制規則。
- 工具呼叫套用 allowlist、scope、classification、timeout、max steps 與 idempotency。
- URL/tool/command 既有 guardrail 必須在 RouterRuntime 共用，不因 remote Agent 路徑被繞過。
- StepEvent summary 經 redaction；不得含 token、credential、完整機密文件、raw SQL/command secret 或 chain-of-thought。

# 12. 如何與 Agent 開發者溝通轉型

## 12.1 可直接發給開發者的 Brief

> 你不需要更換 Agent framework，也不需要把 Agent 併入 ANILA repo。轉型目標是讓既有 Agent 加上一層 ANILA adapter，通過 CSP 治理與 conformance。正式環境最低為 Silver：提供 formal manifest/health/readiness，接受 ANILA invocation headers 與完整 session input，發送 StepEvent v1，支援 idempotency 與 cancellation，正確處理 classification，並把每個 tool/retrieval/approval/output/error 寫入 Full Trace。Bronze 只供 dev/test；需要 durable pause/resume 或高分類任務時升 Gold。不得把 raw chain-of-thought、秘密或未遮罩 payload 送到 Shell，也不得繞過 CSP 直連。

## 12.2 開發者交付清單

- `/.well-known/anila-agent.json` 與 `/ready`。
- input/output JSON Schema、capabilities、classification ceiling、required scopes、event protocol、trace mode。
- invoke/stream/cancel；Gold 再加 answer/resume 與 durable state。
- `trace_id/task_id/invocation_id/session_id/idempotency-key` 全程回傳與保存。
- 所有能力須真實接線；未接線的 skill/MCP/trigger 不宣告。
- conformance report、threat checklist、air-gap build、dependency lock、rollback image。

## 12.3 驗收流程

1. Bronze sandbox contract test。
2. Manifest/health/schema review。
3. classification、scope、secret redaction 與 Full Trace test。
4. Silver conformance：stream、error、cancel、idempotency、tool/retrieval trace。
5. CSP approval 與單一測試部門 canary。
6. Gold 才執行 resume、approval、durable run-state 與高分類測試。

# 13. RACI 與治理節奏

| 工作 | A（最終負責） | R（執行） | C（諮詢） | I（知會） |
|---|---|---|---|---|
| 契約與目標架構 | 技術負責人 | Core/CSP Lead | Security、Agent、UI、Ingestion | PO、SRE |
| RouterRuntime | Core Lead | Core Team | CSP、Agent SDK、QA | PO、SRE |
| CSP registry/policy | CSP Lead | CSP Team | Security、Core | Agent 開發者 |
| Agent Silver SDK | Agent SDK Lead | Agent Team | Core、CSP、Security | 各 Agent 團隊 |
| Shell Agentic UI | Frontend Lead | Shell Team | Core、UX、Security | PO |
| Ingestion | Data/Ingestion Lead | CSP+Worker Team | Security、SRE、QA | PO |
| Migration/Canary/SLO | SRE Lead | SRE+QA | 各模組 Lead、Security | 全體利害關係人 |
| Go/No-Go | 產品與技術共同 | Release Manager | Security、SRE、各 Lead | 全體 |

治理節奏：每週 architecture/contract review；每個 Sprint demo 真實垂直切片；每次 canary 前安全與 migration checklist；每個 phase 結束依 Go/No-Go 條件簽核，不以「程式已 merge」視為完成。

# 14. 風險登錄表

| 風險 | 等級 | 早期訊號 | 緩解 | Owner |
|---|---|---|---|---|
| Router 大改造成正式中斷 | 高 | 新舊 route 差異、error rate 上升 | thin adapter、shadow、per-Agent flag、direct-answer fallback | Core Lead |
| Router/CSP policy 漂移 | 高 | snapshot mismatch、CSP denial 增加 | CSP 為權威、revision/TTL、dispatch 再驗 | CSP Lead |
| Prompt injection 形成錯誤派工 | 高 | invalid/未知 Agent decision | control prompt 隔離、candidate allowlist、invalid 零 dispatch | Core+Security |
| Agent 宣告能力但未接線 | 高 | conformance/trace 缺事件 | 只從 runtime 實際能力產 manifest；Silver gate | Agent SDK Lead |
| 事件重複或亂序造成副作用 | 高 | duplicate step/工具操作 | event_id/sequence/cursor、idempotency、reducer 去重 | Core+UI |
| UI 洩漏推理或秘密 | 高 | raw payload 出現在 timeline | safe projection、redaction、security E2E | UI+Security |
| Ingestion 接受後永久 pending | 高 | oldest pending/outbox age 上升 | transactional outbox、lease/reaper、終態 SLO | Ingestion Lead |
| 分類未傳播或搜尋越權 | 最高 | chunk 無分類、低權限命中 | backfill、constraint、clearance gate、fail-closed | Security+CSP |
| generation 重試產生重複 chunk | 高 | unique conflict、結果倍增 | staging generation、原子 active pointer | Ingestion Lead |
| SDK 升級破壞 paused RunState | 中高 | resume decode error | state schema version、upgrade 前 drain、compat test | Agent SDK Lead |
| Scope 膨脹為 marketplace/swarm | 中高 | Phase 1 出現自由編排需求 | 非目標寫入 gate；multi-agent 條件式且預設 off | PO+Architect |
| 主線與正式分支誤當相同 | 高 | cherry-pick 衝突、card/SSO smoke 失敗 | downstream port matrix、獨立 image/tag、branch-specific rollback | Release+SRE |
| Full Trace 程式存在但部署未開 | 最高 | readiness 正常但 CSP 無 spans | compose/env/preflight、required profile fail-closed | SRE+CSP |
| 觀測資料量過大 | 中 | event store/trace 成長過快 | retention、分級 payload、summary projection、容量測試 | SRE |

# 15. 優先級 Backlog

## P0 — 未完成不得正式擴量

| ID | 項目 | 完成定義 |
|---|---|---|
| P0-01 | caller system prompt 不可覆寫 Router control prompt | injection tests 零 dispatch |
| P0-02 | RouteDecision + candidate filter + PolicyGate | 所有 call 有 decision/gate；bypass 0 |
| P0-03 | CSP formal registry/readiness | unhealthy/unapproved/classification insufficient 零 dispatch |
| P0-04 | AgentClient + StreamBridge + 統一錯誤 | stream break/cancel/retry 皆有唯一終態 |
| P0-05 | SessionEventStore + owner/idempotency | restart replay、cross-owner 403、duplicate side effect 0 |
| P0-06 | 官方 Agent Silver vertical slice | manifest/session/events/trace/conformance 全過 |
| P0-07 | Shell 真實 StepEvent 接線 | 附圖流程、approval/resume/cancel/reload E2E 全過 |
| P0-08 | Ingestion transactional outbox + lease | accepted→terminal 100%、stuck 0 |
| P0-09 | Ingestion generation 原子切換 | worker crash/retry duplicate active chunk 0 |
| P0-10 | 分類傳播 + search clearance | 未分類 chunk 0、低 clearance 命中 0 |
| P0-11 | Full Trace 部署 guard | 正式 profile endpoint 缺失／不可達時 readiness fail；smoke 可回讀 |
| P0-12 | Downstream branch port gate | main/prod commit mapping、SSO/card/air-gap 測試與獨立 rollback tag 完整 |

## P1 — 正式營運強化

- Agent conformance CLI 與 CI badge；所有正式 Agent 逐一升 Silver。
- Router evaluation dashboard、registry diff、route/policy/trace completeness 告警。
- Ingestion stage UI、reprocess generation、operator retry/cancel/runbook。
- SDK strict typing 歸零、Windows/Linux parity、air-gap release smoke。
- Shell 大量 steps 的虛擬化、收合、artifact 下載與安全摘要體驗。

## P2 — 條件式能力

- Gold Agent 的 durable pause/resume、approval、long-running task。
- 受控 ExecutionPlan 與 Coordinator 接入；步數/併發/分類/cost ceiling。
- 進階 routing offline evaluation、AB/shadow calibration。
- Relation/evaluation stage 的獨立 SLO 與資料品質趨勢。

# 16. 完成與退出條件

本變更只有在下列條件全部成立時才算完成：

- `DISPATCH:` 不再是正式 dispatch authority。
- 100% 正式 dispatch 經 RouteDecision、CapabilityFilter、PolicyGate 與 CSP 最終 enforcement。
- CSP formal manifest/readiness 成為 Router 候選來源；stale/invalid Agent 不可派工。
- 官方 `anila-agent` 至少達 Silver；Session/RunState/protocol/type tests 全通過。
- AgentClient、StreamBridge、SessionEventStore 在正式路徑，restart/replay/cancel/idempotency 有 blocking tests。
- Shell 可由 backend events 重建 tool/retrieval/agent/approval/artifact 步驟，且不顯示 raw reasoning/secret。
- Ingestion accepted→terminal 100%，故障重試無 duplicate active chunks，分類傳播與 clearance gate 100%。
- required Full Trace 完整率 100%，policy/classification/cross-owner bypass 均為 0。
- 正式 compose/env 已啟用 Full Trace，startup/readiness/preflight 能在 endpoint 缺失或不可達時阻止正式 profile 上線。
- `main` 與 `prod-intranet-card` 的 commit port、SSO/card/air-gap 驗收、image digest 與各自 rollback tag 已完成並存檔。
- routing eval、contract、security、fault-injection、migration、E2E 與 soak tests 進 CI。
- legacy interface 只在有 usage telemetry 與明確移除條件下保留。
- Multi-Agent 仍預設關閉；只有 single-agent 穩定一個 release 且另行 Go/No-Go 後可開。

<!-- PAGEBREAK -->

# 附錄 A：檔案變更地圖

| 模組 | 現有關鍵檔案 | 預計變更／新增 |
|---|---|---|
| Router HTTP | `anila_core/api/router_server.py` | 只保留 auth/request/SSE adapter；移出決策與執行邏輯 |
| Router contracts | `anila_core/api/events.py` | `router/decision.py`、`router/policy.py`、`agents/contracts.py` |
| Router runtime | `engine/query_engine.py`、`coordinator/coordinator.py` | `request_context.py`、`candidate_filter.py`、`decision_engine.py`、`execution_runtime.py` |
| Registry | `registry/remote_agent_manifest.py`、`registry/agent_registry.py` | formal manifest、snapshot/revision、TTL/readiness |
| Agent call | `tools/dispatch_tool.py`、`tools/agent_as_tool.py` | `agents/client.py`、adapters、conformance |
| Stream/session | `api/events.py`、既有 session | `stream_bridge.py`、`session_event_store.py`、cursor/replay |
| CSP Agent | `models/agent.py`、`schemas/contracts/agents.py`、`api/agents/*` | internal registry view、readiness、revision、gap report |
| CSP Proxy | `api/proxy.py`、`services/proxy/*` | invocation headers、snapshot/policy/classification 再驗、error mapping |
| Router deployment | `infra/compose/platform.yml`、`infra/compose/dev.yml`、Router env | trace endpoint、startup/readiness/preflight、branch-specific image |
| anila-agent service | `serving/service_wrapper.py` | formal manifest、完整 history/session、events、answer/cancel |
| anila-agent runtime | `runtime/run.py`、`runstate.py`、`memory/session.py` | run_once_state、protocol、state version、idempotency |
| anila-agent capability | `skills/loader.py`、`runtime/mcp.py`、`triggers/runner.py` | 由實際 runtime 接線產 manifest；未接線不宣告 |
| Shell stream | `runtime/sse.js`、`app.jsx` | 單一 reducer/hook、共用 callbacks、replay |
| Shell display | `chat.jsx`、`agentic.jsx`、`toolExecution.jsx` | timeline、interrupt、todo、artifact、安全摘要 |
| Ingestion API | `api/ingestion/documents.py`、`jobs.py`、`eval_runs.py`、`relations.py`、`search.py` | outbox transaction、job/cancel/status API、clearance predicate |
| Ingestion model | `models/ingestion.py`、migrations | outbox、lease、attempt、generation、stage、blob、classification constraints |
| Ingestion services | `services/ingestion_queue.py` | 新增 outbox relay、reconciler、watchdog、blob GC |
| Worker | `ingestion_worker/handlers.py`、`main.py`、`parsers.py`、`pgvector_store.py` | atomic claim/publish、heartbeat、retry taxonomy、executor、terminal finally |
| Relation enrichment | `ingestion_worker/similarity_relations.py`、`llm_relations.py` | 與 core indexing 解耦、debounce/dedupe、stale 狀態與重跑 |

# 附錄 B：關鍵狀態機

## B.1 Task/Invocation

```text
queued → running → blocked_approval → running
   │         │             │
   │         ├─────────────┼→ failed
   │         ├─────────────┼→ cancelled
   │         └─────────────┼→ timed_out
   └────────────────────────→ succeeded
```

每個 terminal transition 只能成功一次；duplicate event 僅回傳既有結果。approval/resume 必須重新驗證 owner、classification 與 policy。

## B.2 Ingestion

```text
accepted → queued → running(parse→chunk→embed→persist→enrich→evaluate)
                      │
                      ├→ retry_wait → queued
                      ├→ failed
                      ├→ cancelled
                      ├→ timed_out
                      └→ succeeded → generation active/search_ready
```

`enrich/evaluate` 可依產品政策標為 optional，不得讓單一 `indexed` 同時表示所有階段。新 generation 失敗時，上一個 active generation 保持可用。

# 附錄 C：Agent Conformance 最小案例

1. Manifest schema、revision、capability 與 readiness。
2. Authentication、scope、classification ceiling、CSP-only invocation。
3. 完整 messages/session 與相同 idempotency-key 重送。
4. tool/retrieval/agent/artifact/approval/error StepEvent。
5. SSE heartbeat、multiline、broken stream、client disconnect、cancel。
6. Full Trace correlation 與 safe UI summary redaction。
7. Silver：stream/cancel/idempotency；Gold：pause/answer/resume/restart。
8. Air-gap dependency/build、SBOM、rollback image。

# 附錄 D：設計審查核准清單

- [ ] 北極星非目標已接受：無 marketplace、無自由 swarm、無 raw reasoning。
- [ ] Canonical contracts 與 owner 已指定。
- [ ] CSP 是最終治理權威，Router 不直連正式 Agent。
- [ ] DB migration/backfill/dual-read/rollback 已演練。
- [ ] Current test failures 已分類，P0 functional failures 有修正單。
- [ ] Security、classification、trace、retention 已簽核。
- [ ] Canary metric、觀察窗、自動停止與回滾 owner 已設定。
- [ ] 第一個 Silver Agent 與第一個 ingestion collection 已選定。
- [ ] Multi-Agent 維持條件式、預設 off。
