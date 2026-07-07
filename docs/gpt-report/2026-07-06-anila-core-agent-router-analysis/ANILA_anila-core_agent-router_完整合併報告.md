# ANILA `packages/anila-core` Agent 代理主路由深度分析報告 — 管理摘要

> 日期：2026-07-06  
> 分析範圍：`packages/anila-core` 作為 ANILA Agent 代理主路由／大腦 runtime 的架構、風險、改善方向與落地優先序。  
> 核心對象：Router、Agent dispatch、OpenAI-compatible provider、SSE streaming、session/resume、policy gate、trace/audit、routing eval。

---

## 1. 一句話結論

`packages/anila-core` 目前已具備作為 ANILA 核心 runtime 與 Agent 主路由的基礎，但若要成為長期可治理、可審計、可擴展的「ANILA 大腦」，最需要改善的不是單點功能，而是把目前偏向「LLM 判斷 + 字串式 dispatch + streaming relay」的設計，升級成：

```text
結構化路由決策
+ 能力契約式 Agent Registry
+ dispatch 前政策閘門
+ 可觀測 trace / audit
+ 可恢復 session event store
+ 可測量 routing eval
+ 受控 multi-agent orchestration
```

---

## 2. 當前設計的優點

目前架構有幾個重要優點，這些應該保留：

| 優點 | 價值 |
|---|---|
| OpenAI-compatible 入口 | 可以接 SDK、前端與外部相容呼叫模式，降低整合成本 |
| Router app factory 放在 `anila-core` | 讓部署 wrapper 與核心邏輯分離，利於服務化與測試 |
| Agent manifest 由 CSP 管理 | Agent 註冊、治理、審批可以收斂到 CSP |
| 支援 streaming / SSE passthrough | 對 chat UI、agent tool output、長任務體驗很重要 |
| 有 session / resume / owner 概念 | 已經意識到 Router 不只是一次性 stateless proxy |
| 與 `anila-agent` 模板協作 | 有利於官方 agent starter 與主路由契約一致 |

---

## 3. 當前最大風險

以「ANILA 大腦」標準來看，目前最大風險集中在六類。

| 風險 | 說明 | 影響 |
|---|---|---|
| 路由決策未充分結構化 | 若仍依賴 `DISPATCH:<agent_id>:<query>` 類字串契約，解析與安全邊界脆弱 | 誤派、prompt injection、audit 不清 |
| Agent 能力描述不足 | 若 manifest 主要靠自然語言 description，Router 只能猜 | routing 品質不穩、難 eval |
| policy gate 不夠明確 | Router dispatch 前若缺少明確權限、分類、agent 狀態檢查 | 治理責任模糊、資安風險 |
| SSE / stream bridge 複雜度高 | Agent event、OpenAI SSE、前端 `anila.*` event 混雜 | client 卡住、斷線恢復不穩、錯誤難查 |
| session/resume 可能不夠 durable | 如果 state 依賴本機檔案或 request retry，未來多副本會出問題 | 重複執行、resume 不可靠 |
| 缺少 routing eval | 單元測試可保證不壞，但無法證明「派得好」 | 大腦品質不可量化 |

---

## 4. 最重要的三個改善

### P0-1：將 `DISPATCH` 字串改成 `RouteDecision` 結構化契約

目前最該優先處理的是把路由決策從自由文字／特殊字串，改為 typed schema：

```json
{
  "route_type": "single_agent",
  "agent_id": "image-generator",
  "rewritten_query": "Generate a briefing image...",
  "confidence": 0.87,
  "reason_codes": ["USER_REQUESTED_IMAGE"],
  "policy_requirements": {
    "classification_ceiling": "confidential",
    "requires_audit": true
  }
}
```

這能直接改善：

```text
可測試性
可審計性
prompt injection 抵抗力
multi-agent 擴展性
routing 品質統計
fallback 一致性
```

---

### P0-2：在 dispatch 前加入 Router 自己的 `PolicyGate`

CSP 是治理 SSOT，但 Router 作為大腦，不能只負責轉發。Router 在做出 route decision 後、呼叫 agent 前，應強制通過 policy gate：

```text
RouteDecision
  ↓
PolicyGate
  ↓
Allowed / Denied / Needs Clarification / Needs Approval
```

應檢查：

```text
使用者是否有權使用該 agent
agent 是否 approved / ready / trace-test passed
資料分類是否超過 agent 可處理上限
是否需要 audit / approval
endpoint 是否只能經 CSP proxy
SSRF allow-list 是否符合
service token 是否安全
```

---

### P0-3：將 SSE 轉換抽成 `StreamBridge`

SSE 是 Router 最容易壞、最難 debug 的區域。建議把 streaming 從主路由流程抽成獨立狀態機：

```text
Agent SSE / OpenAI SSE / Internal Event
  ↓
StreamBridge
  ↓
Normalized StreamEvent
  ↓
OpenAI-compatible output + anila.* events
```

建議標準事件：

```text
anila.router.decision
anila.agent.start
anila.agent.delta
anila.agent.tool_call
anila.agent.artifact
anila.agent.error
anila.agent.done
anila.router.fallback
```

---

## 5. 建議目標架構

```text
OpenAI-compatible request
  ↓
Request Normalizer
  ↓
Auth / Task / Trace Context
  ↓
Agent Registry Snapshot
  ↓
Capability Filter
  ↓
Structured Route Decision
  ↓
Policy Gate
  ↓
Execution Plan
  ↓
Agent Dispatcher / Direct Model Provider
  ↓
StreamBridge
  ↓
OpenAI SSE + anila.* Events
  ↓
Trace / Audit / Metrics / Session Store
```

這個架構的本質是把 Router 從「代理轉發器」提升為：

```text
policy-aware
capability-aware
traceable
recoverable
testable
measurable
agent orchestration runtime
```

---

## 6. 建議優先順序

| 優先級 | 項目 | 理由 |
|---|---|---|
| P0 | `RouteDecision` 結構化 | 這是後續 policy、eval、multi-agent 的地基 |
| P0 | Router `PolicyGate` | 防止大腦繞過治理邊界 |
| P0 | `StreamBridge` | 降低 SSE / resume / error handling 複雜度 |
| P0 | route decision trace span | 沒有 trace 就無法審計與改善 |
| P0 | fake CSP + fake agent contract tests | 讓大腦主流程可穩定測 |
| P1 | Agent manifest capability schema | 降低誤派率 |
| P1 | Registry cache + circuit breaker | CSP / agent 異常時避免雪崩 |
| P1 | Provider capability / error taxonomy | 讓 fallback 與 UI 錯誤更準 |
| P1 | Session event store | 讓 resume 真正可靠 |
| P1 | Routing eval dataset | 讓大腦品質可量化 |
| P2 | 受控 multi-agent plan | 支援複合任務 |
| P2 | shadow routing / A-B eval | 安全調整 Router prompt |
| P2 | human approval gate | 高風險任務導入人工核准 |

---

## 7. 最終判斷

`packages/anila-core` 作為 ANILA 大腦，最值得投入的方向不是增加更多 agent，也不是單純寫更長 prompt，而是建立一套可治理的主路由 runtime。

建議以三條主線推進：

```text
1. 決策結構化：RouteDecision / ExecutionPlan
2. 治理內建化：PolicyGate / Trace / Audit
3. 執行可靠化：StreamBridge / SessionStore / Eval
```

這三條做完後，ANILA 的 Router 才能從「可以派 agent」升級成「可以安全、可控、可觀測地協調整個平台能力」。


---

# ANILA `packages/anila-core` Agent 代理主路由深度分析報告 — 現況架構與風險

> 日期：2026-07-06  
> 報告目的：盤點 `packages/anila-core` 作為 Agent 代理主路由的現況、角色、風險熱區與技術債。

---

## 1. 系統定位

`packages/anila-core` 在 ANILA 裡不是單純 SDK，而是多個服務共同依賴的 runtime foundation。從 Agent 代理主路由角度，它扮演的是：

```text
Router app factory
OpenAI-compatible provider 層
Agent dispatch runtime
streaming / SSE 轉接層
session / resume 基礎
memory / tool / security 基礎能力
```

實際部署上：

```text
services/anila-core-router
  ↓
使用 anila-core 內的 router app factory
  ↓
對外提供 OpenAI-compatible Router 服務
```

而 CSP 則作為治理與資料平面：

```text
CSP /v1/chat/completions
  ↓ model=anila-router
Router
  ↓
Primary LLM or Agent dispatch
```

---

## 2. 現況主流程推定

目前 Agent 主路由大致可整理成以下流程：

```text
使用者 / OpenAI SDK / 前端
  ↓
CSP Data Plane `/v1/chat/completions`
  ↓
判斷 model = anila-router
  ↓
轉入 Router
  ↓
Router 取得 agent manifest / registry
  ↓
Router 呼叫 primary LLM
  ↓
LLM 回一般文字，或回 dispatch 指令
  ↓
Router 解析 dispatch
  ↓
透過 CSP proxy 呼叫目標 agent
  ↓
agent 回傳 SSE / JSON / OpenAI-like stream
  ↓
Router passthrough / normalize
  ↓
前端或 SDK 收到結果
```

這是一個有效的初版架構，但當 Agent 數量、任務複雜度、資料分類、session/retry、audit requirement 增加時，會遇到可治理性問題。

---

## 3. 現況優勢

### 3.1 OpenAI-compatible 的入口是正確選擇

Router 使用 OpenAI-compatible 介面，能讓：

```text
前端 chat UI
OpenAI SDK
內部 agent
外部開發者
CSP data plane
```

共享同一組協議，這是降低平台整合成本的關鍵。

---

### 3.2 CSP 作為 Agent Registry SSOT 是正確方向

Agent 不應硬編在 Router 裡。由 CSP 管理 Agent Registry，可以讓：

```text
Agent 狀態審批
endpoint 管理
權限管理
trace-test gate
usage / audit
service token / API key
```

集中治理。

Router 則應該使用 CSP 回傳的 manifest snapshot 做決策。

---

### 3.3 Router 已經開始處理 streaming / resume

這代表設計者已經知道 Router 不只是一次 request/response proxy。Agent 任務可能是：

```text
長時間執行
多段 streaming
中途 tool call
產出 artifact
可中斷恢復
```

這些都是大腦 runtime 必須處理的能力。

---

### 3.4 `anila-core` 與 `anila-agent` 分離合理

`packages/anila-core` 是平台 runtime foundation；`packages/anila-agent` 是官方 starter/template。這個分離有助於：

```text
主路由維持穩定
Agent 開發者有清楚模板
共用 memory / streaming / tracing 契約
平台治理能力不散落各 agent
```

---

## 4. 主要風險熱區

## 4.1 路由決策使用字串協議的風險

如果目前 Router 仍使用類似：

```text
DISPATCH:<agent_id>:<query>
```

這種字串協議，會造成以下問題。

| 問題 | 具體風險 |
|---|---|
| 解析脆弱 | query 內含冒號、換行、特殊 token 時容易誤判 |
| prompt injection | 使用者要求模型輸出 `DISPATCH:` 可能誘導錯誤 dispatch |
| 缺少 typed schema | 無法穩定驗證 `agent_id`、信心分數、reason code |
| 難支援 multi-agent | 字串協議很難表示 plan、dependency、fallback |
| 難 audit | 只能看到派了誰，看不到「為什麼」 |
| 難 eval | 測試無法精準統計錯誤類型 |

這是最優先的技術債。

---

## 4.2 Agent manifest 若過度依賴自然語言 description，會導致誤派

Router 需要知道每個 Agent 的：

```text
能力
輸入格式
輸出模式
分類上限
權限需求
streaming 支援
健康狀態
成本與延遲
適用與不適用情境
```

若 manifest 只包含 `name` / `description` / `endpoint`，Router 會被迫把「理解 agent 能力」交給 LLM 猜測。

這在 demo 期可以，但在正式治理平台不夠穩。

---

## 4.3 Router policy gate 責任不夠明確

CSP 是治理底座，但 Router 是實際做出 dispatch 的大腦。若 Router 只相信外層已經處理完所有 policy，會有幾個風險：

```text
agent 狀態變更後 Router cache 未更新
classification ceiling 未與 route decision 綁定
使用者可透過某些路徑觸發不該觸發的 agent
agent endpoint / model endpoint SSRF 邊界不一致
高風險 task 未留下 route decision audit
```

Router dispatch 前必須有自己的 fail-closed policy gate。

---

## 4.4 SSE streaming 是高複雜度邊界

Agent streaming 至少可能包含：

```text
OpenAI delta
named SSE event
tool call event
artifact event
trace event
error event
done event
heartbeat
```

Router 要同時滿足：

```text
OpenAI-compatible client 不被破壞
ANILA 前端可以收到 richer anila.* event
agent error 可被轉成一致格式
中途斷線可 resume
慢 client 不造成無限 buffer
```

如果沒有獨立 StreamBridge，主路由檔會逐漸變成條件分支堆疊。

---

## 4.5 Session / resume 若不是 event-sourced，長期會不可靠

Resume 不應只是重新送 request。對 Agent orchestration 來說，resume 應基於：

```text
session_id
owner
event cursor
dispatch state
agent execution id
already emitted events
idempotency key
```

如果沒有 durable event store，會發生：

```text
client 重連後 agent 重複執行
同一事件重送或漏送
多副本 Router 無法 resume
owner mismatch 防護不完整
狀態檔長期膨脹
```

---

## 4.6 Provider 層錯誤分類不足，會讓 fallback 失真

Router 必須分辨錯誤類型：

```text
primary LLM timeout
model gateway 401 / 403
model gateway 429
model gateway 5xx
invalid route schema
agent unavailable
agent denied by policy
agent stream broken
client cancelled
```

如果這些都變成一般 `500`，前端、trace、fallback、告警都無法準確處理。

---

## 4.7 缺少 routing eval，無法證明大腦品質

目前若只有 unit tests，最多只能證明：

```text
程式沒有 crash
某些 parser case 可以通過
SSE 片段處理正常
```

但不能證明：

```text
該派 agent 時有派
不該派 agent 時沒派
模糊情境會追問
高分類資料不會派給低分類 agent
agent down 時 fallback 正確
prompt injection 不會觸發 dispatch
```

這是從「功能可用」到「治理可用」的差距。

---

## 5. 風險分級

| 風險 | 嚴重度 | 發生機率 | 優先處理 |
|---|---:|---:|---:|
| 字串式 dispatch 被 prompt injection 影響 | 高 | 中 | P0 |
| Agent 誤派 | 高 | 中高 | P0 |
| SSE stream 破壞 OpenAI client | 高 | 中 | P0 |
| policy gate 不一致 | 高 | 中 | P0 |
| session resume 重複執行 | 中高 | 中 | P1 |
| provider error taxonomy 不足 | 中 | 高 | P1 |
| routing 品質無 eval | 高 | 高 | P1 |
| multi-agent 任務不可控 | 中 | 中 | P2 |

---

## 6. 現況總評

目前 `packages/anila-core` 已經具備大腦原型的基礎，但還不是完整的大腦治理 runtime。若繼續以現況擴充 Agent 數量，短期會很快看到功能增加；但中長期會在以下方面付出成本：

```text
dispatch 難 debug
agent 誤派難歸因
streaming bug 難修
權限與分類責任不清
多 agent 編排變成 prompt 魔法
使用者體驗受 agent failure 影響
```

因此建議先補核心地基，再擴大 Agent 生態。


---

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


---

# ANILA `packages/anila-core` Agent 代理主路由深度分析報告 — 落地路線圖與測試策略

> 日期：2026-07-06  
> 報告目的：將 ANILA 大腦改善拆成可實作的階段、任務、測試矩陣與驗收標準。

---

## 1. 原則

落地改善時，不建議一次大重構。原因是 Router 是 ANILA 大腦，直接大改會影響：

```text
CSP `/v1/chat/completions`
anila-shell chat UI
Agent dispatch
SSE streaming
session/resume
Studio / FLUX / image-generator
OpenAI-compatible clients
```

建議採「旁路抽象 + 漸進切換 + contract tests」策略。

---

## 2. Phase 0：建立保護網

### 目標

在改核心邏輯前，先補足測試與觀測，避免重構造成回歸。

### 任務

| 任務 | 說明 |
|---|---|
| 建立 fake CSP registry | 可回傳測試用 agent manifest |
| 建立 fake primary LLM | 可控制輸出 direct answer / route decision / invalid output |
| 建立 fake agent server | 可回傳 JSON、SSE、error、slow stream |
| 補 Router contract tests | 測完整 request → route → dispatch → stream |
| 補 prompt injection 測試 | 使用者輸入 `DISPATCH:` 不得直接觸發 |
| 補 SSE 邊界測試 | 多行 data、named event、`[DONE]`、agent error |
| 補 owner mismatch 測試 | session/resume owner 不符必須 fail-closed |

### 驗收標準

```text
不需真 CSP / 真模型 / 真 agent 即可測 Router 主流程
現有 streaming / dispatch 行為有 baseline
新增測試能穩定重現 agent failure 與 SSE 邊界
```

---

## 3. Phase 1：RouteDecision 結構化

### 目標

把 Router 決策輸出從特殊字串轉成 typed schema。

### 任務

| 任務 | 說明 |
|---|---|
| 新增 `RouteDecision` model | Pydantic/dataclass 皆可，先內部使用 |
| 新增 parser / validator | 嚴格驗證 JSON schema |
| 支援 legacy `DISPATCH` | 短期保留 compatibility，但標記 deprecated |
| 新增 invalid schema fallback | invalid route decision 不可直接 dispatch |
| route decision trace | 每次決策留下 trace span |
| 補測試 | invalid JSON、unknown agent、低 confidence、prompt injection |

### 建議檔案方向

```text
packages/anila-core/src/anila_core/router/decision.py
packages/anila-core/src/anila_core/router/decision_parser.py
packages/anila-core/src/anila_core/router/trace.py
packages/anila-core/tests/test_router_decision_schema.py
```

### 驗收標準

```text
所有新路由決策都可以用 RouteDecision 表示
使用者自由文字中的 DISPATCH 不會被當成權威指令
invalid decision 不會觸發 agent
trace 中可看到 route_type / agent_id / reason_codes / confidence
```

---

## 4. Phase 2：Agent Manifest 能力契約

### 目標

讓 Router 不再只靠 agent description，而是根據 capability schema 做第一階段篩選。

### 任務

| 任務 | 說明 |
|---|---|
| 定義 `AgentCapabilityManifest` | 包含 capabilities、classification_ceiling、output_modes |
| 做 manifest normalizer | CSP 舊格式 → Router 內部格式 |
| Capability filter | 先 deterministic 過濾 candidate agents |
| 加入 health / status handling | 非 approved / ready 不進候選 |
| 加入 classification ceiling | 超過分類上限不進候選 |
| 補測試 | disabled agent、unknown capability、classification mismatch |

### 建議欄位

```text
id
version
status
health
capabilities
input_modes
output_modes
streaming
classification_ceiling
required_scopes
timeout_ms
latency_slo_ms
sample_user_intents
negative_intents
```

### 驗收標準

```text
Router prompt 中只出現候選 agent，而不是所有 agent
非 approved / unhealthy agent 不會被 dispatch
classification 不符合時不會進入 LLM routing candidate
```

---

## 5. Phase 3：PolicyGate

### 目標

所有 agent dispatch 前都必須通過 Router policy gate。

### 任務

| 任務 | 說明 |
|---|---|
| 新增 `PolicyGate` interface | 接收 request、decision、manifest、context |
| 新增 gate result | allow / deny / clarify / approval_required |
| 實作基本檢查 | user scope、agent status、classification、endpoint path |
| 寫 audit metadata | policy reason code |
| 補測試 | 權限不足、agent 未核准、分類超限、endpoint 不受信任 |

### 建議檔案方向

```text
packages/anila-core/src/anila_core/router/policy.py
packages/anila-core/tests/test_router_policy_gate.py
```

### 驗收標準

```text
任何 single_agent / multi_agent dispatch 必須先通過 PolicyGate
policy denied 時不呼叫 agent
policy denied 有 safe user message
trace/audit 有 reason_code
```

---

## 6. Phase 4：StreamBridge 抽象化

### 目標

把 SSE / streaming 轉換變成獨立狀態機，避免散落在主路由流程。

### 任務

| 任務 | 說明 |
|---|---|
| 定義 `StreamEvent` | 標準內部事件模型 |
| 新增 SSE parser | 支援多行 data、named event、heartbeat |
| 新增 OpenAI emitter | 輸出 OpenAI-compatible SSE |
| 新增 ANILA event emitter | 輸出 `anila.*` event |
| 錯誤轉換 | agent error → normalized error |
| session event hook | stream event 可寫入 session store |
| 補測試 | named event、multi-line、done、broken stream、client cancel |

### 建議檔案方向

```text
packages/anila-core/src/anila_core/router/stream_bridge.py
packages/anila-core/tests/test_router_stream_bridge.py
```

### 驗收標準

```text
OpenAI-compatible client 收到合法 SSE
ANILA 前端可收到 richer anila.* event
agent error 不會讓 client 永久等待
[DONE] 行為一致
stream parser 不因多行 data 截斷
```

---

## 7. Phase 5：Session Event Store / Resume

### 目標

讓 resume 基於 event cursor，而不是重新執行 request。

### 任務

| 任務 | 說明 |
|---|---|
| 定義 `SessionStore` interface | append / read_after / close / ttl |
| 實作 local fallback | 先保留單機開發可用 |
| 實作 Redis-backed store | 支援多副本與 stream cursor |
| owner check | session owner mismatch fail-closed |
| idempotency key | 避免重送造成重複 agent 執行 |
| 補測試 | cursor resume、owner mismatch、expired session、duplicate event |

### 建議檔案方向

```text
packages/anila-core/src/anila_core/router/session_store.py
packages/anila-core/tests/test_router_session_store.py
packages/anila-core/tests/test_router_resume.py
```

### 驗收標準

```text
client 可從指定 cursor 之後恢復
owner 不符時拒絕
已送過事件不重複
agent 已完成時可補送剩餘事件與 done
```

---

## 8. Phase 6：Provider capability / error taxonomy

### 目標

讓 Router 能根據 provider 能力與錯誤類型做正確 fallback。

### 任務

| 任務 | 說明 |
|---|---|
| 定義 provider capabilities | streaming、tool calling、json schema、images |
| 統一 provider errors | timeout、401、429、5xx、invalid response |
| route decision feature check | 若 provider 不支援 JSON schema，走 fallback parser |
| retry / fallback policy | 依錯誤類型決定 |
| 補測試 | timeout、rate limit、invalid response、unsupported feature |

### 建議檔案方向

```text
packages/anila-core/src/anila_core/providers/base.py
packages/anila-core/src/anila_core/providers/openai_compat.py
packages/anila-core/tests/test_openai_compat_provider.py
```

### 驗收標準

```text
provider timeout 不會變成不明 500
429 / auth failed / invalid response 可被分辨
Router 可依 provider capability 選擇 route decision 模式
```

---

## 9. Phase 7：Routing Eval Dataset

### 目標

把大腦品質變成可量化指標。

### Dataset 格式

```json
{
  "id": "image_generation_001",
  "input": "幫我生成一張任務簡報封面圖",
  "context": {
    "user_role": "analyst",
    "classification": "internal"
  },
  "expected": {
    "route_type": "single_agent",
    "agent_id": "image-generator"
  },
  "must_not_route_to": ["rag-search"],
  "notes": "明確影像生成需求"
}
```

### 覆蓋類型

| 類型 | 測試目的 |
|---|---|
| direct answer | 不該派 agent |
| single agent | 明確需求派正確 agent |
| clarify | 模糊需求應追問 |
| prompt injection | 不被格式字串誘導 |
| permission denied | 無權限不 dispatch |
| classification denied | 高分類不派低 ceiling agent |
| agent unavailable | 正確 fallback |
| multi-intent | 受控 plan 或追問 |
| streaming abnormal | agent stream 壞掉可處理 |
| resume | 中斷後接續 |

### 指標

```text
route_accuracy
false_dispatch_rate
missed_dispatch_rate
clarification_precision
policy_denial_correctness
fallback_success_rate
invalid_decision_rate
p95_route_latency_ms
agent_failure_rate
resume_success_rate
```

### 驗收標準

```text
每次改 Router prompt / decision parser / manifest schema 可跑 routing eval
eval 結果可比較前後版本
至少有 prompt injection 與 classification gate 的 regression cases
```

---

## 10. Phase 8：受控 Multi-Agent Plan

### 目標

支援複合任務，但不讓 LLM 任意自由編排。

### 初期策略

```text
只允許白名單 plan pattern
每個 step 都做 capability + policy gate
step 之間資料分類不可降級
每個 step 都有 timeout / retry / trace
artifact merge 交給 Studio 或明確 merge strategy
```

### 不建議

```text
agent 彼此自由互叫
LLM 自由指定任意 endpoint
LLM 自由傳遞未分類中間結果
沒有 trace 的 chain-of-agent
```

---

## 11. 測試矩陣總表

| 層級 | 測試 |
|---|---|
| Unit | RouteDecision parser、PolicyGate、StreamBridge、Provider errors |
| Contract | fake CSP registry、fake agent、fake model |
| Integration | Router + CSP dev stack + fake agent |
| E2E | `/v1/chat/completions` → Router → Agent → SSE |
| Security | prompt injection、SSRF、scope、classification |
| Reliability | timeout、agent crash、stream broken、client disconnect |
| Resume | cursor、owner、expired session、duplicate event |
| Eval | routing dataset accuracy |

---

## 12. 建議驗收 checklist

```text
[ ] route decision 全部可被 schema 驗證
[ ] legacy DISPATCH 不再是主要控制契約
[ ] prompt injection 不會觸發 dispatch
[ ] dispatch 前必經 PolicyGate
[ ] policy denied 不呼叫 agent
[ ] agent manifest 有 capability / classification / status
[ ] SSE parser 支援多行 data / named event / done
[ ] stream error 有 normalized event
[ ] session resume 基於 cursor
[ ] owner mismatch fail-closed
[ ] provider error taxonomy 可分辨 timeout / 429 / auth / 5xx
[ ] routing eval dataset 可跑
[ ] trace 裡可看到 route_type / agent_id / reason_codes / policy result
```

---

## 13. 最小可行落地版本

若要用最小成本先做出顯著改善，建議最小版本包含：

```text
1. RouteDecision schema
2. DISPATCH legacy compatibility + deprecation
3. PolicyGate basic checks
4. StreamBridge parser test
5. route decision trace
6. fake CSP + fake agent tests
```

這六項完成後，即使還沒做 Redis resume、multi-agent、完整 eval，Router 的安全性、可測性、可維護性都會明顯上升。


---

# ANILA 大腦技術規格草案 — `RouteDecision` / `PolicyGate` / `StreamBridge`

> 日期：2026-07-06  
> 目的：提供可直接進入設計審查或派工的技術規格草案。

---

# Part A：`RouteDecision` 規格

## A1. 設計目標

`RouteDecision` 是 Router 對一次使用者請求的結構化判斷結果。它取代自由文字或 `DISPATCH:<agent_id>:<query>` 類字串控制契約。

## A2. 不變式

```text
任何 agent dispatch 必須由有效 RouteDecision 產生
未知 agent_id 不可 dispatch
invalid schema 不可 dispatch
低 confidence 時應 fallback 或 clarify
reason_codes 必須可 audit
```

## A3. Schema 草案

```python
from typing import Literal
from pydantic import BaseModel, Field

class PolicyRequirements(BaseModel):
    classification_ceiling: str | None = None
    requires_audit: bool = True
    requires_approval: bool = False
    required_scopes: list[str] = Field(default_factory=list)

class FallbackDecision(BaseModel):
    route_type: Literal["direct_answer", "clarify", "deny"]
    message: str | None = None
    reason_code: str | None = None

class AgentStep(BaseModel):
    step_id: str
    agent_id: str
    input: dict
    depends_on: list[str] = Field(default_factory=list)
    timeout_ms: int | None = None

class ExecutionPlan(BaseModel):
    type: Literal["single_agent", "multi_agent_plan"]
    steps: list[AgentStep]
    merge_strategy: str | None = None

class RouteDecision(BaseModel):
    route_type: Literal[
        "direct_answer",
        "single_agent",
        "multi_agent_plan",
        "clarify",
        "deny"
    ]
    confidence: float = Field(ge=0.0, le=1.0)
    reason_codes: list[str] = Field(default_factory=list)
    user_visible_reason: str | None = None

    direct_answer: str | None = None

    agent_id: str | None = None
    rewritten_query: str | None = None

    plan: ExecutionPlan | None = None
    fallback: FallbackDecision | None = None
    policy_requirements: PolicyRequirements = Field(default_factory=PolicyRequirements)
```

## A4. 驗證規則

| route_type | 必要欄位 | 禁止或限制 |
|---|---|---|
| `direct_answer` | `direct_answer` 或後續 provider call | 不可有 `agent_id` |
| `single_agent` | `agent_id`, `rewritten_query` | `agent_id` 必須在 candidate list |
| `multi_agent_plan` | `plan.steps` | 每個 step agent 必須通過 policy |
| `clarify` | `user_visible_reason` | 不可 dispatch |
| `deny` | `user_visible_reason`, reason code | 不可 dispatch |

## A5. Reason code 建議

```text
USER_REQUESTED_IMAGE
USER_REQUESTED_DOCUMENT_GENERATION
USER_REQUESTED_KNOWLEDGE_SEARCH
USER_REQUESTED_AGENT_BY_NAME
INSUFFICIENT_INFORMATION
NO_AGENT_MATCH
LOW_CONFIDENCE
POLICY_RESTRICTED
CLASSIFICATION_TOO_HIGH
AGENT_UNAVAILABLE
PROMPT_INJECTION_SUSPECTED
```

## A6. Invalid decision fallback

```text
invalid JSON
  ↓
trace invalid_decision
  ↓
不 dispatch
  ↓
若 safe，可 direct answer：「我需要更多資訊才能選擇工具」
  ↓
否則回 generic error
```

---

# Part B：`PolicyGate` 規格

## B1. 設計目標

`PolicyGate` 是 Router dispatch 前的強制治理檢查。它不取代 CSP，但防止 Router 自己成為繞過治理的捷徑。

## B2. Interface 草案

```python
class PolicyGateInput(BaseModel):
    request_id: str
    trace_id: str
    user_id: str
    user_scopes: list[str]
    task_id: str | None
    classification: str | None
    route_decision: RouteDecision
    agent_manifest: dict | None

class PolicyGateResult(BaseModel):
    action: Literal["allow", "deny", "clarify", "approval_required"]
    allowed: bool
    reason_code: str
    safe_message: str | None = None
    audit_required: bool = True
```

## B3. 檢查順序

```text
1. RouteDecision schema 已驗證
2. route_type 是否需要 dispatch
3. agent_id 是否存在於 registry snapshot
4. agent status 是否 approved / ready
5. user scope 是否滿足 required_scopes
6. request classification 是否 <= agent classification_ceiling
7. endpoint 是否只能經 CSP proxy 或 trusted path
8. SSRF / trusted hosts 是否通過
9. 高風險操作是否需要 approval
10. 回傳 allow / deny / clarify / approval_required
```

## B4. Denial reason code

```text
UNKNOWN_AGENT
AGENT_NOT_APPROVED
AGENT_UNHEALTHY
MISSING_SCOPE
CLASSIFICATION_EXCEEDS_AGENT_CEILING
UNTRUSTED_ENDPOINT
APPROVAL_REQUIRED
POLICY_CONFIG_MISSING
```

## B5. Fail-closed 規則

以下狀況預設 deny：

```text
agent manifest 缺分類上限且 task 有分類
agent endpoint 無法驗證
user scope 無法取得
agent status unknown
policy config 無法讀取
CSP registry snapshot 過期且無安全 cache
```

---

# Part C：`StreamBridge` 規格

## C1. 設計目標

`StreamBridge` 將 agent / provider 的 streaming 輸入轉成 Router 內部標準事件，再輸出為 OpenAI-compatible SSE 與 ANILA richer events。

## C2. 標準事件模型

```python
class StreamEvent(BaseModel):
    event_id: str
    cursor: int | None = None
    event_type: str
    payload: dict
    trace_id: str
    session_id: str | None = None
    created_at: str
```

## C3. Event type

```text
router.decision
agent.start
agent.delta
agent.tool_call
agent.artifact
agent.warning
agent.error
agent.done
router.fallback
session.checkpoint
```

對外 SSE event 可映射成：

```text
anila.router.decision
anila.agent.start
anila.agent.delta
anila.agent.tool_call
anila.agent.artifact
anila.agent.error
anila.agent.done
anila.router.fallback
```

## C4. SSE parser 要求

必須支援：

```text
event: name
data: line1
data: line2

: heartbeat

data: [DONE]
```

多行 `data:` 合併規則：

```text
同一 SSE event 內的多個 data line 必須以 newline 合併
不可只取最後一行
不可把不同 event 合併
```

## C5. Error 行為

| agent 行為 | Router 行為 |
|---|---|
| agent 回 OpenAI error chunk | 轉成 normalized error event |
| agent 中途斷線 | 發 `anila.agent.error`，再依 fallback policy 決定是否 `[DONE]` |
| agent 回非 JSON | 若是 plain text delta，可包成 delta；否則 invalid response |
| agent 無 `[DONE]` | timeout 後產生 stream broken |
| client disconnect | 停止往 client 寫，取消 downstream 或進入 detached mode |

## C6. Resume hook

StreamBridge 每產生一個標準事件，應可呼叫：

```python
session_store.append(session_id, stream_event)
```

Resume 時：

```python
events = session_store.read_after(session_id, cursor)
```

---

# Part D：測試案例草案

## D1. RouteDecision tests

```text
test_valid_single_agent_decision
test_unknown_agent_rejected
test_invalid_json_rejected
test_prompt_injection_dispatch_text_ignored
test_low_confidence_clarify
test_multi_agent_plan_requires_all_steps_valid
```

## D2. PolicyGate tests

```text
test_policy_allows_approved_agent
test_policy_denies_unapproved_agent
test_policy_denies_missing_scope
test_policy_denies_classification_ceiling
test_policy_denies_untrusted_endpoint
test_policy_fail_closed_when_manifest_missing
```

## D3. StreamBridge tests

```text
test_sse_multiline_data_preserved
test_named_event_converted_to_anila_event
test_done_emitted_once
test_agent_error_normalized
test_broken_stream_emits_error
test_client_cancel_stops_downstream
```

## D4. Integration tests

```text
test_router_direct_answer_no_agent_called
test_router_single_agent_dispatch_via_fake_csp_proxy
test_router_policy_denied_does_not_call_agent
test_router_agent_stream_to_openai_sse
test_router_resume_from_cursor
```

---

# Part E：MVP 驗收

MVP 完成後，應可證明：

```text
1. Router 不再依賴自由文字作為 dispatch 權威
2. 任何 dispatch 前都經過 PolicyGate
3. agent stream 可以被標準化
4. route decision 可以被 trace/audit
5. prompt injection 不會觸發 agent dispatch
6. fake CSP / fake model / fake agent 可測完整流程
```


---

# ANILA 大腦改善 — 派工清單與審查 Checklist

> 日期：2026-07-06  
> 目的：提供可直接派工、審查、驗收的任務清單。

---

## 1. Epic 拆分

```text
Epic A：RouteDecision 結構化
Epic B：Agent Manifest 能力契約
Epic C：Router PolicyGate
Epic D：StreamBridge
Epic E：Session Event Store / Resume
Epic F：Provider Capability / Error Taxonomy
Epic G：Routing Eval Dataset
Epic H：受控 Multi-Agent Plan
```

---

## 2. Epic A：RouteDecision 結構化

### 任務

```text
A1. 新增 RouteDecision schema
A2. 新增 decision parser / validator
A3. 新增 invalid decision fallback
A4. 保留 legacy DISPATCH compatibility，但標記 deprecated
A5. 新增 route decision trace payload
A6. 補單元測試
```

### 驗收

```text
[ ] valid single_agent decision 可 dispatch
[ ] invalid JSON 不 dispatch
[ ] unknown agent 不 dispatch
[ ] 使用者輸入 DISPATCH 字串不會直接觸發 dispatch
[ ] trace 有 route_type / confidence / reason_codes
```

---

## 3. Epic B：Agent Manifest 能力契約

### 任務

```text
B1. 定義內部 AgentCapabilityManifest
B2. 實作 CSP manifest normalizer
B3. 實作 capability filter
B4. 加入 status / health / classification ceiling
B5. 補測試
```

### 驗收

```text
[ ] disabled agent 不進 candidate
[ ] unhealthy agent 不進 candidate
[ ] classification ceiling 不足時不進 candidate
[ ] Router prompt 只看到候選 agent summary
```

---

## 4. Epic C：Router PolicyGate

### 任務

```text
C1. 新增 PolicyGate interface
C2. 實作 agent status check
C3. 實作 user scope check
C4. 實作 classification check
C5. 實作 endpoint/trusted path check
C6. policy result 寫 trace/audit metadata
C7. 補測試
```

### 驗收

```text
[ ] 所有 dispatch 前必經 PolicyGate
[ ] policy deny 不呼叫 agent
[ ] deny message 不洩漏敏感資訊
[ ] fail-closed case 有測試
```

---

## 5. Epic D：StreamBridge

### 任務

```text
D1. 定義 StreamEvent
D2. 實作 SSE parser
D3. 實作 OpenAI SSE emitter
D4. 實作 ANILA event mapper
D5. 實作 error normalization
D6. 接 session store hook
D7. 補測試
```

### 驗收

```text
[ ] 支援 multi-line data
[ ] 支援 named event
[ ] [DONE] 只送一次
[ ] agent error 有 normalized event
[ ] stream broken 不會讓 client 永久等待
```

---

## 6. Epic E：Session Event Store / Resume

### 任務

```text
E1. 定義 SessionStore interface
E2. 實作 local store
E3. 實作 Redis-backed store
E4. 實作 cursor resume
E5. 實作 owner check
E6. 實作 TTL cleanup
E7. 補測試
```

### 驗收

```text
[ ] cursor 後事件可回放
[ ] owner mismatch fail-closed
[ ] expired session 有明確錯誤
[ ] duplicate event 不重送
```

---

## 7. Epic F：Provider Capability / Error Taxonomy

### 任務

```text
F1. 定義 ProviderCapabilities
F2. 定義 ProviderError hierarchy
F3. openai_compat provider 映射 timeout / 401 / 429 / 5xx
F4. Router 根據 provider capability 選擇 JSON schema / fallback parser
F5. 補測試
```

### 驗收

```text
[ ] timeout / 429 / auth failed / 5xx 可區分
[ ] provider 不支援 JSON schema 時有 fallback
[ ] provider invalid response 不會被當作 successful route
```

---

## 8. Epic G：Routing Eval Dataset

### 任務

```text
G1. 建立 eval case schema
G2. 建立初版 dataset
G3. 建立 eval runner
G4. 輸出 route accuracy / false dispatch / missed dispatch
G5. 加入 prompt injection / classification cases
```

### 驗收

```text
[ ] 可本機跑 routing eval
[ ] 每個 case 有 expected route_type / agent_id
[ ] eval 結果可比較前後版本
[ ] prompt injection regression case 固定保留
```

---

## 9. Epic H：受控 Multi-Agent Plan

### 任務

```text
H1. 定義 ExecutionPlan schema
H2. 只支援白名單 plan pattern
H3. 每個 step 套 capability filter + policy gate
H4. 每個 step 寫 trace span
H5. 實作 timeout / retry / fallback
H6. 補整合測試
```

### 驗收

```text
[ ] multi-agent plan 不可指定任意 endpoint
[ ] 每個 step policy gate 通過才執行
[ ] step 輸出有分類與 trace metadata
[ ] plan failure 有 fallback
```

---

## 10. Code Review Checklist

### Router decision

```text
[ ] 沒有新增以自由文字當控制訊號的路徑
[ ] RouteDecision schema 驗證嚴格
[ ] unknown agent fail-closed
[ ] invalid LLM output 不 dispatch
```

### Security / policy

```text
[ ] dispatch 前必經 PolicyGate
[ ] user scope 有檢查
[ ] classification ceiling 有檢查
[ ] endpoint 不可繞過 CSP proxy
[ ] SSRF guard 沒被放寬
[ ] error message 不洩漏 token / endpoint secret
```

### Streaming

```text
[ ] SSE parser 支援 multi-line data
[ ] [DONE] 行為一致
[ ] agent error 有 normalized event
[ ] slow client / disconnect 有處理
```

### Session / resume

```text
[ ] session owner check fail-closed
[ ] resume 不會重複執行 agent
[ ] event cursor 單調遞增
[ ] TTL / cleanup 有考慮
```

### Observability

```text
[ ] route decision 有 trace
[ ] policy deny 有 reason code
[ ] agent dispatch 有 latency / status
[ ] fallback 有記錄
```

### Tests

```text
[ ] 有 fake CSP / fake agent 測試
[ ] 有 prompt injection 測試
[ ] 有 policy denied 測試
[ ] 有 SSE 邊界測試
[ ] 有 provider error 測試
```

---

## 11. 不建議接受的 PR 類型

```text
只加長 Router prompt，沒有 schema 或測試
新增 agent dispatch shortcut，繞過 CSP / PolicyGate
直接信任 agent 回傳 event，不做 normalize
把 classification / scope 檢查留給 agent 自己
新增 multi-agent 自由編排但沒有白名單與 trace
只測 happy path，沒有 agent failure / SSE broken case
```

---

## 12. 最小 Sprint 建議

若以兩週 sprint 規劃，建議第一個 sprint 只做：

```text
1. RouteDecision schema
2. invalid decision fallback
3. legacy DISPATCH compatibility
4. basic PolicyGate
5. route decision trace
6. fake CSP + fake agent tests
```

這是性價比最高的一組。
