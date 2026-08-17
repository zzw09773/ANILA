> ⚠ 2026-08-17 盤點：本檔為【歷史紀錄】——redesign 收斂期的設計文件,保留當時的決策與依據,不代表現況。專案權威＝`PLAN.md`(現況與執行順序),規格＝`SYSTEM-MAP.md`。

> ⚠ **2026-08-01 P2.1 契約更新**：下文若仍描述 `csk-`／`bsk-`／`X-CSP-Service-Token`／
> `CSP_SERVICE_TOKEN` 作為 agent 派工身分，該段已過時。現行＝5 分鐘派工 JWT＋JWKS 驗簽
> （開發者不領鑰匙；三級接入見 `docs/guides/developer-guide.md`）。
> 歷史段落未逐字改寫，以免破壞 redesign 文件結構。

# 09. API and Event Contracts

> Status: draft v0.1  
> Purpose: 定義新 ANILA 的 API、SSE、trace、audit、error、schema versioning 契約。  
> Principle: Contract-first；UI 不直接綁 service 臨時 response。  
> 取證基準：現況（Repo evidence）以 `origin/prod-intranet-card`（v1.2.0 系）為準；本機工作樹與其分歧時以 origin 為準。目標設計與現況相左時，一律以目標為準，現況段落僅作遷移起點對照。

---

## 1. API Groups

```text
/api/*    Control Plane, JWT/cookie, card SSO session
/v1/*     Data Plane, Runtime API Key or trusted session
/internal/* Service-to-service only
```

---

## 2. Task API

```http
POST /api/tasks
GET  /api/tasks/{task_id}
POST /api/tasks/{task_id}/submit
POST /api/tasks/{task_id}/cancel
GET  /api/tasks/{task_id}/events
GET  /api/tasks/{task_id}/trace
```

### Create Task

```json
{
  "task_type": "analyze",
  "input": {
    "text": "分析這些檔案的風險"
  },
  "source_scope": "project",
  "selected_collection_ids": [1, 2],
  "requested_output_type": "answer",
  "classification_level": "營業秘密"
}
```

---

## 3. Source API

```http
POST /api/tasks/{task_id}/source-snapshots
GET  /api/source-snapshots/{snapshot_id}
GET  /api/source-snapshots/{snapshot_id}/citations
```

---

## 4. Model API

```http
GET  /api/models
POST /api/models
PUT  /api/models/{id}
POST /api/models/{id}/enable
POST /api/models/{id}/disable
POST /api/models/{id}/healthcheck

GET  /v1/models
POST /v1/chat/completions
POST /v1/embeddings
```

---

## 5. Agent API

### Control Plane

```http
GET  /api/agents
POST /api/agents
GET  /api/agents/{id}
PUT  /api/agents/{id}
POST /api/agents/{id}/submit
POST /api/agents/{id}/approve
POST /api/agents/{id}/reject
POST /api/agents/{id}/disable
POST /api/agents/{id}/issue-bootstrap
POST /api/agents/{id}/bootstrap
POST /api/agents/{id}/trace-test
```

> 目標新增標示（對照現況，見 §15.1）：上表中 `POST /api/agents`（直接建立）、
> `/submit`、`/disable`、`/trace-test` 皆為**目標新增**，repo 現況無此 route。
> 現況對應：建立走 `POST /api/agents/register`、停用/移除走
> `DELETE /api/agents/{agent_id}`、連線測試走
> `POST /api/agents/{agent_id}/test-connection`。目標設計不因此刪除，
> 遷移時以現況 route 為起點改名/擴充。

### Data Plane

```http
GET  /v1/agents
POST /v1/chat/completions model=<agent_name>
POST /v1/traces/{trace_id}/spans
```

### Session / Resume API（現況，must-preserve）

Agent 暫停（interrupt）後續答的 REST 面，兩端現況已存在：

```http
POST /v1/agents/{agent_name}/sessions/{session_id}/answer   # CSP resume proxy（proxy.py）
GET  /v1/sessions/{session_id}/state                        # Router（router_server.py）
POST /v1/sessions/{session_id}/answer                       # Router resume（emit anila.resumed）
```

搭配回應 header `X-Anila-Session-Id`（pin 後續回合，見 §10）。

---

## 6. Service Registry API

```http
GET  /api/services
POST /api/services
GET  /api/services/{id}
PUT  /api/services/{id}
POST /api/services/{id}/activate
POST /api/services/{id}/disable
POST /api/services/{id}/launch
POST /api/services/{id}/healthcheck
POST /api/services/{id}/audit-callbacks
GET  /api/services/{id}/launches
```

---

## 7. Artifact API

```http
POST /api/artifacts
GET  /api/artifacts/{artifact_id}
GET  /api/artifacts/{artifact_id}/versions
POST /api/artifacts/{artifact_id}/export
GET  /api/artifacts/{artifact_id}/download
```

Studio internal:

```http
POST /internal/studio/jobs
GET  /internal/studio/jobs/{job_id}
```

---

## 8. Classification API

```http
GET  /api/classification/events
POST /api/classification/latch
POST /api/classification/declassification-requests
GET  /api/classification/declassification-requests
POST /api/classification/declassification-requests/{id}/approve
POST /api/classification/declassification-requests/{id}/reject
```

> 歷史備註（設計約束）：repo 曾有 `/declassify` endpoint，於 Sprint 8 X / Phase K
> **刻意移除**——它是違反「classified 單向 latch、只升不降」不變量的 security
> backdoor（前端從未呼叫過）。上表的降級申請 workflow 是**目標新增**，設計時必須
> 尊重該決策：一律走「申請 + 主管批核 + audit」三段式，**絕不**提供直接解除
> latch 的 HTTP surface；誤分類的例外處置維持人工 admin script + audit entry。

---

## 9. Trace API

```http
GET  /api/traces/{trace_id}
GET  /api/traces/{trace_id}/spans
POST /v1/traces/{trace_id}/spans
POST /internal/traces/spans
```

### Trace Span Schema

```json
{
  "span_id": "span_1",
  "parent_span_id": null,
  "trace_id": "trace_1",
  "span_type": "agent_run",
  "name": "risk-agent",
  "status": "ok",
  "started_at": "2026-07-01T00:00:00Z",
  "ended_at": "2026-07-01T00:00:02Z",
  "attributes": {}
}
```

---

## 10. SSE Contract

> 權威接縫契約：`docs/platform/router-sse-contract.md`（首凍版 commit `daa4981`，
> 現行版在 `origin/feat/stage3-typed-terminal`；宣告為 Router ⟷ ANILA UI 的
> **唯一接縫契約、FROZEN for v1**）。本節是 redesign 對該契約的**延伸**，
> 不得與其相牴觸；改事件/型別先改該檔（契約是 SSOT），再同步兩端。

保留並正式化 `anila.*` events。依現況分三層明確標示：

### 10.1 現況 live（有 producer，must-preserve）

```text
event: anila.meta          # 最終 metadata，一回合一次（串流在末尾），shape 見 10.4
event: anila.trace         # 路由/派送步驟 {kind,label,detail,status:"ok"|"error",latency_ms?}
event: anila.reasoning     # 推理串流，payload {"delta": "<str>"}
event: anila.resumed       # Router 於 resume 端點（POST /v1/sessions/{id}/answer）emit，僅 resume 流程觸發
（無 event 行）            # 預設 OpenAI chat.completion.chunk（answer delta）
data: [DONE]               # 終止標記
```

### 10.2 現況已接線但無 producer（RESERVED，勿當 live）

```text
event: anila.tool_call_started
event: anila.tool_call_finished
event: anila.interrupt_requested
event: anila.todos_updated
event: anila.follow_ups
event: anila.spans
```

前端 handler（`sse.js`）與 Router passthrough（`_AGENT_PASSTHROUGH_EVENTS`）
兩端皆已備線，但 repo 內**沒有 emitter**——轉發路徑是活的，agent 有送才會出現
（`interrupt_requested` 需 agent 真的 interrupt 才走到）。凍結契約的規則：
勿為這些事件建主動 UI widget，直到真有 producer；它們是有意保留的預留線。

### 10.3 目標新增

```text
event: anila.retrieval_started
event: anila.retrieval_finished
（Task API 落地後的 task 相關事件）
```

repo 現況無實作（emit、handler、test 皆缺，見 §15.5）；屬 redesign 目標，
落地時依凍結契約的變更流程走（先改契約檔、兩端 lockstep）。

### 10.4 `anila.meta`

現況 load-bearing shape（UI `applyMeta`/`trust.jsx` 依賴，must-preserve）：

```json
{
  "trace_id": "trace_123",
  "trace": [],
  "citations": [],
  "confidence": null,
  "handoff_chain": [],
  "follow_ups": [],
  "latency_ms": 1200,
  "classified": false,
  "usage": { "prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150 },
  "reasoning": "…"
}
```

- `usage` / `reasoning` 為 optional key；`usage` 現況由 **CSP proxy 端**補
  （Router 串流端 usage 歸零，見 §15.10）。
- 串流時 `trace: []`（步驟已由 `anila.trace` 逐一送出，避免重複）；
  非串流一次 bundle。
- **目標新增欄位**：`task_id`、`classification_level`（對照現況：現只有
  `classified` bool 單向 latch）。`classified` 保留給舊 UI；新 UI 應使用
  `classification_level`。

巢狀型別（凍結契約 §3）：

| 型別 | Shape |
|---|---|
| Citation | `{id, title, section?, snippet?, score?, source_uri?, updated_at?}` |
| Confidence | `{level: "high"｜"medium"｜"low", score: number, reasons?: string[]}` |
| HandoffEntry | `{agent_id, label, status, latency_ms?, input_summary?, output_summary?}` |
| Usage | `{prompt_tokens, completion_tokens, total_tokens}` |

非串流（`stream: false`）變體：單一 JSON 回應內含 `anila_meta` key
（同上 shape），與 `choices[0].message.content`、`usage` 並列。

注意：Router **不會**把 agent 的 `anila.meta` 原樣轉發——`_merge_anila_meta`
把 downstream meta 併進 Router 自己的 final meta（`handoff_chain` 前插一筆
`{agent_id:"anila-router", label:"Router dispatch", …}`；`classified` 單向
latch 只升不降）。

### 10.5 終止語意（terminal semantics）

- **現況**：OpenAI chunk `finish_reason` 僅 `"stop"` / `"length"`；
  `length`（被 `max_tokens` 截斷）→ `ANILA_UI` `sse.js` `onFinishReason` →
  UI 顯示「繼續」鈕（Continue Response）。此契約 must-preserve。
- **`event: anila.terminal`**（已凍結於 `feat/stage3-typed-terminal` 分支、待併）：

  ```json
  {"reason": "completed｜max_turns｜aborted｜budget｜length｜error", "detail": "<str>?"}
  ```

  Router 在 `[DONE]` 前 emit、一回合一次（方案 A 獨立事件，不動 OpenAI
  chunk 的 `finish_reason`）。stage3 分支已實作 producer（全串流出口，
  預設 `completed`、dispatch agent 錯誤 `error`；`aborted`/`max_turns`/
  `budget` gated 於 anila-core Phase 2）與 consumer（`sse.js` `onTerminal`
  + bubble badge）。redesign 以此為終止語意基線延伸，不另立新事件。

---

## 11. Audit Event Schema

```json
{
  "audit_event_id": "audit_123",
  "timestamp": "2026-07-01T00:00:00Z",
  "actor_user_id": 1,
  "actor_department_id": 2,
  "action_type": "agent.approved",
  "resource_type": "agent",
  "resource_id": "risk-agent",
  "policy_decision_id": "pd_123",
  "trace_id": "trace_123",
  "result_status": "success",
  "metadata": {}
}
```

---

## 12. Error Shape

**目標**：所有 API 使用統一 envelope：

```json
{
  "error": {
    "code": "POLICY_DENIED",
    "message": "此任務分類高於模型可處理等級。",
    "details": {
      "required_level": "極機密",
      "model_ceiling": "機密"
    },
    "trace_id": "trace_123"
  }
}
```

### Error Codes

```text
AUTH_REQUIRED
CARD_AUTH_FAILED
PERMISSION_DENIED
POLICY_DENIED
CLASSIFICATION_VIOLATION
MODEL_NOT_FOUND
MODEL_UNAVAILABLE
AGENT_NOT_FOUND
AGENT_TRACE_REQUIRED
SERVICE_NOT_FOUND
SERVICE_LAUNCH_DENIED
SOURCE_NOT_FOUND
CITATION_MISSING
TASK_CANCELLED
UPSTREAM_TIMEOUT
INTERNAL_ERROR
```

### 現況對照與遷移備註

- **REST 錯誤**：現況全部 API 走 FastAPI 預設 `{"detail": ...}`；前端
  `api.js:readError` 讀 `data.detail`（fallback `JSON.stringify`）。遷移到
  上方 envelope 時，`readError` 需雙讀（`error.message` → `detail`）過渡。
- **串流錯誤是 in-band，不是 HTTP error**：Router dispatch 中 agent 出錯時
  emit `anila.trace`（`status:"error"`）+ 一段友善文字 chunk
  （「agent 暫時不可用…」），然後照常收尾 `anila.meta` + `data: [DONE]`。
- **CSP resume proxy** 例外：上游 4xx/5xx 或連線失敗時 emit
  `event: error`、payload `{"status": <int>, "detail": "<str>"}`（SSE 內）。
- **開流前失敗**才是 HTTP error：下游連不上 → `502`、逾時 → `504`
  （`proxy_service.py`）。

---

## 13. Versioning

- OpenAI-compatible surface 以 `/v1` 為主，但**現況已有出貨的 `/v2` 面**：
  `POST /v2/embeddings` 已存在（`proxy.py`），且 model 註冊 `api_version=="v2"`
  時 chat 會轉發到下游 `/v2/chat/completions`。「只有 /v1、breaking 才開 /v2」
  的框架需修正為：`/v2` 已部分啟用，新 breaking change 沿用該 per-model /
  per-endpoint versioning 機制。
- ANILA-specific schema has `schema_version`。
- Breaking changes require `/v2` or dual-read migration。
- SSE unknown `anila.*` event must be ignored safely by client.

### Route Compatibility Table（✅ 已拍板 v0.2：實作時據此漸進 rename，避免打壞現有前端）

| Target route | Current route（origin） | Compatibility 行為 | Deprecation 計畫 |
|---|---|---|---|
| `POST /api/agents` | `POST /api/agents/register` | 先 alias（兩者並存、同 handler） | register 標 deprecated，v1.1 移除 |
| `POST /api/agents/{id}/submit` | 無（註冊即 pending） | 目標新增（配合 approval_status 七值） | — |
| `POST /api/agents/{id}/disable` | `DELETE /api/agents/{id}` | DELETE 改為 soft disable 語意，disable 為正式名 | DELETE 保留一版後轉 purge-only |
| `POST /api/agents/{id}/trace-test` | `POST /api/agents/{id}/test-connection` | 兩者並存：connection test ≠ trace test（分關） | 皆保留 |
| `GET /api/models/{id}/health`、`POST /api/models/{id}/healthcheck` | `POST /api/models/{model_id}/health-check` | 統一命名 `health-check`，GET health 目標新增 | 不做三名並存 |
| `/api/services/*` | `/api/platform-links` + `/api/service-access-grants` | platform-links 為 migration seed，services 讀寫雙軌一版 | platform-links 標 deprecated，v1.1 移除 |
| `GET /api/audit-events` | `GET /api/audit-logs` | 保留 `audit-logs` 為正式名（現有 UI 依賴），audit-events 不另開 | 目標文件改用 audit-logs |
| `POST /api/auth/revoke` | `POST /api/auth/logout` + `GET /api/auth/revocations` | 目標新增（card 基線無 /revoke） | — |
| `POST /v1/traces`、`GET /api/traces/{id}` | 無（P0 Full Trace gap） | 全新面，無相容問題 | — |
| `/internal/studio/jobs` | `/api/studio/*`（nginx 直達 anila-studio） | 目標新增（internal 面現況為零 route） | — |

規則：每條 rename 先 alias 再 deprecate，一版重疊期；前端（ANILA_UI／ANILALM／CSP）只在 alias 期後才切新名；contract tests（§14）覆蓋雙名期。

---

## 14. Contract Tests

必做：

```text
contract/test_openai_chat.py
contract/test_sse_events.py
contract/test_trace_spans.py
contract/test_agent_manifest.py
contract/test_service_manifest.py
contract/test_classification_policy.py
contract/test_error_shape.py
```

---

## 15. Repo evidence / 現況補齊

### 15.1 現有 `/api/agents` schema

權威 control-plane API 在 `myCSPPlatform/backend/app/api/agents.py`。
目前建立 agent 的 endpoint 是 `POST /api/agents/register`，不是本檔上方草案的
`POST /api/agents`。

`AgentRegisterRequest` 現有欄位：

```ts
{
  name: string
  endpoint_url: string
  description_for_router: string
  api_version: string = "v1"
  base_model_id: number
  collection_id?: number
  capabilities?: object
  input_schema?: object
}
```

`AgentResponse` 現有欄位：

```ts
{
  id: number
  name: string
  owner_user_id: number
  owner_username?: string
  endpoint_url: string
  api_version: string
  description_for_router: string   // required，非 optional
  base_model_id?: number
  base_model_name?: string
  bound_collection_id?: number
  capabilities: object | null      // dict | None
  health_status: string            // schema 是裸 str，見下註
  approval_status: string
  requires_encryption: boolean
  runtime_config: object | null    // dict | None；PATCH 傳 null = 清除 override
  created_at: string
}
```

`health_status` 註：Pydantic schema 是裸 `str`；讀取時經 `_AGENT_HEALTH_MAP`
正規化成 `healthy | unhealthy | unknown`（DB 可能存 ModelRegistry 詞彙
`online | offline | connecting`，背景 health_checker 會寫入該詞彙，
serialize 時 online→healthy、connecting→unknown、offline→unhealthy）。

其他現有 route 包含：

- `GET /api/agents`
- `GET /api/agents/{agent_id}`
- `PUT /api/agents/{agent_id}`
- `POST /api/agents/{agent_id}/approve`
- `POST /api/agents/{agent_id}/reject`
- `DELETE /api/agents/{agent_id}`
- `POST /api/agents/{agent_id}/encryption`
- `POST /api/agents/{agent_id}/health-check`
- `GET/PATCH /api/agents/{agent_id}/runtime-config`
- `POST /api/agents/{agent_id}/issue-bootstrap`
- `POST /api/agents/{agent_id}/bootstrap`
- `POST /api/agents/{agent_id}/test-connection`
- credentials 與 functions 子資源。

Data-plane manifest 在 `myCSPPlatform/backend/app/api/proxy.py`：
`GET /v1/agents` 回 `{"object":"list","data":[...]}`，每筆以 agent `name`
作為 OpenAI-compatible id，含 `description_for_router`、`endpoint_url`、
`capabilities`、`input_schema`、`requires_encryption`。

### 15.2 Studio endpoint shapes

`anila-studio/app/main.py` 掛載的現有 prefix 是：

- `/api/studio`
- `/api/reports`
- `/api/mindmaps`
- `/api/infographics`
- `/api/datatables`

nginx 對應在 `myCSPPlatform/docker/nginx.conf`：
`/api/(studio|reports|mindmaps|infographics|datatables)/` proxy 到
`anila-studio`。目前沒有 `/internal/studio/jobs`。

現有 job contracts：

- Slides:
  `POST /api/studio/slides/jobs`、`GET /api/studio/slides/jobs/{job_id}`、
  `GET /api/studio/slides/jobs/{job_id}/pptx`、
  `DELETE /api/studio/slides/jobs/{job_id}`。
  Request 是 `GenerateSpecRequest`：
  `collection_id`、`preset`、`extra_instructions?`、`skip_retrieval=false`、
  `theme_override?`。Response 是 `JobStatus`：
  `job_id`、`state`、`step`、`title`、`slide_count`、`defects`、
  `qa_passes`、`error`、`created_at`、`updated_at`。
- Reports:
  `POST /api/reports/jobs`、`GET/DELETE /api/reports/jobs/{job_id}`，download:
  `/api/reports/jobs/{job_id}/download/{fmt}`；request 含
  `collection_id`、`preset`、`extra_instructions?`、`document_ids?`、`top_k`；
  status 含 `references_count`、`sections_count`、`download_urls`。
- Mindmaps:
  `POST /api/mindmaps/jobs`、`GET/DELETE /api/mindmaps/jobs/{job_id}`，download:
  `/api/mindmaps/jobs/{job_id}/download/{fmt}`；request 含
  `collection_id`、`preset`、`seed_query?`、`extra_instructions?`、
  `document_ids?`、`max_depth`、`top_k`；status 含 `node_count`、`download_urls`。
- Infographics:
  `POST /api/infographics/jobs`、`GET/DELETE /api/infographics/jobs/{job_id}`，download:
  `/api/infographics/jobs/{job_id}/download/{fmt}`；request 含
  `collection_id`、`preset`、`seed_query?`、`extra_instructions?`、
  `document_ids?`、`top_k`；status 含 `chart_count`、`download_urls`。
- Datatables:
  `POST /api/datatables/jobs`、`GET/DELETE /api/datatables/jobs/{job_id}`，download:
  `/api/datatables/jobs/{job_id}/download/{fmt}`；request 含
  `collection_id`、`preset`、`seed_query?`、`extra_instructions?`、
  `document_ids?`、`target_columns?`、`top_k`；status 含 `row_count`、
  `column_count`、`download_urls`。

注意：**`/jobs` 沒有 GET list endpoint**——各 artifact 的 `GET`/`DELETE` 只作用在
`/jobs/{job_id}` 單筆（POST 建立、GET 查單筆狀態、DELETE 刪單筆）。redesign 若要
job 列表屬目標新增。

### 15.3 Ingestion search response shape

Text search 在 `myCSPPlatform/backend/app/api/ingestion/search.py`：

```http
POST /api/ingestion/collections/{collection_id}/search
```

`SearchRequest`：

```ts
{
  query: string
  top_k: number = 5
  min_score: number = 0
  document_ids?: number[]
  expand_relations: boolean = false
  relation_types?: string[]
  max_related: number = 5
  min_relation_confidence: number = 0
}
```

`SearchResponse`：

```ts
{
  query: string
  embedding_model: string
  embedding_dim: number
  results: Array<{
    chunk_id: number
    document_id: number
    filename: string
    chunk_key: string
    content: string
    score: number
    metadata: object
    parent_chunk_id?: number
    parent_content?: string
    chunk_type: string
    chunk_level: number
  }>
  related: Array<{
    document_id: number
    filename: string
    title?: string
    relation_type: string
    target_ref: string
    source: string
    direction: "outgoing" | "incoming"
    via_document_id: number
    confidence: number
    chunk_id?: number
    chunk_key?: string
    content?: string
    score?: number
  }>
}
```

Image search 另有：

```http
POST /api/ingestion/collections/{collection_id}/images/search
```

回 `query`、`embedding_model`、`embedding_dim`、`results[]`，每筆含
`image_id`、`document_id`、`page`、`storage_path`、`mime`、`caption`、
`filename`、`score`。

### 15.4 ANILA UI `runtime/api.js` contract

`ANILA_UI/anila-ui/src/runtime/api.js` 現況：

- `config.cspBaseUrl` 來自 `VITE_CSP_BASE_URL`，空值代表 same-origin。
- `config.routerBaseUrl` 來自 `VITE_ROUTER_BASE_URL`，空值代表 same-origin。
- `authRequest(path, options, accessToken)` 對 CSP path 發 request，
  `credentials: "include"`，非 safe method 自 cookie `anila_csrf` 帶
  `X-CSRF-Token`，可選 Bearer token。
- `authRequestWithRefresh` 401 時呼叫 `POST /api/auth/refresh` 重試一次。
- `authMultipart` 用同一套 cookie + CSRF + refresh 流程送 FormData。
- `apiKeyRequest` 是 raw Bearer helper。
- Router session helper：
  `GET /v1/sessions/{session_id}/state` 與
  `POST /v1/sessions/{session_id}/answer`，base URL 走 `routerBaseUrl`。

Conversation contract 主要在
`ANILA_UI/anila-ui/src/runtime/conversations.js`：`/api/conversations`、
`/messages`、`/shares`、`/attachments`、`/handoffs`，以及
`POST /api/conversations/{id}/classify`。

### 15.5 現有 SSE `anila.*` events

Frontend parser 在 `ANILA_UI/anila-ui/src/runtime/sse.js`：

- 具名處理：
  `anila.trace`、`anila.meta`、`anila.reasoning`、
  `anila.interrupt_requested`、`anila.resumed`、`anila.todos_updated`、
  `anila.follow_ups`、`anila.tool_call_started`、
  `anila.tool_call_finished`、`anila.spans`。
- 任何其他 `anila.*` 會交給 `onUnknownEvent`，符合本檔 versioning 原則。
- OpenAI chunk 仍走 default `message` channel。
- streaming request 會在 numeric conversation id 存在時送
  `X-ANILA-Conversation-Id`，支援後端分類 latch。

Router passthrough 在 `anila-core/src/anila_core/api/router_server.py`：

- agent 已命名的 `anila.*` 事件會原樣 pass-through。
- agent 的 typed events 會轉成 `anila.<event>`；白名單包含
  `interrupt_requested`、`resumed`、`todos_updated`、`follow_ups`、
  `tool_call_started`、`tool_call_finished`、`usage_update`、
  `memory_saved`、`compact_triggered`、`agent_summary`、
  `task_notification`。

目前 repo 未找到 `anila.retrieval_started` / `anila.retrieval_finished` 實作；
若保留在 redesign contract，需新增後端 emit 與前端 handler/test。

### 15.6 本檔草案中尚非 repo 現況的 API

以下目前是 redesign target，repo 尚未有對應正式 route：

- `/api/tasks`、source snapshot API。
- `/api/services` service registry / launch API。
- `/api/classification/*` 降級申請與主管批核 API。
  （歷史：曾存在的 `/declassify` endpoint 已被**刻意移除**——security backdoor，
  違反 classified 單向 latch 不變量；新 workflow 必須是「申請 + 批核 + audit」，
  絕不重開直接解除 latch 的介面。詳見 §8 備註。）
- `/api/traces/{trace_id}` 與 `/v1/traces/{trace_id}/spans` REST trace API。
- `/internal/studio/jobs`；現況是 `/api/studio` 與各 artifact prefix。

### 15.7 Auth 契約（現況，must-preserve）

`myCSPPlatform/backend/app/api/auth.py`（prefix `/api/auth`）與 `jwks.py`：

```http
POST /api/auth/register
POST /api/auth/login
POST /api/auth/refresh
POST /api/auth/logout
GET  /api/auth/me
GET  /api/auth/revocations          # ?since=，retention 窗內 ASC
PUT  /api/auth/password
GET  /.well-known/jwks.json         # Cache-Control: max-age=3600, public
```

card 分支（origin/prod-intranet-card）另有：

```http
GET  /api/auth/card/challenge
POST /api/auth/card/verify
GET  /api/auth/card/registration/departments
POST /api/auth/card/complete-registration
```

Session 機制：

- Cookie：httpOnly `anila_access_token` / `anila_refresh_token` +
  非 httpOnly `anila_csrf`（double-submit）；mutating request（非
  GET/HEAD/OPTIONS）必帶 `X-CSRF-Token`（值取自 `anila_csrf` cookie）。
- Logout / 改密碼 = `users.token_version` bump + 清 cookie；JWT 帶 `tv`
  claim，比對不符即拒——舊 token 全數失效。
- 跨服務撤銷同步架構：durable `token_revocations` 表（migration 0036）+
  `GET /api/auth/revocations` 讀面 + Redis channel
  `anila:auth:token-revoke`（publisher 模組，best-effort）；anila-studio
  啟動時 cold-start replay `GET /api/auth/revocations?since=` + 訂閱
  pubsub，Redis 失聯 **fail-closed**（503）。
- 接線備註：在 origin 基準（v1.2.0+6），bump 路徑尚未寫入
  `token_revocations` / publish，`POST /api/auth/revoke` 亦不存在；該接線
  （含 `/revoke` route）已完成於本機尚未推送的 card 後續 commits，落地後
  本節升級為完整鏈路。

### 15.8 身分／關聯 headers（現況）

`proxy_service.py` 兩套出向 header builder，**目的地驅動、不可混用**：

- **對 agent**（`build_agent_headers`）：`X-CSP-Service-Token`（per-agent
  credential，5 分鐘快取，fallback legacy env token）+
  `X-ANILA-User-Id` = **員編**（`downstream_identity`；非卡帳號省略、
  絕不偽造，request 照常進行）+ `X-ANILA-User-Email`。
  `X-ANILA-User-Groups`：builder 有參數、agent SDK 有 reader，但 CSP 端
  **沒有任何 caller 傳入** → dead plumbing，從未發出。
- **對 model gateway**（`build_model_gateway_headers`）：只帶
  `X-ANILA-User-Id`（員編，可追溯）；**絕不**帶 `X-CSP-Service-Token` /
  Email / Groups；`_apply_gateway_auth` 只在 model 呼叫加
  `Authorization: Bearer MODEL_GATEWAY_API_KEY`（不覆蓋既有
  Authorization）。agent 的 service token 永不送往 model。
- **關聯**：request header `X-ANILA-Trace-Id` → `token_usage.trace_id`
  attribution；`X-ANILA-Conversation-Id` → 分類 latch + usage
  conversation attribution。回應 header `X-Anila-Session-Id` → pin
  resume 回合（見 §5 Session API）。

### 15.9 CSP proxy 對 `anila.meta` 的過站行為（現況）

`proxy_service.py:proxy_stream` / `proxy_request`：

- **強制 usage**：轉發前改寫下游 request body 加
  `stream_options: {"include_usage": true}`。
- **in-transit 單向 classified 升級**：agent `requires_encryption` 時，
  過站的 `anila.meta` 若 `classified` 為 false 會被改寫成 `true`
  （只升不降）。
- **[DONE] 扣留 + fallback meta**：`[DONE]` 區塊先扣住；串流走完若下游
  沒送過 `anila.meta`，CSP 合成 `build_default_anila_meta`（帶**真實
  usage**——下游回報或伺服端估算）先 emit，再放行 `[DONE]`。UI 因此
  永遠拿得到 meta。
- **非串流**：回應 payload 無 `anila_meta` 時注入 default meta；已有
  meta 且 requires_encryption 時升級 `classified=true`。

### 15.10 Usage／計量契約（現況）

- 寫入：`enqueue_usage`（usage_writer，非同步佇列）落 `token_usage`——
  `api_key_id` / `user_id`（DB PK，非員編）/ `department_id` / `model_id`
  + `conversation_id` / `trace_id` / `caller_agent_id` / `caller_client_id`
  attribution；下游未回報 usage 時伺服端估算並記 warning。
- 讀取面 `/api/usage/*`：`summary`、`chart`、`top-models`、`top-users`、
  `top-departments`、`top-agents`、`by-base-model`、`by-client`、
  `agents/{agent_id}`、`legacy-token-stats`、`export`（CSV）。
- 已知缺口：**Router 端串流 usage 現況歸零**（凍結契約 §6 列管）——
  per-message token 數目前靠 CSP proxy 補進 meta 的 usage；redesign 的
  計量設計須以補齊 Router 端真實計數為目標，不得倒退。
