> ⚠ 2026-08-17 盤點：本檔為【歷史紀錄】——redesign 收斂期的設計文件,保留當時的決策與依據,不代表現況。專案權威＝`PLAN.md`(現況與執行順序),規格＝`SYSTEM-MAP.md`。

> ⚠ **2026-08-01 P2.1 契約更新**：下文若仍描述 `csk-`／`bsk-`／`X-CSP-Service-Token`／
> `CSP_SERVICE_TOKEN` 作為 agent 派工身分，該段已過時。現行＝5 分鐘派工 JWT＋JWKS 驗簽
> （開發者不領鑰匙；三級接入見 `docs/guides/developer-guide.md`）。
> 歷史段落未逐字改寫，以免破壞 redesign 文件結構。

# 05. Agent Registry and Runtime Protocol

> Status: draft v0.1  
> Purpose: 定義所有 Agent 如何從 OpenWebUI 暫存註冊遷移到 CSP，並以 Full Trace 級別接入 ANILA。  
> Scope: anila-agent、LangChain、OpenWebUI Pipe-compatible、custom HTTP/OpenAI-compatible Agent。  
> 取證基準：現況（Repo evidence）以 `origin/prod-intranet-card`（v1.2.0 系）為準；本機工作樹與其分歧時以 origin 為準。目標設計與現況相左時，一律以目標為準，現況段落僅作遷移起點對照。

---

## 1. Core Decision

Agent 是受控能力，不是使用者逛的 marketplace。

```text
Developer creates Agent
→ CSP Agent Registry
→ Admin approval
→ Permissions / Classification / Full Trace policy
→ ANILA Task Service / Router dispatch
→ Agent emits trace events
→ CSP audit / usage / trace
```

OpenWebUI 不再是正式註冊平台；它只是目前 ML Team Agent 的暫存狀態。

---

## 2. Agent Registration Levels

因使用者要求 Full Trace，正式 v1 只允許 L3 進正式任務。但為降低遷移成本，保留過渡層。

| 等級 | 用途 | 是否可進正式任務 | 要求 |
|---|---|---|---|
| L0 Catalog | 盤點 OpenWebUI 現有 Agent | 否 | 名稱、擁有者、描述 |
| L1 Proxy-compatible | 測試呼叫 | 否，僅 dev/test | OpenAI-compatible endpoint，經 CSP proxy |
| L2 Run Protocol | 受控測試 | 低機敏任務可例外 | `/runs` + basic events |
| L3 Full Trace | 正式 | 是 | Run/step/tool/retrieval/model/output/error spans |

v1 正式 policy：

```text
approved Agent 必須 audit_level = full_trace。
```

---

## 3. AgentDefinition Schema

```ts
AgentDefinition {
  id: number
  name: string
  owner_user_id: number
  owner_department_id?: number

  runtime_type:
    | "anila_agent"
    | "langchain"
    | "openwebui_pipe_compatible"
    | "openai_compatible_agent"
    | "custom_http"

  endpoint_url: string
  manifest_url?: string
  healthcheck_url?: string
  api_version: "v1"

  description_for_router: string
  supported_task_types: string[]
  input_schema?: object
  output_schema?: object
  capabilities?: object

  base_model_id?: string
  bound_collection_id?: number
  allowed_tool_ids: string[]

  audit_level: "full_trace"
  classification_ceiling: ClassificationLevel
  default_classification_level: ClassificationLevel

  runtime_config?: object

  approval_status:
    | "draft"
    | "pending_connection_test"
    | "pending_trace_test"
    | "pending_security_review"
    | "approved"
    | "rejected"
    | "disabled"
  // v0.2（目標新增）：審批拆為三關——連得上（connection test）、
  // trace 過（trace-test；Full Trace 是 approval blocker，不是 enhancement）、
  // 安全審查——避免「能連上」與「能正式用」混為一談。
  // 現況 enum 僅 pending / approved / rejected；其餘值為目標新增
  //（見第 12 節 Refactor 與第 13 節現況 schema）。
  created_at: string
  updated_at: string
}
```

---

## 4. Manifest Contract

```http
GET /.well-known/anila-agent.json
```

```json
{
  "agent_id": "risk-analysis-agent",
  "name": "風險分析 Agent",
  "version": "1.0.0",
  "runtime_type": "langchain",
  "api_version": "v1",
  "supported_task_types": ["analyze", "compare", "draft"],
  "description_for_router": "分析風險、找出檔案差異並產出風險表。",
  "input_schema": {},
  "output_schema": {},
  "capabilities": {
    "retrieval": true,
    "tools": ["document_search", "table_reader"],
    "streaming": true
  },
  "trace": {
    "required": true,
    "protocol": "anila-full-trace-v1",
    "callback_mode": "sse_and_post"
  },
  "classification": {
    "ceiling": "機密",
    "default": "營業秘密"
  }
}
```

---

## 5. Runtime Invocation Contract

### OpenAI-compatible Chat

CSP dispatch to agent（目標契約）:

```http
POST {agent.endpoint_url}/v1/chat/completions
X-CSP-Service-Token: <agent integration key>
X-ANILA-User-Id: <employee_id>
X-ANILA-User-Email: <email>
X-ANILA-Task-Id: <task_id>                    # 目標新增
X-ANILA-Trace-Id: <trace_id>                  # 目標新增
X-ANILA-Classification-Level: <level>         # 目標新增
Content-Type: application/json
```

現況（origin/prod-intranet-card）dispatch 實際形狀：

- 送出 `X-CSP-Service-Token`＋`X-ANILA-User-Id`＋`X-ANILA-User-Email`。`X-ANILA-User-Id` 的值是**員編**（`downstream_identity()`）；非卡片帳號（如 admin）取不到員編時 fail-safe **省略**該 header、請求照常進行，不偽造身分。
- Header 由**目的地驅動**的雙 builder 組出（`app/services/proxy_service.py` 的 `build_agent_headers` / `build_model_gateway_headers`）：只有 agent 拿得到 service token＋完整身分；model gateway 永遠拿不到 service token，只帶員編。
- `X-ANILA-User-Groups`：builder 有參數管線，但目前**沒有任何呼叫點傳入**（dead plumbing）。
- `X-ANILA-Task-Id`／`X-ANILA-Trace-Id`／`X-ANILA-Classification-Level` 現況 dispatch **不送**；CSP 目前只讀取**入向**的 `X-ANILA-Trace-Id` 作 usage 歸因（`proxy.py`）。三者保留於目標契約，屬目標新增。

Body remains OpenAI-compatible:

```json
{
  "model": "agent-name",
  "messages": [],
  "stream": true,
  "metadata": {
    "task_id": "task_123",
    "trace_id": "trace_123",
    "source_snapshot_id": "snap_123"
  }
}
```

---

## 6. Full Trace Protocol

> ⛔ **P0 Full Trace Implementation Gap**（與 doc 09 §10、doc 10 Slice 4 同一 P0）：
> 本節是**全新工程**，不是既有功能的正式化——`/v1/traces/{trace_id}/spans` 與
> `trace_spans` table 尚未存在、`anila_trace_sdk` 尚未存在、anila-agent
> `service_wrapper` 現況刻意不外送 tool / reasoning trace、anila-core tracing hooks
> 尚未接入 Router dispatch 與 Agent runtime、`anila.spans` 前端已接線但無 producer
> （RESERVED）。正式 Agent approval 必須 blocked until trace-test passed。

Agent must emit trace events by SSE and/or callback.

### SSE Event

```text
event: anila.spans
data: {"spans":[...]}
```

### Callback

```http
POST /v1/traces/{trace_id}/spans
Authorization: Bearer <agent integration key>
```

### Span Shape

```json
{
  "span_id": "span_abc",
  "parent_span_id": "span_parent",
  "trace_id": "trace_123",
  "span_type": "tool_call",
  "name": "document_search",
  "status": "ok",
  "started_at": "2026-07-01T00:00:00Z",
  "ended_at": "2026-07-01T00:00:03Z",
  "agent_id": "risk-analysis-agent",
  "attributes": {
    "collection_ids": [1],
    "query": "..."
  }
}
```

### Required Span Types

```text
agent.run.started
agent.step.started
agent.step.finished
agent.model_call.started
agent.model_call.finished
agent.tool_call.started
agent.tool_call.finished
agent.retrieval.started
agent.retrieval.finished
agent.output.started
agent.output.finished
agent.error
agent.run.finished
```

### 現況對照：既有 live SSE trace 通道（`anila.spans` 的遷移基礎）

`anila.spans` 不是 greenfield 協定；Router 今天已有一條 live SSE trace 通道，`anila.spans` 必須定位為它的擴充／替代：

- Router 即時發出 `anila.trace`（step 物件 `{kind, label, detail, status, latency_ms}`）與 `anila.meta`（`{trace_id, trace[], citations, confidence, handoff_chain, follow_ups, latency_ms, classified}`），另有 `anila.resumed`（session 續跑回音）。
- 下游 agent 的具名事件經 11 項白名單 pass-through 為 `anila.<name>`：`interrupt_requested`、`resumed`、`todos_updated`、`follow_ups`、`tool_call_started`、`tool_call_finished`、`usage_update`、`memory_saved`、`compact_triggered`、`agent_summary`、`task_notification`（`router_server.py` 的 `_AGENT_PASSTHROUGH_EVENTS`）。
- 例外：agent 發出的 `anila.meta` **不會**原樣轉發 —— Router 把它截為 `downstream_meta`，與自身 trace／handoff_chain 合併後才發出最終 `anila.meta`。
- ANILA UI 已消費這些事件（trace 面板、interrupt／todo 流程、classified latch）；串流路徑的最終 meta 刻意帶 `trace: []`（既有決策：步驟已即時逐筆發出，最終 meta 不重複攜帶、也未持久化）。

因此導入 `anila.spans` 必須提供上述事件的相容或映射路徑，不可讓現有 UI 消費契約中斷。

---

## 7. Tool and Retrieval Trace

Agent 讀資料不可成為黑盒。

### Retrieval Span

```json
{
  "span_type": "retrieval",
  "attributes": {
    "source_scope": "project",
    "collection_ids": [12],
    "document_ids": ["doc_1"],
    "chunk_ids": ["chunk_1", "chunk_2"],
    "top_k": 8
  }
}
```

### Tool Span

```json
{
  "span_type": "tool_call",
  "attributes": {
    "tool_name": "search_documents",
    "input_redacted": true,
    "output_preview": "...",
    "classification_level": "機密"
  }
}
```

---

## 8. Agent Authentication

保留既有 bootstrap/token 設計：

```text
Admin issues bootstrap token
→ Agent exchanges bootstrap token
→ CSP stores agent credential
→ CSP dispatch uses per-agent service token
→ token can rotate/revoke
```

現況（origin）補充 —— 同一把 csk- 已是雙角色金鑰：

- 入向：Router／CSP dispatch 帶 `X-CSP-Service-Token`，agent 端 fail-closed 驗證。
- 出向：agent 對 CSP collection search 的 RAG 憑證。`anila-agent` 的 service_wrapper 直接重用 agent 自己的 csk-（`CSP_SEARCH_TOKEN` 未設時退回 `CSP_SERVICE_TOKEN`；S-Q1 單金鑰設計，僅限其綁定的 collection）。

改名為 Agent Integration Key 時必須保留此雙角色語意，不是只換入向驗證的名字。

Registry 重設計應一併保留的既有 onboarding 資產（現況已存在，目標設計不可退步）：

- csk- inbound-guard onboarding snippets：`myCSPPlatform/frontend/src/components/agents/inboundGuardSnippets.js`（issue-static／rotate／註冊精靈 step-2 一次性顯示 csk- 時，附 fail-closed middleware 範例與出向 RAG 用法）。
- dispatch-credential badge：`DeveloperAgentsView.vue` 鏡射後端「最近 issued-or-rotated」的憑證挑選邏輯，標示 dispatch 實際使用的那把憑證。
- Agent 樣板下載：`GET /api/agents/template/download`。
- System prompt 產生器：`POST /api/agents/system-prompt/suggest`。

UI 命名：

```text
不要叫 CSK
改叫 Agent Integration Key
```

---

## 9. Runtime Types

### anila-agent

官方建議路徑。提供：

- `anila-agent init`
- `anila-agent register`
- `anila_trace_sdk`
- default Full Trace middleware
- CSP retriever
- model provider 預設指向 CSP

### LangChain

提供 adapter：

```python
from anila_trace_sdk.langchain import AnilaCallbackHandler

handler = AnilaCallbackHandler(
    trace_id=os.environ["ANILA_TRACE_ID"],
    agent_id="risk-analysis-agent",
    callback_url=os.environ["ANILA_TRACE_CALLBACK_URL"],
)
```

### OpenWebUI Pipe-compatible（future / reference / manual migration only，不列入 v1）

> ✅ 已拍板（2026-07-02，見 doc 06 文首）：`wrap_pipe` bridge 不列入 v1 範圍、
> 不排程；本小節僅保留作日後參考。`openwebui_pipe_compatible` runtime type
> 保留為預留值，v1 不作為交付項。既有 Pipe agent 一律人工重新註冊。

不把 OpenWebUI 當 host；只提供 wrapper / converter（附錄性質設計）：

```python
from anila_openwebui_bridge import wrap_pipe

Pipe = wrap_pipe(
    original_pipe=OriginalPipe,
    agent_id="foo",
    trace_level="full"
)
```

bridge API 統一命名為 `wrap_pipe`（doc 06 §5 同名；不再使用 `traced_pipe`）。

### Custom HTTP

必須實作：

```text
GET  /.well-known/anila-agent.json
GET  /health
POST /v1/chat/completions
POST /anila/trace-test
```

---

## 10. Router Integration

Router discovery:

```text
GET CSP /v1/agents
```

回傳使用者可用且已 approved 的 Agent manifest。Router 不直接讀 DB。

Router dispatch:

```text
Router
→ CSP /v1/chat/completions model=<agent-name>
→ CSP resolves agent
→ Agent endpoint
```

Router 不直接打 agent endpoint。

---

## 11. Classification

Agent 有：

```text
classification_ceiling
default_classification_level
bound_collection_id classification
```

執行時：

```text
effective_task_level = max(task level, source snapshot level, agent default level)
allow only if effective_task_level <= agent.classification_ceiling
```

---

## 12. Migration From Current Implementation

### Keep

- `agents` table。
- `UserAgentPermission` / `ApiKeyAgentPermission`。
- `bound_collection_id`。
- per-agent bootstrap token / service token。
- `runtime_config`。
- `/v1/agents` discovery。
- `description_for_router`。
- CSP proxy service token injection。

### Refactor

- `requires_encryption` → `classification_policy`。
- `capabilities` JSON → formal manifest schema。
- `runtime_config` → typed sections: tools / workspace / guardrails / trace。
- `approval_status` 由三值擴為七值（draft / pending_connection_test / pending_trace_test / pending_security_review / approved / rejected / disabled，見 §3）——「能連上」與「能正式用」分關把守。
- add Full Trace required fields。
- agent non-streaming usage gap 必須修。

### Remove / Block

- 未 Full Trace 的 Agent 不可進正式任務。
- Agent 內直接拿模型 API Key。
- （現況更正）「Agent 直接查 CSP search JWT-only endpoint」的狀態並不存在：collection search endpoint 已接受 agent 的 csk- 作 Bearer（經 `verify_service_token` 解析），以 agent OWNER 身分執行、硬鎖在 `bound_collection_id`（S-Q1 單金鑰設計；`app/api/ingestion/search.py` 約 :69–110）。與目標設計的實際落差是 per-user 變體（以 `X-ANILA-User-Id` 落實使用者身分＋per-user RLS scope），屬目標新增。
- 開發者在 OpenWebUI 註冊後自動可用於 ANILA。

---

## 13. Repo evidence / 現況補齊

### 現有 Agent DB schema

`myCSPPlatform/backend/app/models/agent.py` 目前已具備：

```text
Agent.id
Agent.name
Agent.owner_user_id
Agent.base_model_id
Agent.bound_collection_id
Agent.endpoint_url
Agent.api_version
Agent.description_for_router
Agent.input_schema
Agent.capabilities
Agent.health_status              # unknown / healthy / unhealthy；health loop 也可能寫 online/offline
Agent.approval_status            # pending / approved / rejected
Agent.requires_encryption
Agent.approved_by
Agent.approved_at
Agent.created_at
Agent.bootstrap_token_hash
Agent.bootstrap_token_expires_at
Agent.bootstrap_token_consumed_at
Agent.bootstrap_token_issued_by
Agent.runtime_config
```

已存在但尚未 formalize 成第 3 節 schema 的欄位：

- `requires_encryption`：目前是分類 migration bridge。
- `runtime_config`：JSONB open shape，已支援工具權限 / workspace / guardrails 類設定。
- `bound_collection_id`：Agent 的 csk- 可被硬綁到單一 collection。

尚未存在：

```text
runtime_type
agent_version
audit_level
classification_ceiling
default_classification_level
trace_callback_mode
manifest_url
healthcheck_url
owner_department_id
supported_task_types
output_schema
allowed_tool_ids
```

### 現有 `/api/agents` endpoint surface

`myCSPPlatform/backend/app/api/agents.py` 目前提供：

```text
GET    /api/agents/template/download
POST   /api/agents/register
GET    /api/agents
GET    /api/agents/{agent_id}
PUT    /api/agents/{agent_id}
POST   /api/agents/{agent_id}/approve
GET    /api/agents/{agent_id}/runtime-config
PATCH  /api/agents/{agent_id}/runtime-config
POST   /api/agents/{agent_id}/encryption
POST   /api/agents/{agent_id}/health-check
POST   /api/agents/{agent_id}/reject
DELETE /api/agents/{agent_id}

POST   /api/agents/{agent_id}/issue-bootstrap
POST   /api/agents/{agent_id}/bootstrap
POST   /api/agents/{agent_id}/credentials/issue-static
POST   /api/agents/{agent_id}/test-connection
GET    /api/agents/{agent_id}/credentials
POST   /api/agents/{agent_id}/credentials/{credential_id}/rotate
DELETE /api/agents/{agent_id}/credentials/{credential_id}
GET    /api/agents/{agent_id}/credentials/me
# HISTORICAL (removed): GET /api/agents/me/runtime-config
#   — agent self-fetch + etag poller withdrawn; see runtime_config section.

GET    /api/agents/{agent_ref}/functions
POST   /api/agents/{agent_ref}/functions
PUT    /api/agents/{agent_ref}/functions/{function_id}
DELETE /api/agents/{agent_ref}/functions/{function_id}
POST   /api/agents/system-prompt/suggest
```

`AgentRegisterRequest` 現況必填：

```text
name
endpoint_url
description_for_router
base_model_id
```

可選：

```text
api_version = v1
collection_id
capabilities
input_schema
```

註冊與更新 endpoint 都會跑 SSRF guard；已 approved 的 Agent 變更 endpoint 會退回
`pending` 重新審核。`/api/agents/{id}/test-connection` 會使用該 Agent 實際要收到的
csk- 對 `{endpoint_url}/v1/chat/completions` 發空 messages，測 token 是否被接受。

### Agent credential bootstrap / rotation / revocation

`myCSPPlatform/backend/app/services/service_token_envelope.py` 定義 token shape：

```text
bsk-<43 chars>    # bootstrap token，單次交換，CSP 僅存 hash
csk-<43 chars>    # service token / integration key
enc::v1::<...>    # envelope 加密儲存 plaintext csk-
```

`myCSPPlatform/backend/app/models/agent_credential.py` 的 per-agent credential 欄位支援：

```text
service_token_envelope
service_token_lookup_hash
service_token_previous_envelope
service_token_previous_lookup_hash
service_token_previous_expires_at
service_token_issued_at
service_token_rotated_at
client_cert_fingerprint
is_active
revoked_at
revoked_by
```

`myCSPPlatform/backend/app/services/agent_credential_service.py` 的現有 lifecycle：

- admin 發 `bsk-`：`issue_bootstrap_token(...)`，預設 15 分鐘 TTL。
- Agent 以 `bsk-` 交換 `csk-`：`consume_bootstrap_token(...)`，會檢查
  registered `endpoint_url`，並以 CAS 防 replay。
- owner/admin 直接發 static `csk-`：`issue_static_credential(...)`。
- admin rotate credential：`rotate_agent_credential(...)`，舊 token 進
  previous envelope，預設可保留 grace window。
- admin revoke credential：`revoke_agent_credential(...)`，soft revoke 並清 token cache。
- `verify_service_token(...)`（`agent_credential_service.py:111`）先查 `service_clients`，
  再查 `agent_credentials`，回傳 `CallerIdentity(kind="service_client" | "agent", ...)`。

結論：現有 credential lifecycle 足以支援正式 Agent Integration Key 的 bootstrap、
rotation、revocation；但 Full Trace callback 的 endpoint 與 token scope 尚未獨立建模。

### `runtime_config` 與 agent self-fetch

`Agent.runtime_config` 是 JSONB open shape。`PATCH /api/agents/{id}/runtime-config`
採 replace semantics：

- `null`：清除 override，Agent 回到 code defaults。
- `{}`：管理員明確設定為空設定。
- 未做 deep merge。

> **HISTORICAL (removed):** `GET /api/agents/me/runtime-config` 曾允許
> Agent 以自己的 `X-CSP-Service-Token` 讀設定並回傳 etag 做輪詢 hot
> reload。該端點與 agent 端 `RuntimeConfigPoller` 已移除；admin
> `PATCH /api/agents/{id}/runtime-config` 維持 410。治理 UI 對
> `runtime_config` 為唯讀檢視。

### Permission model

現有權限表：

```text
user_agent_permissions(user_id, agent_id)
api_key_agent_permissions(api_key_id, agent_id)
```

`myCSPPlatform/backend/app/services/api_key_service.py` 的 `check_agent_permission`
邏輯：

- owner/admin bypass。
- API key caller 先看 `ApiKeyAgentPermission`，再看 key owner 的
  `UserAgentPermission`。
- JWT/cookie caller 看 `UserAgentPermission`。

這可支援「使用者可見 Agent」與「特定 `sk-*` 可呼叫 Agent」兩層授權。

### Router / discovery 現況

Data plane `GET /v1/agents` 在
`myCSPPlatform/backend/app/api/proxy.py`，只回 approved 且 caller 有權限的 Agent
manifest，回應是 OpenAI-style `{"object": "list", "data": [...]}` envelope，wire 欄位包含：

```text
id                       # wire 欄位；值＝agent 的 name（非 DB 數字 id，也不叫 agent_id）
name
description_for_router
endpoint_url
capabilities
input_schema
requires_encryption
```

`anila-core/src/anila_core/registry/remote_agent_manifest.py` 以 caller 的 Bearer token
呼叫 CSP `/v1/agents`；其 `agent_id` 只是 client 端 dataclass 屬性（由 wire 欄位 `id` 填入），
不是 wire 欄位名。`anila-core/src/anila_core/api/router_server.py` 的 dispatch
路徑是：

```text
Router
→ CSP /v1/chat/completions model=<agent_id>
→ CSP resolves agent
→ Agent endpoint
```

Router 不直接打 Agent endpoint，符合第 10 節目標。

### Trace hooks 現況

`anila-core` 已有 trace building blocks：

- `anila-core/src/anila_core/tracing/span.py`
  - `SpanKind`: `run` / `agent` / `llm` / `tool` / `handoff` / `interrupt` / `internal`。
  - `Span.to_dict()` 可序列化 span。
- `anila-core/src/anila_core/tracing/hooks.py`
  - `TracingHooks(RunHooks)` 可把 run / agent / tool / handoff / interrupt 轉成 spans。

`anila-agent` 目前有：

- `anila-agent/anila_agent/observability/hooks.py`
  - `AuditHooks(RunHooks)` 記錄 tool start/end 與 agent usage 到 logger。
- `anila-agent/anila_agent/serving/service_wrapper.py`
  - OpenAI-compatible `/v1/chat/completions`。
  - inbound 驗 `X-CSP-Service-Token`，驗過才信任 `X-ANILA-User-*`。
  - streaming 只轉發 OpenAI `chat.completion.chunk` 的可見答案；註解明確表示
    tool/reasoning 軌跡不外送。

結論：`anila-core` 有可用的 trace hook 基礎，`anila-agent` 有 RunHooks 稽核；
但 repo 目前沒有 `anila_trace_sdk`、沒有預設 Full Trace middleware、
沒有 `/v1/traces` 或 `/v1/traces/{trace_id}/spans` ingestion router，
也沒有把 Agent spans durable 寫回 CSP 的 shipper。Full Trace protocol 仍是待實作目標。

現況可觀測性小結（「Full Trace 仍是待實作目標」的具體基線）：

- token usage：`enqueue_usage` 寫 `token_usage` 列，`trace_id` 為可選欄位、來源是**入向**
  `X-ANILA-Trace-Id`。串流路徑攔截最後的 usage chunk，缺 usage 時伺服端估算；
  **非串流 agent 轉發完全不寫 usage 列**（已知缺口，`proxy.py` 註解明載，對應第 12 節 Refactor 項）。
- `audit_logs`：涵蓋 registry 生命週期（register／approve／reject／credential
  issue／rotate／revoke 等 `log_audit_event` 呼叫點）。
- Router trace：只存在於 SSE 瞬態通道（見第 6 節現況對照），spans 從未持久化 ——
  tracing 套件只有 `InMemoryProcessor`，`TracingHooks` 未接進 dispatch 路徑。
- anila-agent：`runtime/model.py` 呼叫 `set_tracing_disabled(True)`（air-gap 防外連
  exporter），service_wrapper 串流只轉發可見答案，tool／reasoning 軌跡不外送。

### OpenWebUI Pipe-compatible 現況

repo 內搜尋 `OpenWebUI`、`openwebui`、`Pipe`、`wrap_pipe`、`traced_pipe`、
`import-openwebui` 的結果顯示：

- `anila_openwebui_bridge` 尚未存在。
- `wrap_pipe` / `traced_pipe` 只出現在 redesign doc 草案。
- `anila agent import-openwebui` / `trace-test` CLI 尚未存在。
- `POST /api/agents/{id}/trace-test` 尚未存在；現有的是
  `POST /api/agents/{id}/test-connection`。

最小現有可行 bridge 是 L1：

1. 讓既有 Agent 暴露 OpenAI-compatible `POST /v1/chat/completions`。
2. 透過 CSP `/api/agents/register` 註冊 endpoint。
3. 核發 `csk-`，讓 Agent 驗 `X-CSP-Service-Token`。
4. 先以 `/api/agents/{id}/test-connection` 驗證連線與 token。

若既有 OpenWebUI Pipe 只有 OpenWebUI 內部 `pipe(...)` 函式，沒有 HTTP endpoint，
目前 repo 沒有可直接套用的 wrapper；需要新增 sidecar 或 bridge package 後才能遷移。
