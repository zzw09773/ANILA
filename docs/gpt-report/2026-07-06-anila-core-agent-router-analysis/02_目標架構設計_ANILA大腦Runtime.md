# ANILA `packages/anila-core` Agent 代理主路由深度分析報告 — 目標架構設計

> 日期：2026-07-06  
> 報告目的：提出 `packages/anila-core` 作為 ANILA 大腦 runtime 的目標架構、核心抽象與契約。

---

## 1. 目標

目標不是把 Router 變成大型任務編排框架，而是建立一個可治理、可觀測、可測試的 Agent 代理主路由。

應達成：

```text
Router 能清楚判斷是否需要 agent
Router 能以結構化方式記錄為何派給某 agent
Router dispatch 前一定經過 policy gate
Router 能穩定轉換 agent stream
Router 能在中斷後依 cursor resume
Router 的 routing 品質可以被 eval dataset 測量
Router 可逐步支援受控 multi-agent plan
```

---

## 2. 目標架構總覽

```text
OpenAI-compatible Request
  ↓
Request Normalizer
  ↓
Context Builder
  ↓
Agent Registry Snapshot
  ↓
Capability Filter
  ↓
Route Decision Engine
  ↓
Policy Gate
  ↓
Execution Planner
  ↓
Agent Dispatcher / Model Provider
  ↓
StreamBridge
  ↓
Session Event Store
  ↓
Trace / Audit / Metrics
```

---

## 3. 核心模組設計

## 3.1 Request Normalizer

責任：

```text
統一 OpenAI-compatible chat/completions request
整理 messages / model / stream / metadata
抽出 task_id / conversation_id / session_id
建立 request_id / trace_id
限制最大 input size
清理不支援欄位
```

輸出：

```python
class RouterRequest:
    request_id: str
    trace_id: str
    user_id: str
    model: str
    messages: list[ChatMessage]
    stream: bool
    metadata: dict
    task_id: str | None
    session_id: str | None
```

---

## 3.2 Context Builder

責任：

```text
取得目前使用者權限
取得 task / classification context
取得對話必要摘要
取得 agent registry snapshot
取得 provider capability
決定 Router prompt 可看到什麼資訊
```

重點：Context Builder 不應無限制灌入完整 memory / RAG chunk。Router 決策只需要最小上下文：

```text
目前使用者意圖
可用 agent capability summary
目前 task / classification
必要 conversation state
policy flags
```

---

## 3.3 Agent Registry Snapshot

Agent manifest 應該成為能力契約，而非單純清單。

建議 schema：

```json
{
  "id": "image-generator",
  "version": "1.2.0",
  "display_name": "Image Generator",
  "status": "approved",
  "health": "ready",
  "capabilities": [
    "image_generation",
    "openai_images_api",
    "visual_briefing"
  ],
  "input_modes": ["text"],
  "output_modes": ["sse", "artifact", "image"],
  "streaming": true,
  "classification_ceiling": "confidential",
  "required_scopes": ["agent.invoke.image"],
  "latency_slo_ms": 60000,
  "timeout_ms": 120000,
  "cost_profile": "high",
  "sample_user_intents": [
    "generate an image",
    "create a visual concept"
  ],
  "negative_intents": [
    "analyze an uploaded image without generation"
  ]
}
```

---

## 3.4 Capability Filter

在讓 LLM 做最終 route decision 前，先做 deterministic filter：

```text
只保留 approved / ready agent
只保留使用者有權限的 agent
只保留 classification ceiling 足夠的 agent
只保留 capability 與輸入模式可能符合的 agent
排除健康狀態不佳或 circuit open 的 agent
```

這會降低：

```text
LLM prompt 長度
routing 誤派率
policy gate 壓力
高風險 agent 被誤選機率
```

---

## 3.5 Route Decision Engine

Router 的核心決策輸出應是 typed schema，而不是自由文字。

### RouteDecision schema

```python
class RouteDecision:
    route_type: Literal[
        "direct_answer",
        "single_agent",
        "multi_agent_plan",
        "clarify",
        "deny"
    ]
    confidence: float
    reason_codes: list[str]
    user_visible_reason: str | None
    direct_answer: str | None
    agent_id: str | None
    rewritten_query: str | None
    plan: ExecutionPlan | None
    fallback: FallbackDecision | None
    policy_requirements: PolicyRequirements
```

### route_type 說明

| route_type | 意義 |
|---|---|
| `direct_answer` | Router 直接用 primary LLM 回答 |
| `single_agent` | 派給一個 agent |
| `multi_agent_plan` | 受控多 agent 步驟 |
| `clarify` | 資訊不足，需要追問 |
| `deny` | policy 或安全理由拒絕 |

---

## 3.6 Policy Gate

Policy Gate 是 dispatch 前必經路徑。

### 輸入

```text
RouterRequest
RouteDecision
AgentManifest
UserContext
TaskContext
ClassificationContext
```

### 輸出

```python
class PolicyGateResult:
    allowed: bool
    action: Literal["allow", "deny", "clarify", "approval_required"]
    reason_code: str
    audit_required: bool
    safe_message: str | None
```

### 檢查項目

| 類別 | 檢查 |
|---|---|
| Agent 狀態 | approved、ready、trace-test passed |
| 使用者權限 | role、scope、project membership |
| 分類上限 | request/task/source classification <= agent ceiling |
| Endpoint | 必須經 CSP proxy，不直連未治理 endpoint |
| SSRF | endpoint host 必須 trusted |
| Token | 不可使用 dev fallback secret |
| Audit | 高風險 dispatch 必須 trace/audit |
| Approval | 高分類或高影響操作可要求人工核准 |

---

## 3.7 Execution Planner

ExecutionPlanner 把 RouteDecision 轉成可執行計畫。

### Single agent execution

```json
{
  "type": "single_agent",
  "agent_id": "image-generator",
  "input": {
    "query": "Generate a briefing image..."
  },
  "timeout_ms": 120000,
  "stream": true
}
```

### Multi-agent execution

```json
{
  "type": "multi_agent_plan",
  "steps": [
    {
      "step_id": "search",
      "agent_id": "rag-search",
      "input": {"query": "..."}
    },
    {
      "step_id": "outline",
      "agent_id": "report-writer",
      "depends_on": ["search"]
    },
    {
      "step_id": "cover_image",
      "agent_id": "image-generator",
      "depends_on": ["outline"]
    }
  ],
  "merge_strategy": "studio_artifact"
}
```

注意：multi-agent 初期只應支援白名單 pattern，不應讓 LLM 任意自由規劃。

---

## 3.8 Agent Dispatcher

Agent Dispatcher 負責實際呼叫 agent。

要求：

```text
只透過 CSP proxy 或受控內網路徑
加入 trace headers
加入 task_id / conversation_id / session_id
統一 timeout / retry / circuit breaker
處理 idempotency key
把 agent response 交給 StreamBridge
```

錯誤分類：

```text
AGENT_UNAVAILABLE
AGENT_TIMEOUT
AGENT_POLICY_DENIED
AGENT_INVALID_RESPONSE
AGENT_STREAM_BROKEN
AGENT_RATE_LIMITED
AGENT_AUTH_FAILED
```

---

## 3.9 StreamBridge

StreamBridge 是 Router 中最關鍵的可靠性元件之一。

### 輸入

```text
OpenAI SSE
Agent named SSE
JSON events
internal event
error event
heartbeat
```

### 輸出

```text
OpenAI-compatible SSE
ANILA internal event
session event log
trace span
```

### 標準事件

```text
anila.router.decision
anila.agent.start
anila.agent.delta
anila.agent.tool_call
anila.agent.artifact
anila.agent.error
anila.agent.done
anila.router.fallback
anila.session.checkpoint
```

### 要處理的邊界案例

```text
data: 多行事件
[DONE]
空 heartbeat
agent 中途 error
agent 回非 SSE
client disconnect
slow client backpressure
resume cursor
重複事件去重
```

---

## 3.10 Session Event Store

Session/resume 建議以 event log 模式設計：

```python
class SessionEvent:
    session_id: str
    cursor: int
    event_type: str
    payload: dict
    owner_user_id: str
    trace_id: str
    created_at: datetime
```

### Resume 行為

```text
client 帶 session_id + cursor
Router 檢查 owner
Router 從 cursor 後回放 event
若 agent 仍在跑，接回 live stream
若 agent 已結束，補送剩餘 events + done
若 cursor 太舊，回明確錯誤
```

### 儲存選擇

| 選項 | 優點 | 風險 |
|---|---|---|
| local file | 實作簡單 | 多副本不可用 |
| Redis stream | 適合 stream/resume | 需 TTL 與容量控管 |
| CSP DB event table | audit 最完整 | 成本較高、需 schema |
| hybrid | 彈性高 | 複雜度較高 |

建議：P0 可先抽象 interface；P1 導入 Redis-backed store；P2 視 audit 需求寫入 CSP。

---

## 3.11 Trace / Audit / Metrics

每次 route decision 都應記錄：

```json
{
  "trace_id": "...",
  "route_type": "single_agent",
  "selected_agent_id": "image-generator",
  "candidate_agents": ["image-generator", "studio-agent"],
  "confidence": 0.87,
  "reason_codes": ["USER_REQUESTED_IMAGE"],
  "policy_gate": "allow",
  "latency_ms": 312,
  "fallback_used": false
}
```

建議 metrics：

```text
router_route_total{route_type, agent_id}
router_route_latency_ms
router_policy_denied_total{reason_code}
router_invalid_decision_total
router_agent_dispatch_total{agent_id, status}
router_stream_error_total{reason_code}
router_resume_total{status}
router_fallback_total{reason_code}
```

---

## 4. Provider 層目標

Provider 不應只是 HTTP client，而應提供 capability 與錯誤分類。

```python
class ProviderCapabilities:
    supports_streaming: bool
    supports_tool_calling: bool
    supports_json_schema: bool
    supports_images: bool
    supports_embeddings: bool
    max_context_tokens: int | None
```

Provider 錯誤：

```text
PROVIDER_TIMEOUT
PROVIDER_AUTH_FAILED
PROVIDER_RATE_LIMITED
PROVIDER_SERVER_ERROR
PROVIDER_INVALID_RESPONSE
PROVIDER_UNSUPPORTED_FEATURE
```

Router 依錯誤類型決定：

```text
retry
fallback
direct error
ask user retry
switch provider
disable candidate temporarily
```

---

## 5. 目標資料流

### 5.1 Direct answer

```text
Request
  ↓
Context
  ↓
RouteDecision: direct_answer
  ↓
PolicyGate: allow
  ↓
Primary LLM
  ↓
StreamBridge
  ↓
OpenAI response
```

### 5.2 Single agent

```text
Request
  ↓
Context + Registry Snapshot
  ↓
Capability Filter
  ↓
RouteDecision: single_agent
  ↓
PolicyGate
  ↓
ExecutionPlan
  ↓
AgentDispatcher
  ↓
StreamBridge
  ↓
OpenAI SSE + anila.* events
```

### 5.3 Clarification

```text
Request
  ↓
RouteDecision: clarify
  ↓
回覆追問
  ↓
不 dispatch
  ↓
trace reason = INSUFFICIENT_INFORMATION
```

### 5.4 Policy denied

```text
Request
  ↓
RouteDecision
  ↓
PolicyGate: deny
  ↓
safe denial message
  ↓
audit
```

### 5.5 Agent unavailable fallback

```text
Request
  ↓
RouteDecision: single_agent
  ↓
PolicyGate: allow
  ↓
AgentDispatcher: timeout/unavailable
  ↓
FallbackDecision
  ↓
direct answer or safe failure message
  ↓
trace fallback
```

---

## 6. 設計原則

```text
不要讓自由文字成為權威控制訊號
不要讓 agent 彼此自由互叫
不要把所有治理責任推給 CSP 或 Router 任一方
不要用更長 prompt 取代 typed schema
不要只做 health 200，要做 contract test
不要讓 resume 變成重新執行
```

---

## 7. 最終目標

Router 不只是判斷「要不要派 agent」，而是要能回答：

```text
為什麼派這個 agent？
使用者是否有權限？
資料分類是否允許？
agent 是否健康？
輸入輸出契約是否符合？
如果失敗怎麼 fallback？
stream 中斷後怎麼恢復？
這次決策可不可以被 audit？
這類 prompt 的 routing accuracy 是多少？
```

能回答這些問題，才是 ANILA 大腦 runtime 的完成型。
