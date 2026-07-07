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
