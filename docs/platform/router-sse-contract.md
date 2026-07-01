# Router ⟷ ANILA UI — SSE 事件契約（FROZEN, ANILA v1）

> **狀態：FROZEN（凍結）for ANILA v1**（2026-07-01 對碼驗證 + review 通過；typed-terminal 採方案 A 獨立 `anila.terminal` 事件）。這是 Router 與 ANILA UI 的**唯一接縫契約**，兩端 lockstep。
> **Producer** = anila-core Router（`anila-core/src/anila_core/api/router_server.py`）。
> **Consumer** = ANILA UI（`ANILA_UI/anila-ui/src/runtime/sse.js` + `app.jsx:applyMeta` + `trust.jsx`）。
> 本檔記錄「**已實作於兩端**」的 de-facto 契約（2026-07-01 對碼盤點）＋ reserved/缺口。改契約要同時改兩端並更新本檔（見文末流程）。

---

## 1. 端點與封包

- **端點**：`POST /router/v1/chat/completions`，body 為 OpenAI-compat（`{model, messages, stream}`）。
- `stream: true` → `text/event-stream`；`stream: false` → 單一 JSON（見 §5）。
- **header**：`X-CSRF-Token`（double-submit cookie）、`X-ANILA-Conversation-Id`（分類 latch 用）；回應 header `X-Anila-Session-Id`（pin 後續回合）。
- **SSE 區塊格式**（`_make_event` / `_make_chunk`）：
  ```
  event: <name>\n
  data: <json>\n
  \n
  ```
  無 `event:` 行者 = 預設 OpenAI `chat.completion.chunk`。

---

## 2. 事件目錄（v1 LIVE — 有 producer）

### （預設，無 event 行）OpenAI chunk — **answer delta**
```json
{"id":"chatcmpl-…","object":"chat.completion.chunk","created":<int>,"model":"<str>",
 "choices":[{"index":0,"delta":{"content":"<str>"},"finish_reason":"<str|null>"}]}
```
- producer `_make_chunk`；consumer `sse.js` → `onText`（累積 `delta.content`）、`finish_reason` → `onFinishReason`。
- **finish_reason**：目前只有 OpenAI 標準 `"stop"` / `"length"`。`length` → UI 顯示「繼續」鈕。（結構化 typed-terminal 見 §6。）

### `event: anila.trace` — 路由/派送步驟
```json
{"kind":"<str>","label":"<str>","detail":"<str>","status":"ok|error","latency_ms":<int?>}
```
- producer `_make_trace_step`（每步一事件）；consumer `onTrace` → 累積 `message.trace` + `stageLabel`。

### `event: anila.meta` — 最終 metadata（一回合一次，串流在末尾）
```json
{"trace_id":"<str>","trace":[],"citations":[Citation],"confidence":Confidence|null,
 "handoff_chain":[HandoffEntry],"follow_ups":["<str>"],"latency_ms":<int|null>,
 "classified":<bool>,"usage":Usage?,"reasoning":"<str>?"}
```
- producer `_default_anila_meta` / `_merge_anila_meta`；consumer `app.jsx:applyMeta`。
- 串流時 `trace:[]`（步驟已由 `anila.trace` 逐一送，避免重複）；非串流一次 bundle。
- `usage` / `reasoning` 為 optional key。

### `event: anila.reasoning` — 推理串流（gpt-oss 類模型）
```json
{"delta":"<str>"}
```
- consumer `onReasoning` → `message.reasoning += delta`。

### `data: [DONE]` — 終止標記

---

## 3. 巢狀型別（consumer 端在 `trust.jsx` 渲染）

| 型別 | Shape | 渲染元件 |
|---|---|---|
| **Citation** | `{id, title, section?, snippet?, score?, source_uri?, updated_at?}` | `CitationsDrawer`（來源面板）+ inline `[N]` `CitationInline` |
| **Confidence** | `{level:"high"｜"medium"｜"low", score:number, reasons?:string[]}` | `ConfidenceChip` + 低信心 `FollowUpSuggestions` |
| **HandoffEntry** | `{agent_id, label, status, latency_ms?, input_summary?, output_summary?}` | `HandoffTimeline`（跑了哪個 agent） |
| **Usage** | `{prompt_tokens, completion_tokens, total_tokens}` | `AuditWatermark` |

Router dispatch 時 `_merge_anila_meta` 會在 `handoff_chain` 前插一筆 `{agent_id:"anila-router", label:"Router dispatch", …}`。`classified` 為單向 latch（只升不降）。

---

## 4. 非串流（`stream: false`）

單一 JSON（`_make_full_response`）：
```json
{"id":"chatcmpl-…","object":"chat.completion","created":<int>,"model":"<str>",
 "choices":[{"index":0,"message":{"role":"assistant","content":"<str>"},"finish_reason":"stop"}],
 "usage":Usage,"anila_meta":<同 §2 anila.meta shape>}
```

---

## 5. PASSTHROUGH / 條件性事件（**路徑是活的**，但 ANILA v1 一般不觸發）

> **更正（2026-07-01 對碼）**：這些**不是「完全無 producer」**——轉發/emit 路徑是活的，只是 ANILA v1 的 agent 通常不送。勿當「一定會來」，也勿假設「永遠不來」。`sse.js` 兩端 parser/callback 皆備。

- **Agent passthrough**（`router_server.py:826`：agent 串流中送的事件，除 `anila.meta`（會被 merge）、`anila.trace`（即時轉發）外，**其餘一律 `_make_event` 原樣轉發**）——**agent 有送才出現**：
  - `anila.tool_call_started` / `anila.tool_call_finished` — ANILA 派送制、v1 agent 不送（A/B 決策封存 v2，見 [ROADMAP §3c](../ROADMAP.md)）。
  - `anila.spans` — OTel-style trace tree，同理。
  - `anila.todos_updated` — 同理。
- **Resume / pause 流程**（端點 `POST /v1/sessions/{id}/answer`，`streamSessionAnswer`）：
  - `anila.interrupt_requested` — agent 暫停（ask_user / plan / tool_approval）時經 passthrough 送。
  - `anila.resumed` — **Router 在 resume 端點真的 emit**（`router_server.py:1187`）。整條 pause-resume 機制是活的，只是**要 agent 真的 interrupt 才會走到**。

**規則**：勿為這些「建主動 UI widget」直到 ANILA agent 真的會送（v2）。但它們是**有意保留的活路徑**，不是死碼。（agent 錯誤現況：走 `anila.trace status=error` + friendly chunk（`router_server.py:833`），**非**結構化終止——見 §6 typed-terminal。）

---

## 6. v1 待補缺口（皆在**生產端**；UI 消費端已就緒）

| 缺口 | 現況 | 補在哪 |
|---|---|---|
| **confidence** | Router 送 `None`；`ConfidenceChip` 全黑 | Stage 3 / agent：檢索分數 → `{level,score,reasons}` |
| **citations** | 直答空；agent 需填 `meta.citations` | agent RAG（+ R-WIRE-1 `related`→citations） |
| **usage（串流）** | 串流 usage 歸零 | 補真 token 計數 |
| **typed-terminal** | 只有 `finish_reason`（stop｜length） | **Stage 3**：Router emit 結構化終止；UI 補 render「為何停」（唯一真 UI 缺口） |

### typed-terminal 事件定義（**✅ 已定：方案 A — 獨立事件**，2026-07-01 拍板；Stage 3 emit）
新增獨立事件 `event: anila.terminal`（**不動** OpenAI chunk 的 `finish_reason`，不污染 OpenAI 相容層）：
```json
{"reason":"completed｜max_turns｜aborted｜budget｜length｜error","detail":"<str>?"}
```
- Router 在 `[DONE]` 前 emit（一回合一個）：正常=`completed`；撞回合/預算上限=`max_turns`/`budget`；使用者中止=`aborted`；上游錯=`error`；`max_tokens` 截斷=`length`。
- **producer 狀態**：✅ **已實作**（`feat/stage3-typed-terminal`）——`_with_terminal` wrapper 在全 5 個串流出口 emit：預設 `completed`，dispatch agent 錯誤 emit `error`。⬜ 剩 `aborted`（需 honor abort）、`max_turns`（需 query_engine 補救）、`budget`——**gated 於 anila-core Phase 2 keystone 重構**。
- **consumer 狀態**：✅ **已實作**——`sse.js` `onTerminal` dispatch、`app.jsx` 存 `msg.terminal`、`chat.jsx` bubble render「為何停」badge（`completed`/`length` 靜默，`length` 走既有「繼續」鈕）。

---

## 7. 變更流程（凍結後）

要新增/修改事件或型別：
1. **先改本檔**（契約是 SSOT）。
2. anila-core Router `router_server.py` 改 emit。
3. ANILA UI `sse.js`（parser/dispatch）+ `app.jsx:applyMeta`（套用）+ `trust.jsx`（渲染）。
4. **同一 PR / sync 三處一致**；`router_server.py` / `sse.js` 是共用碼，**main 起 → cherry-pick 全分支**。
