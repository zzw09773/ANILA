# runtime_logic — agent runtime reference codebases

> **定位**：本目錄收兩份**生產級 agent runtime 的原始碼快照**，作為「設計參考」。它**不是 ANILA 的執行碼**（執行碼在 `anila-core/` Python tree、agent template 在 `AgenticRAG/`），而是用來**對照、借鑑、把好的設計模式翻譯成 Python 後納入 anila-core / AgenticRAG**。
>
> **下一波重點目標**：用這兩份 reference 強化 [`AgenticRAG`](../AgenticRAG/) 的 agent runtime 能力 — multi-agent handoffs、guardrails、tracing、sandbox tool execution、session memory 等等 — 都已經在 reference codebase 內有成熟實作，可以照著 pattern 翻譯。
>
> ⚠️ **`claude-code-src/` 與 `openai-agents-python/` 的整個 source tree 已由 `.gitignore` 排除**，不會進 repo。只有本 README 被追蹤。原始碼在本機維護，作者自行管理副本。
>
> 規範：**讀 pattern、學介面、自己重寫**；不可逐字複製。授權見各 codebase 自己的 LICENSE。

---

## 為什麼要保留兩份 reference

不同 codebase 暴露不同 design surface。各自的長處：

| Reference | 長處 | 對應 ANILA 痛點 |
|---|---|---|
| **`claude-code-src/`**（TypeScript Claude Code CLI） | 長期單一 conversation 的 turn-loop 細節（compact 三層、PTL retry、background memory extraction、stop hooks、tool-as-folder UI/prompt 分離） | 我們已經從這裡移植 7-stage QueryEngine、coordinator、compact 三層、SessionMemory、Memdir 4-type 等到 `anila-core/`；尚有 **PTL retry、preventContinuation hook、prompt-cache fork prefix** 等待移植 |
| **`openai-agents-python/`**（OpenAI 官方 Agents SDK） | 多 agent 編排（handoffs / agents-as-tools / sub-agent state）、tool guardrails、structured tracing、sandbox tool execution、conversation session abstraction、retry semantics、MCP server 整合 | AgenticRAG 目前**單 agent + 線性 tool loop**；要往「多 agent handoff RAG」（檢索 agent → 生成 agent → 引用驗證 agent）、「sandboxed tool 執行」、「結構化 tracing」走，這份 reference 是現成藍圖 |

兩份合起來涵蓋了「單 agent 深度 」+「多 agent 廣度」兩個維度。

---

## 兩份 reference 的目錄概覽

> 完整檔案結構保留在本機 `claude-code-src/src/`、`openai-agents-python/src/agents/`，以下只列出最常被引用的子模組以便對應地圖閱讀。

### `claude-code-src/`（TypeScript）

```
claude-code-src/
├── README.md, LICENSE
├── docs/                            ← Claude Code 官方文件（離線快照）
└── src/                             ← 主要源碼樹（gitignored）
    ├── QueryEngine.ts               ← 7-stage turn loop 入口
    ├── Task.ts, Tool.ts, tools.ts   ← Task type union + Tool 契約 + registry
    ├── query/
    │   ├── config.ts                ← QueryConfig immutable snapshot
    │   ├── deps.ts                  ← QueryDeps DI（callModel / compact / uuid）
    │   ├── stopHooks.ts             ← AsyncGenerator-based stop hook chain
    │   └── tokenBudget.ts           ← BudgetTracker + diminishing returns
    ├── tools/                       ← 43 個內建 tool（每個是 1 個資料夾）
    │   ├── AgentTool/               ← subagent fork + byte-identical prefix
    │   ├── BashTool/, GrepTool/, FileEditTool/, …
    │   └── REPLTool/, RemoteTriggerTool/, ScheduleCronTool/
    ├── services/
    │   ├── compact/                 ← micro / auto / sessionMemory / time-based
    │   ├── extractMemories/         ← background memory extraction + cursor
    │   ├── SessionMemory/           ← conversation summary maintenance
    │   ├── AgentSummary/            ← subagent completion summary
    │   ├── awaySummary.ts           ← "what changed while you were away"
    │   ├── MagicDocs/               ← magic doc generation
    │   └── PromptSuggestion/        ← inline prompt suggestion engine
    ├── memdir/                      ← MEMORY.md + 4-type taxonomy + relevance
    ├── coordinator/                 ← multi-worker orchestration prompt
    ├── hooks/toolPermission/        ← 5-source PermissionContext
    └── ink/, components/, screens/  ← terminal UI（**不要移植**）
```

### `openai-agents-python/`（Python）

```
openai-agents-python/
├── README.md, LICENSE, AGENTS.md, CLAUDE.md, PLANS.md
├── pyproject.toml, mkdocs.yml
├── docs/, examples/, tests/
└── src/agents/                      ← 主要源碼樹（gitignored）
    ├── agent.py, _public_agent.py   ← Agent 抽象 + tool / handoff 配置
    ├── run.py, run_state.py         ← Run loop + persistent state
    ├── run_internal/                ← Run loop 細節（hidden API）
    ├── retry.py, run_error_handlers ← Retry + error policies
    ├── lifecycle.py                 ← AgentHooks (on_start / on_tool / on_end)
    ├── stream_events.py             ← Event-stream iteration
    ├── tool.py, tool_context.py     ← Tool 契約
    ├── tool_guardrails.py           ← Tool-level safety check pipeline
    ├── guardrail.py                 ← Input/output guardrail framework
    ├── handoffs/                    ← Multi-agent handoff（控制權移轉）
    ├── extensions/handoff_filters.py← 標準 handoff filter 範本
    ├── memory/
    │   ├── session.py               ← Session abstraction
    │   ├── sqlite_session.py        ← Sqlite 持久化
    │   ├── openai_responses_compaction_session.py ← 自動 compact
    │   ├── openai_conversations_session.py        ← OpenAI Conversations API 後端
    │   └── session_settings.py      ← Session config
    ├── models/                      ← Provider abstraction + retry
    │   ├── interface.py             ← Provider Protocol
    │   ├── openai_chatcompletions.py / openai_responses.py
    │   ├── _openai_retry.py         ← 429 / 5xx retry policy
    │   └── multi_provider.py
    ├── mcp/                         ← MCP server 整合（tools / prompts / resources）
    │   ├── manager.py, server.py, util.py
    ├── tracing/                     ← 結構化 tracing
    │   ├── create.py, processor_interface.py, processors.py
    │   └── provider.py
    ├── sandbox/                     ← Tool sandboxed execution
    │   ├── apply_patch.py, files.py
    │   ├── capabilities/, entries/, instructions/, manifest.py
    ├── realtime/                    ← Voice / realtime agents（多半 RAG 不需要）
    └── extensions/experimental/     ← 預覽功能
```

---

## AgenticRAG 強化對應地圖

> 這是本目錄存在的核心理由：每當 AgenticRAG 要長新能力，從這個表查「哪份 reference 的哪個檔有現成 pattern」。

### 已落地的能力（AgenticRAG / anila-core 已實作）

| 能力 | 來源 reference | 目前位置 |
|---|---|---|
| 7-stage turn loop | `claude-code-src/src/QueryEngine.ts` + `query/config.ts` | `anila-core/src/anila_core/engine/query_engine.py` |
| BudgetTracker + diminishing returns | `claude-code-src/src/query/tokenBudget.ts` | `anila-core/src/anila_core/engine/budget_tracker.py` |
| AgentContext fork (subagent isolation) | `claude-code-src/src/tools/AgentTool/runAgent.ts` + ts AsyncLocalStorage | `anila-core/src/anila_core/context/agent_context.py` |
| ExtractMemories + cursor + trailing-run | `claude-code-src/src/services/extractMemories/` | `anila-core/src/anila_core/memory/extract_memories.py` |
| AutoCompact + buffer reservation | `claude-code-src/src/services/compact/autoCompact.ts` | `anila-core/src/anila_core/compact/auto_compact.py` |
| MicroCompact (per-turn output trim) | `claude-code-src/src/services/compact/microCompact.ts` | `anila-core/src/anila_core/compact/micro_compact.py` |
| SessionMemory | `claude-code-src/src/services/SessionMemory/` | `anila-core/src/anila_core/compact/session_memory.py` |
| Memdir 4-type taxonomy | `claude-code-src/src/memdir/memoryTypes.ts` | `anila-core/src/anila_core/memory/memdir.py` |
| Coordinator XML notifications | `claude-code-src/src/coordinator/coordinatorMode.ts` | `anila-core/src/anila_core/coordinator/coordinator.py` |
| Tool-driven RAG loop（vector / keyword / read_document） | original AgenticRAG（沒從 reference 來） | `AgenticRAG/src/agentic_rag/` |
| Sliding-window compact (Layer 3 hard truncation) | original AgenticRAG | `AgenticRAG/src/agentic_rag/compact/sliding_window.py` |
| Hierarchical chunking + parent-child（Sprint 9 X）| original ANILA | `anila-core/src/anila_core/ingestion/chunking_plugins/builtins.py:HierarchicalChunker` |

### 強化 backlog（priority 順序）

> 每條都是 AgenticRAG 即將要長 / 該長但還沒長的能力。標 ⭐ 為高 ROI 候選。

#### 🟢 P0 — 強烈推薦，高 ROI、低改動

| # | 能力 | reference 位置 | 為什麼值得 | 預估工作量 |
|---|---|---|---|---|
| 1 ⭐ | **Multi-agent handoff** — 從 single-agent RAG → 「retrieve agent → answer agent → cite-verify agent」pipeline | `openai-agents-python/src/agents/handoffs/` + `extensions/handoff_filters.py` | RAG 品質的下一個躍升點：retrieval / 生成 / 驗證分工。OpenAI SDK 的 handoff 介面已經 production-ready，pattern 直接移植 | 3–5 天 |
| 2 ⭐ | **Tracing 框架** — agent run / tool call / handoff 的結構化 tracing | `openai-agents-python/src/agents/tracing/` | 目前 AgenticRAG 跟 anila-core 都只有 `_post_turn_hooks`，沒有結構化 trace。對 debug RAG quality issue（哪個 chunk 命中、為什麼選這個 strategy）超有幫助 | 2 天 |
| 3 ⭐ | **Tool guardrails** — tool 呼叫前的 safety / validation pipeline | `openai-agents-python/src/agents/tool_guardrails.py` + `guardrail.py` | RAG agent 對外暴露 search tool 時，guardrail 可以擋 PII / SQL-injection / over-broad query。有現成 pattern 不用自己想 | 1.5 天 |
| 4 | **PTL (Prompt Too Long) retry**（已 flagged 在 anila-core README） | `claude-code-src/src/services/compact/compact.ts:truncateHeadForPTLRetry + MAX_PTL_RETRIES=3` | compact 自己撞到 token limit 時的 graceful degrade。沒做的話 long conversation 會 fail-stop | 0.5 天 |
| 5 | **`stripImagesFromMessages` for compact** | `claude-code-src/src/services/compact/compact.ts` | 送 compact request 前移除 image block；image tokens 特別肥，且 compact summary 不需要 | 0.3 天 |
| 6 | **`preventContinuation` stop hook semantic** | `claude-code-src/src/query/stopHooks.ts` | post-turn hook 目前在 `anila-core` 只能 log；不能阻止下一輪 budget nudge 繼續。補進 `engine/query_engine.py` | 0.5 天 |

#### 🟡 P1 — 中等優先，有清楚動機就做

| # | 能力 | reference 位置 | 為什麼 | 預估 |
|---|---|---|---|---|
| 7 | **Session abstraction**（conversation persistence with auto-compact） | `openai-agents-python/src/agents/memory/session.py` + `sqlite_session.py` + `openai_responses_compaction_session.py` | AgenticRAG 目前 session storage 散在多處；統一成 Session Protocol 後可以接 SQLite / Postgres / Redis 多 backend | 2 天 |
| 8 | **MCP server 整合作為 tool source** | `openai-agents-python/src/agents/mcp/` + `claude-code-src/src/services/mcp/` + `tools/MCPTool/` | 讓 AgenticRAG 能消費外部 MCP tool（例如 Slack / Jira / GitHub MCP server），不用為每個整合自己刻 | 2 天 |
| 9 | **AgentTool fork — byte-identical prefix** | `claude-code-src/src/tools/AgentTool/forkSubagent.ts` | 多 worker 共享 prompt-cache 的關鍵；`coordinator.py` 目前直接 fork context，cache 沒共享，每個 worker 都重算 prefix → 浪費 30–50% provider 成本 | 1 天 |
| 10 | **Lifecycle hooks** (`on_start` / `on_tool_start` / `on_handoff` / `on_end`) | `openai-agents-python/src/agents/lifecycle.py` | observability + plugin point；很多 enterprise需求（audit / quota / rate-limit）走 lifecycle hook 最乾淨 | 1.5 天 |
| 11 | **Tool sandboxing**（apply patch / file ops in sandbox） | `openai-agents-python/src/agents/sandbox/` | RAG agent 如果要長「執行 user-provided code」這類能力，sandbox 是必須。短期不需要，但設計地基要先讀 | 3 天 (僅讀 + 寫 design doc) |
| 12 | **Stream-event semantics** | `openai-agents-python/src/agents/stream_events.py` + `claude-code-src/src/api/streamProcessor.ts` | 統一 Tool call / message_delta / handoff / final 的 event 命名與 schema，方便前端 / tracing / replay 共用同一份 event 模型 | 2 天 |

#### 🔵 P2 — 看實際需求再評估

| # | 能力 | reference 位置 | 注意 |
|---|---|---|---|
| 13 | Magic Docs（auto-generate doc from code） | `claude-code-src/src/services/MagicDocs/` | 跟 AgenticRAG core mission 距離稍遠；可能更適合作為獨立 agent template |
| 14 | PromptSuggestion（inline 提示） | `claude-code-src/src/services/PromptSuggestion/` | UI-driven，比較像 anila-ui 的事 |
| 15 | AwaySummary | `claude-code-src/src/services/awaySummary.ts` | 對長期 conversation 有用；短 RAG 互動沒差 |
| 16 | Realtime / voice | `openai-agents-python/src/agents/realtime/` | 多半超出 RAG 範疇；如果未來做 voice-RAG 再來 |

#### ⚫ 明確不適合移植

- `claude-code-src/src/ink/`、`components/`、`screens/`、`buddy/`、`voice/` — terminal UI / CLI bound，跟 ANILA 後端服務不對齊
- `openai-agents-python/src/agents/realtime/` — voice / WebSocket 路徑，本案非目標
- 各種 `keybindings/` — keyboard 快捷鍵 binding

---

## 工作流：當 AgenticRAG 要新長一個能力

### Step 1 — 先到對應地圖找

「我們要加 X」→ 翻上面的 backlog 表 → 找到對應 reference 模組。

### Step 2 — 讀 reference 的 contract，不讀 implementation

看：
- 函式簽名、type / class shape（API 表面）
- 配置選項（哪些東西該被外露）
- 錯誤處理 / edge case（commit message、test 名字常常有 hint）
- 跟其他模組的依賴關係

不要：
- 逐字翻譯邏輯（變相 derivative work）
- 把 reference 的 internal helper 一起搬過來

### Step 3 — 設計 ANILA 自己的 implementation

問自己：
- 這個 pattern 在 ANILA 的 context 下需要哪些調整？（pgvector vs in-memory store / 多租戶 / 中文 corpus / on-prem 約束）
- 有沒有更簡單的形狀？（reference 為了通用性常 over-design，我們可以更貼合）
- API surface 有沒有跟既有模組對齊？

### Step 4 — 寫 anila-core / AgenticRAG 的版本

純 Python，自己重寫，自己取名字，自己寫 docstring。注釋裡可以引用 reference 路徑作為 design provenance（譬如 `# Pattern from runtime_logic/openai-agents-python/src/agents/handoffs/`），但不引用任何具體 code。

### Step 5 — 跨 reference verify

如果兩份 reference 都有對應 module（譬如 compact / memory），看它們**怎麼處理同一個問題**。差異點通常是真正的 design decision 所在。

---

## 對應地圖之外 — 兩份 reference 該讀的「軟體工程養分」

### claude-code-src

- **commit history 寫得極佳**（內含 docs.zip 是離線快照不含 commits，但仍可從 src 看 hint）
- **tool-as-folder pattern** — 每個 tool 自帶 prompt / UI / logic / constants 的分檔哲學，照搬到 Python 就是一個 package per tool
- **`ToolUseContext` 的 50+ 欄位** 跟 `DeepImmutable<T>` 寫法 — 提示了一個成熟系統需要傳給 tool 哪些 contextual 資訊

### openai-agents-python

- **PLANS.md** + **AGENTS.md** + **CLAUDE.md** 三份 dev-facing doc — 看人家怎麼寫 agent SDK 的「如何擴充」說明
- **`docs/`** 的編寫順序（getting started → concepts → advanced → reference）— 對 AgenticRAG 自己的 docs 重整有借鑑
- **`tests/`** 大量 fixture pattern — 寫 agent test 怎麼 mock provider / tool 結果 / handoff，照學

---

## 維護規則

### 加新 reference codebase 時

1. 把整個 source tree 放到 `runtime_logic/<codebase-name>/`
2. 在 `.gitignore` 加 `runtime_logic/<codebase-name>/`
3. 在本 README 的「兩份 reference 概覽」加一節，列關鍵模組
4. 在「對應地圖」加新 codebase 適合解決的能力
5. commit 只動 README + .gitignore；source tree 永遠不進 repo

### 移植 pattern 進 anila-core / AgenticRAG 時

1. 在 PR description 註明來源 reference 路徑（譬如 `Inspired by openai-agents-python/src/agents/handoffs/agent_tool_input.py`）
2. **不引用** reference 的具體 code chunk
3. 移植完後在這份 README 的「已落地的能力」表加一行記錄

### 移除 reference 時

當 ANILA 自己的實作已經完整覆蓋 reference 的某個 pattern 集合，可以考慮把 reference 移到 archive。流程：

1. 在本 README 「已落地的能力」表確認所有想要的 pattern 都 ✓
2. 把該 codebase 從 `runtime_logic/<codebase-name>/` 搬到本機 archive
3. 更新 `.gitignore` 移除對應行
4. 更新本 README 把該 codebase 段落改寫成「已歸檔」備註

---

## 深入分析

| 對象 | 文件 |
|---|---|
| `openai-agents-python` | [`../docs/runtime-logic-openai-agents-deep-dive.md`](../docs/runtime-logic-openai-agents-deep-dive.md) — 12 條 subsystem 一一拆解 + 對 AgenticRAG 強化的 actionable map（P0–P2 順序與起點檔） |
| `claude-code-src` | **不另寫深入分析** — 該 codebase 自帶完整 `docs/` 與 `docs.zip`（離線快照），架構與 feature locator 都在裡面，需要時 grep 該 docs 即可 |

## 跟其他文件的關係

- 平台總覽：[`../README.md`](../README.md)
- Python runtime（移植目的地）：[`../anila-core/README.md`](../anila-core/README.md)
- RAG agent template（強化目標）：[`../AgenticRAG/README.md`](../AgenticRAG/README.md)
- 既有 service-token cutover plan：[`../docs/runbooks/service-token-cutover.md`](../docs/runbooks/service-token-cutover.md)
- Parent-child RAG design（Sprint 9 X）：[`../docs/parent-child-rag-design.md`](../docs/parent-child-rag-design.md)
- openai-agents-python deep dive：[`../docs/runtime-logic-openai-agents-deep-dive.md`](../docs/runtime-logic-openai-agents-deep-dive.md)

---

## 使用注意

- 兩份 reference 的 source tree 在 `.gitignore` 排除之下僅存在於本機副本，**僅作為本專案內部架構參考**
- 移植成 Python 時務必**用自己的實作重寫**，避免逐字複製原文。模式（pattern）與介面（interface）可以借鑑；具體 code 字串不可以
- 本 README 是 ANILA 工作流的一部分；source 不在 repo 不代表參考它的工作流也不在

---

**Last updated**: 2026-05-02（Sprint 9 X — runtime_logic 結構從單 TS reference 升級為雙 reference 配置）
**Status**: 兩份 source tree gitignored；只追本檔
**Next consumers**: AgenticRAG 強化（multi-agent handoff、tracing、guardrails、session）+ anila-core 補洞（PTL retry、stripImagesFromMessages、preventContinuation hook）
