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
