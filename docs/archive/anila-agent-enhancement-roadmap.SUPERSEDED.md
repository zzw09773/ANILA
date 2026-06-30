# anila-agent Enhancement Roadmap — 三 SDK 整合報告

> **狀態**:Active · **撰寫時間**:2026-05-26 · **資料來源**:三份 deep-dive 報告(共 4144 行)
>
> **三份來源報告**(請對應細節讀):
> - [`openai-agents-python-deep-dive.md`](./openai-agents-python-deep-dive.md) (1338 行)
> - [`antigravity-sdk-python-deep-dive.md`](./antigravity-sdk-python-deep-dive.md) (1160 行)
> - [`claude-code-src-deep-dive.md`](./claude-code-src-deep-dive.md) (1646 行)

本檔是上述三份報告的**整合分析**:對齊優先級、解依賴、列出跨 SDK 的共識與衝突,給 anila-agent v0.3 / v1 一份可執行的 roadmap。

---

## 0. TL;DR — 給趕時間的 reviewer

| 維度 | 結論 |
|---|---|
| **三 SDK 各自定位** | openai-agents = anila-agent **runtime base**(已用 ~30%);claude-code = anila-agent **harness pattern source**(已 port memdir + 部分 hook);antigravity = **架構成熟度 reference**(不抄 Go binary,只抄設計) |
| **最大缺口(三 SDK 共識)** | **sub-agent dispatch**(claude-code §4.1 + openai-agents handoffs)、**hook 系統薄**(三 SDK 都比 anila-agent 完整)、**tool surface 太瘦**(沒 metadata / guardrail / context injection) |
| **總工作量** | P0:**8-12 d**(6 sprint-week 1 人) · P1:**14-22 d** · P2:**20-30 d** · 全部約 **10-13 週** 1 人 full-time |
| **推薦起手 sprint** | **Sprint 1 (P0-A,2 週)**:lifecycle hooks 補滿 + tracing 基礎 + tool metadata + 基礎 guardrails + AgentTool dispatch。一次拉起 4 個閉環 |
| **戰略升級路徑** | Triggers(antigravity)+ Connections(antigravity)+ Handoffs(openai-agents)+ AgentTool(claude-code) **聯動之後**,anila-agent 從「對話模板」進化到「自動化 sub-agent 平台」(KB 變動自動 reindex、studio job 完成自動續推、健康檢查自動切 fallback) |
| **不要做的方向** | openai-agents 的 `realtime/` / `voice/` / `sandbox/`;antigravity 的 Go binary + WebSocket + protobuf + 鎖 Gemini;claude-code 的 ink/components/screens/buddy/voice 終端 UI;三 SDK 任何「綁特定雲端 LLM」的部分 |

---

## 1. 三 SDK 戰略定位 — 為何各保留 + 怎麼分工

```
┌──────────────────────────────────────────────────────────┐
│  anila-agent (Python, sub-agent template)                │
├──────────────────────────────────────────────────────────┤
│  Runtime layer  ◀──── openai-agents-python (主 base)     │
│    Agent / Runner / RunHooks / FunctionTool / Session    │
│                                                          │
│  Harness layer  ◀──── claude-code-src (TS, ported)       │
│    memdir / 7-stage turn loop / 5-event hook /           │
│    slash-command CLI / compact strategies                │
│                                                          │
│  Architecture   ◀──── antigravity-sdk-python (reference) │
│    Hook taxonomy (Inspect/Decide/Transform) /            │
│    Policy DSL / Triggers / ConnectionStrategy            │
│    (不引依賴,只借鑑設計)                                  │
└──────────────────────────────────────────────────────────┘
```

### 1.1 三 SDK 的天然分工

| SDK | 在 anila-agent 內的角色 | 抄取方式 |
|---|---|---|
| **openai-agents-python** | **執行引擎**(已直接 `from agents import ...`) | 從 SDK 直接 import + 用新 release feature(handoffs / guardrails / tracing / mcp / RunState) |
| **claude-code-src** | **設計藍圖**(TS source,不執行) | **重寫 Python 等效版** 進 `anila_agent/`,參考 interface 但不抄 logic verbatim |
| **antigravity-sdk-python** | **架構教材**(Python source,但鎖 Gemini) | **只抄設計概念**(三類 hook 分類 / Policy DSL / Trigger / Connection),自己重寫,不引依賴 |

### 1.2 三份報告的視角差異

- **openai-agents** 報告角度:「**SDK 已 release 但 anila-agent 還沒用的 surface**」 — 14 個 pattern
- **claude-code** 報告角度:「**Claude Code 有設計但 anila-agent 還沒 port 的 harness primitive**」 — 23 個 pattern
- **antigravity** 報告角度:「**比 anila-agent 現用更成熟的工程模式**」 — 11 個 pattern

三份視角互補,不重疊,結論一致:**anila-agent 的 runtime / harness / architecture 三層都有大量未開發 surface**。

---

## 2. 跨 SDK 能力對比 Matrix

每一行是一個 agent 能力維度,看三個 SDK 各自的成熟度 + 對 anila-agent 該選誰。

| 能力維度 | openai-agents | antigravity | claude-code | anila-agent 推薦 | 來自 |
|---|---|---|---|---|---|
| **Hook 系統** | `RunHooks` + `AgentHooks` 12 callback,**lifecycle-driven** | 三類強型別 hook (**Inspect / Decide / Transform**) + decorator factory | 5-event declarative hook + flavor 多樣(command/prompt/http) | **混合**:lifecycle callback 用 openai-agents、event taxonomy 用 antigravity 分類、hook flavor 用 claude-code | 三 SDK 都有,各勝場 |
| **Multi-agent (sub-agent dispatch)** | `Handoff` 控制權轉移 + `Agent.as_tool` sub-routine | (對應 `connections/`,本質不同) | `AgentTool` sub-agent + `forkSubagent` byte-identical prefix(prompt cache) | **兩條都抄**:`Handoff`(交給 SDK)+ `AgentTool` 風格(claude-code,抄 byte-identical prefix 對 vLLM prompt cache 有效) | openai-agents + claude-code |
| **Guardrails** | `InputGuardrail / OutputGuardrail` + tripwire + tool-level 三 behavior | `Policy DSL`:allow/deny/disable + priority bucket + workspace_only | 沒有顯式 guardrail concept(走 hook 阻擋) | **openai-agents 為主**(decorator + tripwire 直觀);**Policy DSL 補強**(antigravity 的 priority 排序解 conflict) | openai-agents + antigravity |
| **Tracing** | `Trace` + `Span` + `TracingProcessor`(OTel-friendly) | 內嵌在 Go binary,不可移植 | 自家 telemetry,但 anila-agent 沒 port | **openai-agents**(OTel spec standard) | openai-agents |
| **MCP** | `MCPServer` ABC + `MCPServerManager` stdio/sse/streamable | `McpBridge` stdio/SSE/HTTP 三 transport | 也接 MCP 但實作不同 | **openai-agents**(類別清楚)+ 參考 antigravity transport 配置 | openai-agents + antigravity refer |
| **Memory(memdir 長期)** | 沒有對應 | 沒有對應 | `memdir/` 4-type taxonomy(已 port 進 anila-agent) | **claude-code 模式**(已完成) | claude-code |
| **Session(短期 conversation)** | `Session` Protocol + `SQLiteSession`(已用) | `Conversation` 層 turn / compaction index 追蹤 | turn loop 內帶,沒抽出 | **openai-agents SQLiteSession**(現況)+ 看 antigravity 的 turn/compaction index 補進 metadata | openai-agents 主 + antigravity 補 |
| **Triggers(背景事件喚醒)** | 沒有 | `triggers/` 子系統(`every`/`on_db_change`/`on_file_change`) | 沒有 | **antigravity**(獨家 P1) | antigravity only |
| **Connections(runtime 抽象)** | 沒有(綁 openai SDK) | `ConnectionStrategy` 三層 ABC | 沒有(綁 anthropic API) | **antigravity**(獨家 P1) | antigravity only |
| **Tool framework** | `FunctionTool` 八種子型 | `ToolContext` auto-injection + `tools/tool_runner.py` | 43 個 tool 各自 dir + `is_read_only / is_destructive` metadata + `ToolSearch / deferred tools` | **混合**:`FunctionTool`(openai-agents)+ tool metadata(claude-code §4.8)+ context injection(antigravity) | 三 SDK 都用 |
| **Slash command** | 無 | 無 | 自家 CLI(已 port 進 anila-agent/cli) | **claude-code**(已完成) | claude-code |
| **Skills(declarative agent feature)** | 無 | `google/antigravity/skills/`(可能對應 anthropic Skills 概念) | 無 | **antigravity** refer(若 anila 要做 Skills) | antigravity only |
| **Compaction** | `CompactingSession` decorator + `OpenAIResponsesCompactionSession`(綁雲端) | `Conversation` 內含 compaction_threshold | `microcompact / autoCompact / snip` 三層 | **claude-code 三層 strategy**(已部分 port,可補 microcompact)+ openai-agents 的 `CompactingSession` decorator pattern(自寫 LLM-summarize 版,不綁 OpenAI Responses) | claude-code + openai-agents decorator |
| **HITL(human-in-the-loop)** | `RunState.to_json / from_json` pause / resume + tool approval | 無 | 走 hook 阻擋 user prompt | **openai-agents** RunState(獨家) | openai-agents only |
| **Streaming** | `Runner.run_streamed` + 三層 `StreamEvent` | (Go binary 內含) | 走 IPC pipe | **openai-agents** | openai-agents only |
| **Retry policy** | `RetryPolicy` + `RetryDecision`(network_error / retry_after) | (Go binary 內含) | 無 | **openai-agents** | openai-agents only |

### 2.1 三 SDK 各自的「**獨家**」能力(其他 SDK 沒有對應)

| SDK | 獨家能力 | 對 anila-agent 戰略價值 |
|---|---|---|
| **openai-agents** | RunState HITL / Streaming / Retry policy / Tracing (OTel) | **runtime 內核必備**,沒有就無法做 multi-turn HITL / 即時回饋 / 可觀測 |
| **antigravity** | Triggers / Connections / Policy DSL priority bucket | **平台升級級的戰略 differentiator**,讓 sub-agent 從「對話」進化到「自動化」 |
| **claude-code** | memdir / forkSubagent prompt cache / Hook flavor(command/prompt/http)/ Tool metadata 屬性 | **harness 細節**,長期 production 用得到(prompt cache 對 vLLM 有效降 latency) |

### 2.2 三 SDK 都有的能力 → 看誰設計最好

| 能力 | 三 SDK 設計比較 | 推薦選誰 |
|---|---|---|
| Hook | openai-agents:**lifecycle phase**(on_agent_start 等)<br>antigravity:**強型別三類**(Inspect / Decide / Transform)<br>claude-code:**event-driven**(5 種 event + multi-flavor) | **三層混合**:lifecycle layer(openai-agents)+ semantic layer(antigravity 三類)+ flavor layer(claude-code event + 多種 hook 來源) |
| MCP | openai-agents:`MCPServer` ABC + lifecycle 整合<br>antigravity:`McpBridge` 三 transport<br>claude-code:有但分散 | **openai-agents**(API 清楚 + lifecycle bridge);antigravity 的 transport 配置可借鑑(尤其 SSE/HTTP timeout 細節) |
| Tool | 詳見 §2 表內最後一行 | **三 SDK 都抄,各取一塊**(FunctionTool + metadata + context injection) |
| Compaction | openai-agents:decorator pattern<br>antigravity:turn/compaction index<br>claude-code:三層 micro/auto/snip | **claude-code 三層 為主**(已部分 port);decorator pattern 套上去做 plug-in 介面 |

---

## 3. 跨 SDK 重複 / 衝突 — 解讀三份報告的差異

### 3.1 共識項(三份報告都點到 = 100% 該做)

| Pattern | openai-agents 提到 | antigravity 提到 | claude-code 提到 |
|---|:-:|:-:|:-:|
| Hook 系統不足 | ✅ §4 lifecycle | ✅ §4 三類 hook | ✅ §4.4 flavor + §4.13 event |
| Tool metadata 太瘦 | ✅ §4 ToolGuardrail | ✅ §4 ToolContext | ✅ §4.8 屬性擴充 |
| 缺 sub-agent dispatch | ✅ §4 handoffs | (antigravity 走 connections,本質不同) | ✅ §4.1 AgentTool |
| 缺可觀測(tracing/telemetry) | ✅ §4 tracing | ✅ §4 HookContext scope | ✅ §4.19 queryTracking |

→ **這 4 件事是 P0 第一輪必做**。

### 3.2 衝突項(兩份建議不同方向)

| 議題 | openai-agents 建議 | claude-code 建議 | 推薦 |
|---|---|---|---|
| **長期記憶用什麼** | `Session` Protocol(SDK 內含) | `memdir/` 4-type taxonomy | **memdir 已 port 完成**,維持(claude-code 對 LLM-recall 友善,SDK Session 適合短期 conversation,不衝突)|
| **Sub-agent 怎麼做** | `Handoff`(控制權轉移) | `AgentTool`(sub-routine 不轉移) | **兩條都做**:`Handoff` 給「換 agent 接手」場景、`AgentTool` 給「先讓另一個 agent 做完再回來」場景。實際使用一致(SDK 內 `Agent.as_tool` 是 `AgentTool` 的等效) |
| **Compaction 策略** | decorator + LLM-summarize | 三層 micro/auto/snip 規則 | **claude-code 三層** 為主(已 port 一部分),decorator pattern 套上去做 plug-in(可以同時有多個 compactor) |

### 3.3 互補項(各 SDK 補對方的洞)

| 缺口 | 由誰補上 |
|---|---|
| openai-agents 沒有 Triggers / Connections / Policy DSL | **antigravity 補**(P1 階段,5-7 d) |
| openai-agents 沒有 hook flavor 多元化 | **claude-code 補**(command/prompt/http hook,P0,3 d) |
| openai-agents 沒有 prompt-cache 友善的 sub-agent fork | **claude-code 補**(forkSubagent byte-identical prefix,P0,1 d) |
| claude-code 沒有 OTel tracing | **openai-agents 補**(tracing,P0,2-3 d) |
| claude-code 沒有 streaming SDK | **openai-agents 補**(`run_streamed`,P1,2-3 d) |
| antigravity 不能引依賴(鎖 Gemini) | **只抄設計**,所有實作從零自寫 |

---

## 4. 整合 P0 / P1 / P2 統一表

合併三份報告的 priority + 解依賴 + 去重複後的完整待辦。

### 4.1 P0 — 第一輪必做(8-12 d)

| # | 項目 | 來源報告 | 來自 SDK | 工作量 | 依賴 |
|---:|---|---|---|---:|---|
| P0-1 | **Lifecycle hooks 補滿**(on_agent_start / on_handoff / per-agent AgentHooks) | openai-agents §4 | openai-agents | 1d | — |
| P0-2 | **Tool metadata 擴充**(is_read_only / is_destructive / concurrency_safe / cost_estimate) | claude-code §4.8 | claude-code | 1d | — |
| P0-3 | **AnilaToolContext + FileStateCache**(workspace + state injection 基礎) | claude-code §4.5 + antigravity ToolContext | claude-code + antigravity | 2d | — |
| P0-4 | **Hook flavor 擴充**(command/prompt/http + decorator factory) | claude-code §4.4 + antigravity decorator | claude-code + antigravity | 3d | P0-1 |
| P0-5 | **Hook 三類強型別分類**(Inspect / Decide / Transform) | antigravity §4 | antigravity | 1-2d | P0-1 |
| P0-6 | **Guardrails framework**(InputGuardrail + OutputGuardrail + tool 三 behavior + tripwire) | openai-agents §4 + antigravity §4 policy | openai-agents + antigravity | 1-2d | P0-1 |
| P0-7 | **Policy DSL 基礎**(allow/deny + priority bucket + workspace_only) | antigravity §4 | antigravity | 2-3d | P0-6 |
| P0-8 | **AgentTool / sub-agent dispatch**(基本版,搭 openai-agents handoff) | claude-code §4.1 + openai-agents §4 | claude-code + openai-agents | 2-3d | P0-3, P0-2 |
| P0-9 | **Tracing framework**(Trace + Span + JSONL processor + 在 hook bridge fire) | openai-agents §4 | openai-agents | 2-3d | P0-1 |
| | **— P0 小計** | | | **15-21d** | (用平行可壓到 **8-12 d**) |

> 平行化機會:P0-1/2/3 可同時做(無依賴);P0-4/5/6 在 P0-1 完成後可平行;P0-7/8/9 在前面完成後可平行。

### 4.2 P1 — 中期重構(14-22 d)

| # | 項目 | 來源 | 工作量 | 依賴 |
|---:|---|---|---:|---|
| P1-1 | **forkSubagent byte-identical prefix**(prompt cache 命中) | claude-code §4.2 | 1d | P0-8 |
| P1-2 | **runTools concurrency partition**(throughput 提升) | claude-code §4.3 | 2d | P0-2, P0-3 |
| P1-3 | **HookContext 三層 scope**(Session / Turn / Operation) | antigravity §4 | 1d | P0-5 |
| P1-4 | **Triggers 子系統**(every / on_db_change / on_file_change) | antigravity §4 | 3-5d | P0-5, P1-3 |
| P1-5 | **MCP 整合**(MCPServerManager + lifecycle + 2 yaml example) | openai-agents §4 | 2-3d | P0-1 |
| P1-6 | **CompactingSession decorator + microcompact 補完** | openai-agents §4 + claude-code §4.12 | 2-3d | — |
| P1-7 | **Streaming(`run_streamed` + 三層 StreamEvent + CLI renderer)** | openai-agents §4 | 2-3d | P0-1 |
| P1-8 | **RunState HITL pause/resume + tool approval** | openai-agents §4 | 1w+ | P0-1, P0-9 |
| P1-9 | **Token budget continuation** | claude-code §4.6 | 0.5d | — |
| P1-10 | **systemContext / userContext 兩段 prompt 結構** | claude-code §4.7 | 1d | P0-3 |
| P1-11 | **ConnectionStrategy ABC + 第二 backend(raw HTTP runtime)** | antigravity §4 | 1w+(1-2d ABC + 5d 第二 backend) | — |
| P1-12 | **McpBridge transport 配置擴充**(看 antigravity 的 SSE/HTTP timeout 設計) | antigravity §4 | 1-2d | P1-5 |
| P1-13 | **coordinatorMode XML notification 機制** | claude-code §4.11 | 1.5d | P0-8, P1-1 |
| P1-14 | **Stop hook prevent-continuation** | claude-code §4.10 | 1d | P0-4 |
| P1-15 | **Cost tracker + USD pricing** | claude-code §4.14 | 1d | P0-3 |
| P1-16 | **Permission rule grammar** | claude-code §4.15 | 1.5d | — |
| P1-17 | **Hook event 擴充**(SubagentStart / SubagentEnd 等 lifecycle 細節) | claude-code §4.13 | 1d | P0-4 |
| P1-18 | **ToolSearch / deferred tools** | claude-code §4.9 | 2-3d | P0-2 |
| P1-19 | **`disable` vs `deny` 二維工具控管(純 flag + 文件)** | antigravity §4 | 0.5d | P0-7 |
| | **— P1 小計** | | **22-34d** | (平行可壓到 **14-22 d**) |

### 4.3 P2 — 評估後再做(20-30 d)

| # | 項目 | 來源 | 工作量 | 依賴 |
|---:|---|---|---:|---|
| P2-1 | `Agent.as_tool` sub-routine 寫法(跟 P0-8 並列文件) | openai-agents | 0.5d | P0-8 |
| P2-2 | `RetryPolicy` framework | openai-agents | 1d | — |
| P2-3 | `prompt_with_handoff_instructions` extension | openai-agents | 0.5d | P0-8 |
| P2-4 | `FunctionTool` 直接掛 tool_guardrails(取代全局 regex) | openai-agents | 1d | P0-6 |
| P2-5 | `Conversation` turn / compaction index 追蹤(可選) | antigravity | 2-3d | — |
| P2-6 | SessionMemory + autoDream | claude-code §4.16 | 3d | P0-8, P1-1 |
| P2-7 | file-index fuzzy file search | claude-code §4.17 | 1d | P0-2 |
| P2-8 | policyLimits 組織政策 | claude-code §4.18 | 2d | P0-7 |
| P2-9 | queryTracking chainId/depth | claude-code §4.19 | 0.5d | P0-9 |
| P2-10 | Slash command 漸進補 10 個 | claude-code §4.20 | ~5d | 各 feature |
| P2-11 | Skill loader frontmatter | claude-code §4.21 + antigravity skills/ | 2-3d | P0-2 |
| P2-12 | Task / background task tool | claude-code §4.22 | 3-5d | P0-8, P0-3 |
| P2-13 | `draw_graph` visualization extension | openai-agents | 0.5d | P0-8 |
| P2-14 | `repl.run_demo_loop` streaming CLI 對齊 | openai-agents | 0.5d | P1-7 |
| P2-15 | claude-code §4.23 9 個 misc pattern | claude-code | ~10d | 各自獨立 |
| | **— P2 小計** | | **30-40d** | (平行可壓到 **20-30 d**) |

### 4.4 總計

| 階段 | 工作量(序列) | 工作量(平行) | 累計(平行)|
|---|---:|---:|---:|
| P0 | 15-21 d | **8-12 d** | 8-12 d |
| P1 | 22-34 d | **14-22 d** | 22-34 d |
| P2 | 30-40 d | **20-30 d** | 42-64 d |
| **總計** | **67-95 d** | **42-64 d** | **約 10-13 週 1 人 full-time** |

---

## 5. Master Phase Plan — 建議 sprint 排程

### 5.1 整體分 5 個 sprint(2 週/sprint)

```
┌─ Sprint 1 (P0-A, 2 週) ──────────────────────────────────────┐
│  目標:hook + tool + tracing 三層基礎拉起                       │
│  ├── [1d] P0-1  Lifecycle hooks 補滿                          │
│  ├── [1d] P0-2  Tool metadata 擴充     ◀── 平行              │
│  ├── [2d] P0-3  AnilaToolContext        ◀── 平行              │
│  ├── [3d] P0-4  Hook flavor 擴充                              │
│  ├── [1-2d] P0-5 Hook 三類強型別      ◀── 平行 (與 P0-4 不同檔) │
│  ├── [1-2d] P0-6 Guardrails framework                         │
│  ├── [2-3d] P0-9 Tracing framework                            │
│  Deliverable:閉環 1 = debug 友善 / audit 完整 / tool surface 標準化│
└──────────────────────────────────────────────────────────────┘

┌─ Sprint 2 (P0-B, 2 週) ──────────────────────────────────────┐
│  目標:Policy + sub-agent dispatch 上線                         │
│  ├── [2-3d] P0-7  Policy DSL 基礎                             │
│  ├── [2-3d] P0-8  AgentTool dispatch                          │
│  ├── [1d]   P1-1  forkSubagent prefix(立刻接 P0-8)            │
│  ├── [1d]   P1-19 disable vs deny flag                        │
│  ├── [2d]   P1-2  runTools concurrency partition              │
│  Deliverable:閉環 2 = multi-agent 上線 + 沙箱政策 + throughput 提升│
└──────────────────────────────────────────────────────────────┘

┌─ Sprint 3 (P1-A, 2 週) ──────────────────────────────────────┐
│  目標:Triggers + MCP 上線 + 中期擴充                          │
│  ├── [1d]   P1-3   HookContext 三層 scope                     │
│  ├── [3-5d] P1-4   Triggers 子系統(every/on_*)                 │
│  ├── [2-3d] P1-5   MCP 整合                                   │
│  ├── [1-2d] P1-12  McpBridge transport 細節                   │
│  Deliverable:閉環 3 = 自動化背景任務 + MCP server 接入            │
└──────────────────────────────────────────────────────────────┘

┌─ Sprint 4 (P1-B, 2 週) ──────────────────────────────────────┐
│  目標:Streaming + HITL + compaction 補完                       │
│  ├── [2-3d] P1-6  CompactingSession + microcompact            │
│  ├── [2-3d] P1-7  Streaming                                   │
│  ├── [1w+]  P1-8  RunState HITL (大票,可拆到 Sprint 5)         │
│  ├── [1d]   P1-9  Token budget continuation     ◀── 平行       │
│  ├── [1d]   P1-10 systemContext/userContext      ◀── 平行       │
│  Deliverable:閉環 4 = 即時 UX + 可中斷恢復 + 自動 compaction     │
└──────────────────────────────────────────────────────────────┘

┌─ Sprint 5 (P1-C, 2 週) ──────────────────────────────────────┐
│  目標:Connections + 細節打磨,P1 結尾                          │
│  ├── [1-2d] ConnectionStrategy ABC (P1-11 第一段)              │
│  ├── [5d]   ConnectionStrategy 第二 backend (P1-11 第二段)     │
│  ├── [1.5d] P1-13 coordinatorMode XML notification             │
│  ├── [1d]   P1-14 Stop hook prevent-continuation               │
│  ├── [1d]   P1-15 Cost tracker                                 │
│  ├── [1.5d] P1-16 Permission rule grammar       ◀── 平行       │
│  ├── [1d]   P1-17 Hook event 擴充                ◀── 平行       │
│  ├── [2-3d] P1-18 ToolSearch / deferred tools                  │
│  Deliverable:閉環 5 = runtime 可換 backend + 全 production 級 harness│
└──────────────────────────────────────────────────────────────┘

  Sprint 6+ (P2,可選擴充): SessionMemory / Slash command / Task tool / Skills / 其他
```

### 5.2 依賴關係圖

```
                        ┌───────┐
                        │ P0-1  │  Lifecycle hooks
                        │ P0-2  │  Tool metadata          (3 個 P0 基底,平行)
                        │ P0-3  │  ToolContext
                        └───┬───┘
                            │
              ┌─────────────┴────────────────────────┐
              ▼                                       ▼
         ┌─────────┐   ┌────────┐   ┌────────┐   ┌────────┐   ┌────────┐
         │  P0-4   │   │ P0-5   │   │ P0-6   │   │ P0-9   │   │ P0-8   │
         │ flavor  │   │ 三類   │   │guardrail│   │tracing │   │AgentTool│
         └────┬────┘   └───┬────┘   └────┬────┘   └────────┘   └───┬────┘
              │            │             │                          │
              ▼            ▼             ▼                          ▼
         ┌────────┐   ┌────────┐   ┌────────┐                  ┌────────┐
         │  P1-14 │   │  P1-3  │   │  P0-7  │                  │  P1-1  │
         │stop hk │   │ ctx 3  │   │Policy  │                  │ fork   │
         └────────┘   └───┬────┘   │  DSL   │                  │ prefix │
                          │        └────────┘                  └───┬────┘
                          ▼                                        │
                     ┌────────┐                                    ▼
                     │  P1-4  │                               ┌────────┐
                     │triggers│                               │ P1-13  │
                     └────────┘                               │coordnt │
                                                              │  XML   │
                                  ┌────────┐                  └────────┘
                                  │  P1-5  │                       │
                                  │  MCP   │                       ▼
                                  └────────┘                  ┌────────┐
                                                              │  P2-6  │
                                                              │SessMem │
                                                              └────────┘

  獨立可平行:P1-2 (concurrency) / P1-6 (compaction) / P1-7 (streaming) /
            P1-8 (HITL) / P1-9 (token budget) / P1-10 (context split) /
            P1-11 (ConnectionStrategy) / P1-15 (cost) / P1-16 (permission)
```

### 5.3 獨立可 ship 的最小單位

如果不想跑完整個 5 sprint,**以下任一單獨 ship 都對 anila-agent 有顯著加分**:

| 單位 | 包含 | 工作量 | 影響 |
|---|---|---:|---|
| **🚀 quickstart** | P0-1 + P0-9 | 3-4 d | 第一個閉環:debug 友善 + audit 完整 |
| **🛡️ safety** | P0-2 + P0-6 + P0-7 | 4-6 d | input 防線 + tool-level reject + workspace 限制 |
| **🤝 multi-agent** | P0-3 + P0-8 + P1-1 | 4-6 d | sub-agent 上線 + prompt cache 命中 |
| **⚡ automation** | P0-5 + P1-3 + P1-4 | 5-8 d | trigger 自動化 = 平台 differentiator |
| **🔧 runtime swap** | P1-11 | 6-7 d | 解綁 openai-agents,可換 backend |

---

## 6. 對映現有 anila-agent backlog + 既有 docs

### 6.1 anila-agent README 內既有 backlog 對照

| README 既有條目 | 對應本報告 |
|---|---|
| P0:多 agent handoff(retrieve → answer → cite-verify) | **P0-8** AgentTool + openai-agents Handoff |
| P0:Tracing framework | **P0-9** |
| P0:Tool guardrails | **P0-6** |
| P0:PTL retry | claude-code §4.6 → **P1-9 token budget continuation** |
| P0:`stripImagesFromMessages` for compact | (合進 **P1-6** microcompact) |
| P0:`preventContinuation` stop hook | **P1-14** |
| P1:Session 抽象 | (claude-code §4.16 + openai-agents Session) → **P2-6** |
| P1:MCP server | **P1-5** |
| P1:AgentTool fork prefix | **P1-1** |
| P1:Lifecycle hooks | **P0-1** |
| P2:Magic Docs / PromptSuggestion / AwaySummary / Realtime | (Realtime 不做;PromptSuggestion 在 claude-code §4.23 小 pattern 群) |

→ README 既有 P0 = 7 個,本報告**全部覆蓋** + 補了 antigravity 帶來的 2 個 P0(三類 hook + policy DSL)。

### 6.2 既有 docs 對映

- `runtime-logic-openai-agents-deep-dive.md`(1339 行):anila-core + AgenticRAG 視角,**跟本報告不衝突**;本報告聚焦 anila-agent template 角度,內容互補
- `anila-agent-framework-architecture.md`:anila-agent v0.1 設計(5 個 primitive)— 本報告 §1.2 指出「實作只走完 Memory + Hook 兩個,剩 3 個 primitive 對應 P0-8 / P0-3 / P1-11 等項目」
- `anila-agent-framework-porting-decisions.md`:已 SUPERSEDED,但 claude-code 報告 §3 audit 跟它對齊

---

## 7. 風險 + 跟 ANILA 平台戰略對齊

### 7.1 工程風險

| 風險 | 來源 | 緩解 |
|---|---|---|
| **openai-agents SDK 內部 API 變動**(`run_internal/*`) | openai-agents §4 | 報告已標「不該抄 hidden API」,只用 public `from agents import ...` |
| **prompt cache 在 vLLM 命中率未驗** | claude-code §4.2 forkSubagent | **P1-1 上線時必跑 benchmark**(報告 §8.3 已列) |
| **Triggers 引入後 session lifecycle 變複雜** | antigravity §4 | **P1-4 必加完整 test**(模擬 trigger fire 失敗 / race condition) |
| **ConnectionStrategy 第二 backend 工作量爆炸** | antigravity §4 | **P1-11 拆兩段**:先 ABC(1-2d)再第二 backend(5d),中間 review 是否真要做 |
| **HITL `RunState` 序列化複雜度** | openai-agents §4 | **P1-8 拆兩個 sprint**(序列化 + 反序列化 + Redis 整合) |

### 7.2 跟 ANILA 平台戰略對齊

| ANILA 平台場景 | 用到的 enhancement | 階段 |
|---|---|---|
| **Studio 簡報生成 sub-agent**(已有) | P0-8 AgentTool / P1-7 Streaming / P1-15 Cost tracker | Sprint 2-4 |
| **KB 變動自動 reindex 通知** | P1-4 Triggers (`on_db_change`) | Sprint 3 |
| **ComfyUI job 完成自動續推** | P1-4 Triggers (`on_file_change` or polling)| Sprint 3 |
| **內網 ML 模型 health check** | P1-4 Triggers (`every(30, ...)`)| Sprint 3 |
| **多 agent 協作 RAG**(retriever → answer → fact-check) | P0-8 + P1-13 coordinatorMode | Sprint 2 / 5 |
| **anila-studio 切到 vLLM raw HTTP**(去除 openai-agents 依賴) | P1-11 ConnectionStrategy | Sprint 5 |
| **5 branch sync 出 sub-agent 各客戶版**(prod-intranet-card / prod-public-passwd …) | P0-7 Policy DSL(讓不同 branch 有不同 policy yaml) | Sprint 2 |
| **AI 治理(ISO/IEC 42001)合規** | P0-9 Tracing(audit log)+ P0-6 Guardrails(decision log)+ P1-15 Cost tracker | Sprint 1 / 5 |
| **內網部署 dev tooling** | P1-7 Streaming(REPL UX)+ P2-10 Slash command 漸進補 | Sprint 4 + Sprint 6+ |

→ Sprint 1-3 的產出 **直接解了 ANILA 平台 3 個 use case**(KB reindex / job 續推 / health check)+ 鋪了 AI 治理 audit log 基底。

---

## 8. 行動建議

### 8.1 立即可做(本週,不需審批)

1. **Read 三份 deep-dive 報告完整內容**:對應 §4 統一表內每個項目都有對應檔/行號可深讀,不要只看 summary。
2. **建一個 GitHub project board**(`anila-agent v0.3-v1 roadmap`):把 §4 內 50 個項目 import 進去,每個項目對應一個 issue(可以直接用本報告 P0-N / P1-N 編號)。
3. **挑 Sprint 1 的 ship 單位**:推薦從 **🚀 quickstart (P0-1 + P0-9, 3-4 d)** 開始,3-4 天就能 ship 第一個閉環。

### 8.2 需要決策(列給你拍板)

| 決策點 | 選項 | 推薦 |
|---|---|---|
| **memory 是否還要動** | A. 維持 memdir 不動 / B. 補 antigravity Conversation index | **A**(已穩定) |
| **sub-agent dispatch 兩條都做 or 擇一** | A. 只做 Handoff / B. 只做 AgentTool / C. 兩條都做 | **C**(用途不同,Handoff 轉移控制 vs AgentTool sub-routine 等回值) |
| **HITL `RunState` 優先級** | A. P1 / B. 提到 P0 / C. 延後 P2 | **A**(P1,但獨立 spike,可拆成兩個 sprint 做) |
| **ConnectionStrategy 真的要做嗎** | A. 做完整(P1-11) / B. 只做 ABC(2 d 不做第二 backend) / C. 完全不做 | **B**(留架構彈性,但不投資第二 backend 直到實際需要) |
| **antigravity Triggers** | A. P1 列入 / B. P2 / C. 不做 | **A**(平台 differentiator,KB reindex / job 續推 兩個 use case 立刻有用) |

### 8.3 不該做(明確列入 anti-roadmap)

| 不做 | 原因 |
|---|---|
| `realtime/` / `voice/`(openai-agents) | anila-agent template 不是聲控介面 |
| `sandbox/` 全套 manifest(openai-agents) | 等真要做 untrusted exec 再評估;ANILA 內部用都是 trusted code |
| Go binary / WebSocket / protobuf(antigravity) | 鎖 Gemini + 不利 on-prem,徒增複雜 |
| `GeminiConfig` / `BuiltinTools` enum(antigravity) | 對應 Go 端內建工具,Python 端沒對應 |
| 終端 UI:`ink/` / `components/` / `screens/` / `buddy/`(claude-code) | anila-agent 是服務化的 sub-agent,不是 CLI |
| voice / keybindings(claude-code) | 同上 |
| `OpenAIResponsesCompactionSession`(openai-agents) | 綁 OpenAI 雲端 API,改用自寫 LLM-summarize 版即可 |
| 任何「綁特定雲端 LLM」的 SDK feature | ANILA 全部 on-prem |

---

## 9. 結語

三 SDK 各自 1100-1700 行的 deep-dive 報告匯流到一個結論:

> **anila-agent template 的 Python harness 已經是 ANILA 平台珍貴的 substrate,但它對「runtime feature 表面」、「harness primitive 完整度」、「architecture pattern 抽象層」三個維度都還有 6-13 週 1 人 full-time 的 enhancement 工作。**
>
> P0 8-12 d 是「不做就阻礙日常開發」(hook / guardrail / tracing / tool metadata / sub-agent dispatch);P1 14-22 d 是「做了立刻在 ANILA 平台 use case 看到回饋」(Triggers / MCP / Streaming / HITL);P2 20-30 d 是「長期 production-grade polish」。
>
> **下一步**:本報告 §8.1 建議從 🚀 quickstart(P0-1 + P0-9,3-4 d)開始。

三份 deep-dive 對應的 source code 也都在 `anila-agent/templete/{openai-agents-python,antigravity-sdk-python,claude-code-src}/` 內(gitignored,本地解壓),工程師可以隨時 grep / read,不需要再重新 clone。

---

**Last updated**: 2026-05-26 · **作者**: ANILA 平台分析(整合三份 subagent 報告)· **下次更新觸發**:Sprint 1 落地後回填實際工作量 / 任一 SDK upstream 大版本更新時重做對應 deep dive
