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
