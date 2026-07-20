# 06 — ANILA Core 對外 Agent Harness / Integration Contract 架構設計

> 日期：2026-07-06  
> 範圍：`packages/anila-core`  
> 定位：本文件修正前一版「Agent harness」容易造成的誤解：**ANILA 不應強制外部開發者使用 `packages/anila-agent`。**  
> 結論：`anila-agent` 應是官方樣板與 reference implementation；真正重要的是 `anila-core` 定義並執行一套**跨框架、跨語言、跨 Agent runtime 的互通契約與代理端 client harness**。

---

## 1. 核心判斷

你的判斷是正確的：

```text
packages/anila-agent = 給開發者下載 / 參考 / 快速起步的官方樣板
packages/anila-core  = ANILA 本體需要依賴的核心 runtime / contract / router foundation
```

因此，ANILA 不能假設：

```text
所有 Agent 都使用 anila-agent
所有 Agent 都是 Python
所有 Agent 都是 OpenAI Agents SDK
所有 Agent 都照 ANILA 官方樣板寫
```

外部團隊可能使用：

```text
LangChain
LlamaIndex
AutoGen
CrewAI
自製 FastAPI service
Node.js service
Go service
既有內網服務包一層 agent endpoint
純 OpenAI-compatible HTTP server
```

所以 `anila-core` 的正確責任不是「規定 Agent 怎麼寫」，而是定義並執行：

```text
ANILA 如何安全、穩定、可觀測地呼叫任何合規 Agent。
```

---

## 2. 正確分層

應採用這個心智模型：

```text
外部 Agent 實作
  - 可以是 LangChain / LlamaIndex / 自製服務 / anila-agent 樣板
  - 不屬於 ANILA 本體強制依賴
  - 只需符合 ANILA 對外 Agent Contract

ANILA Core Agent Integration Harness
  - 屬於 packages/anila-core
  - Router / CSP proxy 呼叫 Agent 時使用
  - 負責 contract validation、policy metadata、stream normalization、error mapping、trace propagation

ANILA Router / CSP Governance
  - 決策、審批、權限、分類、audit、usage
  - 不相信 Agent 自己完成所有治理
```

圖示：

```text
User / SDK / UI
  ↓
CSP Data Plane
  ↓
Router in anila-core
  ↓
RouteDecision + PolicyGate
  ↓
ANILA Core Agent Client Harness
  ↓
CSP Agent Proxy / scoped invocation
  ↓
External Agent Endpoint
      ├── LangChain Agent
      ├── anila-agent sample
      ├── custom FastAPI
      ├── Node service
      └── legacy internal service
```

---

## 3. `anila-core` 的 Agent Harness 應該是什麼

這裡的 harness 不是測試 harness，也不是 Agent 實作框架。

它應該是：

```text
Router-side / Platform-side Agent Integration Harness
```

也就是 ANILA 本體在呼叫外部 Agent 時使用的一組標準化能力。

### 它負責

```text
1. 讀取並驗證 Agent manifest
2. 將 Router 的 ExecutionPlan 轉成 Agent invocation request
3. 注入 trace_id / task_id / session_id / invocation_id
4. 套用 timeout / retry / cancellation / idempotency
5. 確保 dispatch 走 CSP proxy 或受控 endpoint
6. 不把完整使用者權限或 raw secret 交給 Agent
7. 將 Agent 回應轉成標準 AgentEvent
8. 將 Agent 錯誤轉成標準 AgentError
9. 將 stream 交給 Router StreamBridge
10. 將結果寫入 trace / audit / metrics
```

### 它不負責

```text
1. 不強迫 Agent 使用 anila-agent package
2. 不規定 Agent 內部一定用哪個 framework
3. 不要求 Agent import anila-core
4. 不讓 Agent 直接碰 CSP DB
5. 不把 policy enforcement 完全交給 Agent 自己
6. 不讓 Agent 自由呼叫其他 Agent
```

---

## 4. 最重要的架構原則：wire contract 優先，library optional

ANILA 對外應該要求的是 **wire contract**，不是要求某個 Python library。

```text
必須遵守：HTTP / JSON / SSE / manifest / error / trace contract
可以選用：anila-agent 官方樣板
```

因此，文件與程式碼應清楚寫成：

```text
你可以使用任何 Agent framework；
只要你的服務符合 ANILA Agent Contract，
CSP 就能註冊，Router 就能安全 dispatch。
```

`anila-agent` 的定位應是：

```text
官方範例
快速起步模板
最佳實務 reference implementation
conformance 測試 fixture
但不是唯一 runtime
```

---

## 5. 建議的 Agent 相容等級

為了避免一開始 contract 太重，建議定義三個 profile。

---

### 5.1 Bronze：最低相容 Agent

適合既有服務快速接入。

必須提供：

```text
GET /healthz
GET /manifest
POST /invoke 或 POST /v1/chat/completions
JSON response 或 OpenAI-compatible SSE
標準錯誤格式，或可被 anila-core adapter 轉換
```

能力：

```text
可被 CSP 註冊
可被 Router dispatch
可被 timeout / error mapping
可被基本 trace headers 串起來
```

限制：

```text
不保證完整 artifact event
不保證 resume
不保證 fine-grained tool / memory governance
```

---

### 5.2 Silver：ANILA native Agent

適合新開發的正式 Agent。

必須支援：

```text
AgentInvocationEnvelope
anila-agent-events-v1 SSE
trace propagation
classification metadata
scoped invocation token
idempotency_key
標準 AgentError
artifact.created event
usage event
```

能力：

```text
Router StreamBridge 可完整解析
CSP audit 可看到 invocation / artifact / error
中途錯誤可 normalized fallback
支援取消與 timeout
```

---

### 5.3 Gold：治理完整 Agent

適合高分類、高風險、可長時間執行的 Agent。

額外支援：

```text
resume cursor
durable invocation state
cancellation endpoint
tool gateway conformance
memory gateway conformance
approval_required event
fine-grained usage metrics
capability-specific manifest
```

能力：

```text
可做多輪任務
可與 session/resume 整合
可進入 multi-agent orchestration
可進入正式高治理環境
```

---

## 6. Agent Manifest Contract

`anila-core` 應該定義 Agent manifest 的內部標準格式，並允許 CSP 或 adapter 把舊格式 normalize 進來。

### 建議 manifest

```json
{
  "schema_version": "anila-agent-manifest-v1",
  "agent_id": "image-generator",
  "display_name": "Image Generator",
  "version": "1.2.0",
  "description": "Generates governed images for briefing and artifact workflows.",
  "compatibility_profile": "silver",
  "status": "approved",
  "capabilities": [
    "image_generation",
    "openai_images_api",
    "artifact_generation"
  ],
  "input_modes": ["chat", "text"],
  "output_modes": ["sse", "json", "artifact", "image"],
  "streaming": true,
  "supports_resume": false,
  "supports_cancellation": true,
  "classification_ceiling": "confidential",
  "required_scopes": ["agent.invoke.image"],
  "timeout_ms": 120000,
  "latency_slo_ms": 60000,
  "endpoints": {
    "invoke": "/invoke",
    "openai_chat_completions": "/v1/chat/completions",
    "health": "/healthz",
    "ready": "/readyz"
  },
  "event_protocols": [
    "anila-agent-events-v1",
    "openai-chat-sse"
  ],
  "sample_user_intents": [
    "生成圖片",
    "製作簡報封面圖",
    "建立視覺概念圖"
  ],
  "negative_intents": [
    "分析既有圖片但不生成",
    "純文字摘要"
  ]
}
```

### 重點

`manifest` 是 Router 做 deterministic candidate filtering 的來源，不應只靠自然語言 `description`。

Router 應用欄位：

```text
capabilities
input_modes
output_modes
classification_ceiling
required_scopes
streaming
supports_resume
status / health
timeout_ms
sample_user_intents / negative_intents
```

---

## 7. Agent Invocation Contract

`anila-core` 應提供 `AgentInvocationEnvelopeBuilder`，把 Router 的 `ExecutionPlan` 轉成 Agent 可接收的標準 request。

### 標準 envelope

```json
{
  "schema_version": "anila-agent-invocation-v1",
  "invocation_id": "inv_...",
  "trace_id": "trace_...",
  "task_id": "task_...",
  "conversation_id": "conv_...",
  "session_id": "sess_...",
  "idempotency_key": "idem_...",
  "agent": {
    "id": "image-generator",
    "version": "1.2.0"
  },
  "user": {
    "id": "user_...",
    "roles": ["analyst"],
    "scopes": ["agent.invoke.image"]
  },
  "classification": {
    "level": "internal",
    "ceiling": "confidential",
    "source": "task"
  },
  "input": {
    "type": "chat",
    "messages": [
      {
        "role": "user",
        "content": "幫我生成一張任務簡報封面圖"
      }
    ],
    "rewritten_query": "生成一張任務簡報封面圖"
  },
  "attachments": [],
  "budget": {
    "timeout_ms": 120000,
    "max_tool_calls": 8,
    "max_tokens": 4096,
    "max_artifacts": 3
  },
  "stream": {
    "enabled": true,
    "protocol": "anila-agent-events-v1"
  },
  "policy": {
    "requires_audit": true,
    "allow_external_network": false,
    "requires_citations": false
  }
}
```

---

## 8. 為什麼 envelope 仍然重要，即使 Agent 不用 anila-agent

LangChain / LlamaIndex / custom service 也需要知道：

```text
這次 invocation 是誰發起的
trace_id 是什麼
資料分類是多少
可不可以 streaming
timeout 是多少
是否允許產 artifact
是否允許外部網路
要把錯誤用什麼格式回來
```

這些是平台互通資訊，不是 Python library 細節。

外部 Agent 可以自己寫 adapter：

```text
ANILA InvocationEnvelope
  ↓
LangChain Runnable input
  ↓
LangChain callbacks
  ↓
ANILA AgentEvent SSE
```

因此 envelope 是 **跨框架接頭**，不是 `anila-agent` 綁定。

---

## 9. Agent Event Contract

Router 不應要求所有 Agent 內部用同一套 framework，但應要求輸出事件可被理解。

### 建議 event protocol：`anila-agent-events-v1`

```text
event: anila.agent.started
data: {}

event: anila.agent.status
data: {"message":"開始處理"}

event: anila.agent.delta
data: {"text":"正在產生圖片..."}

event: anila.agent.artifact.created
data: {"artifact_id":"art_...","type":"image","uri":"/api/artifacts/art_..."}

event: anila.agent.error
data: {"code":"AGENT_TOOL_FAILED","safe_message":"影像服務暫時無法使用","retryable":true}

event: anila.agent.completed
data: {"usage":{"tool_calls":1}}
```

### 同時支援 OpenAI-compatible SSE

為了降低接入成本，Bronze profile 可支援：

```text
data: {"choices":[{"delta":{"content":"..."}}]}
data: [DONE]
```

`anila-core` 的 Agent Client Harness 負責把它轉成標準內部事件。

---

## 10. Agent Error Contract

外部 Agent 不應把 raw exception、stack trace、內部 endpoint、secret 回給 Router 或使用者。

標準錯誤：

```json
{
  "error": {
    "code": "AGENT_TOOL_FAILED",
    "safe_message": "工具服務暫時無法使用。",
    "retryable": true,
    "details": {
      "public_reason": "upstream_unavailable"
    }
  }
}
```

建議錯誤碼：

```text
AGENT_INVALID_INPUT
AGENT_POLICY_DENIED
AGENT_AUTH_FAILED
AGENT_TOOL_DENIED
AGENT_TOOL_FAILED
AGENT_MEMORY_DENIED
AGENT_MEMORY_FAILED
AGENT_MODEL_TIMEOUT
AGENT_MODEL_FAILED
AGENT_ARTIFACT_FAILED
AGENT_BUDGET_EXCEEDED
AGENT_CANCELLED
AGENT_INTERNAL_ERROR
AGENT_UNSUPPORTED_PROTOCOL
```

`anila-core` 需提供 mapper，把不同 agent 格式轉成上述 taxonomy。

---

## 11. Trace 與 Header Contract

Router 呼叫 Agent 時，至少帶：

```text
X-ANILA-Invocation-Id
X-ANILA-Trace-Id
X-ANILA-Task-Id
X-ANILA-Conversation-Id
X-ANILA-Session-Id
X-ANILA-Classification
X-ANILA-Agent-Id
X-ANILA-Idempotency-Key
Authorization: Bearer <scoped-invocation-token>
```

注意：不要把使用者原始 access token 或 service root token 交給 Agent。

建議使用：

```text
scoped invocation token
短 TTL
綁定 agent_id
綁定 invocation_id
綁定 allowed scopes
綁定 classification ceiling
```

---

## 12. `anila-core` Agent Client Harness 模組建議

建議新增或整理成：

```text
packages/anila-core/src/anila_core/agents/
├── contracts/
│   ├── manifest.py
│   ├── invocation.py
│   ├── events.py
│   ├── errors.py
│   └── profiles.py
├── client/
│   ├── agent_client.py
│   ├── envelope_builder.py
│   ├── dispatcher.py
│   ├── retry.py
│   └── cancellation.py
├── streams/
│   ├── anila_events.py
│   ├── openai_sse_adapter.py
│   ├── json_adapter.py
│   └── normalizer.py
├── policy/
│   ├── manifest_filter.py
│   ├── invocation_policy.py
│   └── scoped_token.py
├── conformance/
│   ├── manifest_validator.py
│   ├── protocol_probe.py
│   └── report.py
└── telemetry/
    ├── trace.py
    └── metrics.py
```

---

## 13. Router-side Agent Client Harness 流程

```text
RouteDecision: single_agent
  ↓
PolicyGate allow
  ↓
AgentManifest normalized
  ↓
AgentInvocationEnvelopeBuilder
  ↓
Scoped invocation token issue/request
  ↓
AgentClient.dispatch()
  ↓
Agent response adapter:
      - anila-agent-events-v1
      - openai-chat-sse
      - json
      - plain text fallback
  ↓
StreamBridge
  ↓
SessionStore / Trace / Audit / Metrics
```

---

## 14. Cross-framework Adapter 思路

### 14.1 LangChain Agent

外部開發者可自己實作 adapter：

```text
/invoke receives ANILA envelope
  ↓
extract input.messages / rewritten_query
  ↓
call LangChain Runnable / AgentExecutor
  ↓
callbacks convert tokens/tool events to anila.agent.* SSE
  ↓
return completed / error event
```

ANILA 不需要知道 LangChain 內部細節。

---

### 14.2 LlamaIndex Agent

```text
ANILA envelope
  ↓
query_engine / agent.chat
  ↓
citation nodes mapped to anila.agent.citation.added
  ↓
artifact metadata optional
```

---

### 14.3 Custom FastAPI Agent

```text
POST /invoke
  ↓
validate JSON
  ↓
run domain logic
  ↓
yield SSE events
```

---

### 14.4 Existing legacy service

可包一層 thin adapter：

```text
ANILA /invoke
  ↓
legacy service request
  ↓
legacy response
  ↓
map to anila.agent.completed
```

---

## 15. Conformance Tool 很重要，但不等於測試 harness

雖然這不是「測試 harness」主題，但對外 agent integration 需要 conformance probe。

`anila-core` 可提供 CLI：

```bash
python -m anila_core.agents.conformance probe https://agent.local
python -m anila_core.agents.conformance validate-manifest manifest.json
python -m anila_core.agents.conformance invoke --profile silver manifest.json
```

檢查：

```text
/healthz 是否可用
/manifest 是否符合 schema
invoke endpoint 是否接受 envelope
SSE 是否可被解析
錯誤格式是否可 map
trace headers 是否有回傳或保留
classification 超限時是否 fail-closed
```

這不強制使用 `anila-agent`，但能讓任何 Agent 驗證自己是否能接入 ANILA。

---

## 16. CSP / Router / Agent 的責任邊界

| 責任 | CSP | anila-core Router | external Agent |
|---|---:|---:|---:|
| Agent 註冊 / 審批 | 是 | 否 | 提供 manifest |
| Agent candidate filtering | 提供資料 | 是 | 否 |
| Route decision | 否 | 是 | 否 |
| Dispatch policy gate | 提供 policy | 是 | 可本地 fail-closed |
| Invocation envelope | 可協助 token | 是 | 接收 |
| Tool / memory 權限 | 是 | 傳遞限制 | 遵守限制 |
| Stream normalize | 否 | 是 | 輸出合規 stream |
| Full trace | 收斂 | 產生 route/dispatch span | 產生 invocation/tool span |
| Artifact governance | 是 | 轉接事件 | 建立或回報 artifact |
| Final user experience | 否 | 是 | 提供內容 |

---

## 17. 風險與對策

| 風險 | 對策 |
|---|---|
| 過度綁定 `anila-agent` | 只要求 wire contract，樣板 optional |
| Agent manifest 手動漂移 | conformance probe + manifest schema version |
| Agent 回傳格式混亂 | Stream adapters + profile 分級 |
| Agent 取得過大權限 | scoped invocation token，不傳 raw user token |
| Agent 自由使用工具 | 工具走 platform API / gateway / scoped token |
| Agent 自由呼叫其他 Agent | 不允許直接互叫，只能回報 delegation request |
| 高分類資料外流 | classification ceiling + Router policy + token scope |
| SSE bug 散落各 Agent | ANILA event protocol + OpenAI SSE adapter |
| 第三方框架不支援 ANILA event | Bronze profile 先接 OpenAI-compatible JSON/SSE |

---

## 18. P0 / P1 / P2 落地路線

### P0：先把跨框架 contract 固定

```text
1. 定義 AgentManifest schema
2. 定義 AgentInvocationEnvelope schema
3. 定義 AgentEvent / AgentError schema
4. 實作 anila-core AgentClient
5. 實作 OpenAI SSE adapter + ANILA event adapter
6. 實作 manifest normalizer
7. Router dispatch 改走 AgentClient，不散落 HTTP 呼叫
8. 文件明確寫：anila-agent optional，任何 framework 可接
```

### P1：提高治理與可觀測性

```text
1. scoped invocation token
2. trace header propagation
3. artifact event normalization
4. cancellation / timeout
5. conformance CLI
6. Bronze / Silver / Gold profile
7. manifest health / readiness refresh
```

### P2：支援高階 Agent 生態

```text
1. resume / durable invocation state
2. platform ToolGateway HTTP contract
3. platform MemoryGateway HTTP contract
4. approval_required event
5. multi-agent delegation request
6. sidecar adapter for high-risk / non-Python agents
```

---

## 19. 對前一份改善報告的修正

前一份報告裡提到 Agent-side harness 時，容易讓人誤解為：

```text
ANILA 要求所有 Agent 都用 packages/anila-agent
```

正確修正如下：

```text
anila-core 應定義 wire contract 與 Router-side Agent Client Harness。
anila-agent 只是官方樣板，實作這套 contract 的 reference implementation。
外部 Agent 可以使用任意 framework，只要符合 manifest / invoke / event / error / trace contract。
```

---

## 20. 最終結論

`anila-core` 最重要的不是提供 Agent 實作框架，而是提供：

```text
Agent Integration Contract
Agent Client Harness
Agent Stream / Error Normalizer
Agent Manifest / Capability Model
Agent Invocation Envelope
Agent Conformance Profiles
```

這會讓 ANILA 能同時支援：

```text
官方 anila-agent 樣板
LangChain Agent
LlamaIndex Agent
自製 FastAPI Agent
Node / Go Agent
既有內網服務 adapter
```

而不犧牲：

```text
Router 可治理性
CSP 審批與 audit
classification 安全
trace 可觀測性
streaming 一致性
failure fallback
```

一句話總結：

> `anila-agent` 是一條推薦道路；`anila-core` 應定義的是所有道路都必須接上的 ANILA Agent 互通閘道。
