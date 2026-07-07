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
