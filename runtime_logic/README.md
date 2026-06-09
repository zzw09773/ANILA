# runtime_logic — agent runtime 參考原始碼快照 / agent runtime reference snapshots

> Agent runtime 的「設計參考目錄」：收兩份生產級 runtime 的原始碼快照，供 ANILA 對照、借鑑、把好的 design pattern 翻譯成 Python 後納入 `anila-core/` 與 agent template。**這不是執行碼**。

> English mirror: [`README.en.md`](./README.en.md)

---

## 簡介 / Overview

本目錄收兩份**生產級 agent runtime 的原始碼快照**，當作「設計參考」用：

- **不是 ANILA 的執行碼**。執行碼在 `anila-core/` 的 Python tree、agent template 在另一個 agent 專案。
- 目的是**對照、借鑑、把成熟的設計模式翻譯成 Python** 後納入 `anila-core/`。
- 規範：**讀 pattern、學介面、自己重寫**；不可逐字複製。授權見各 codebase 自身的 LICENSE。

> ⚠️ **`claude-code-src/` 與 `openai-agents-python/` 的整個 source tree 已由 `.gitignore` 排除**（見 repo 根 `.gitignore`，項目 `runtime_logic/claude-code-src/`、`runtime_logic/openai-agents-python/` 等），不會進 repo。唯一被追蹤的檔案是本 README 與其 English mirror。原始碼在本機維護。

兩份 reference 合起來涵蓋兩個維度：

| Reference | 長處 | 對應 ANILA 痛點 |
|---|---|---|
| **`claude-code-src/`**（TypeScript Claude Code CLI） | 長期單一 conversation 的 turn-loop 細節（compact 三層、PTL retry、background memory extraction、stop hooks、tool-as-folder 分離） | 已移植 7-stage QueryEngine、coordinator、compact 三層、SessionMemory、Memdir 4-type 到 `anila-core/`；尚有 **PTL retry、preventContinuation hook、prompt-cache fork prefix** 待移植 |
| **`openai-agents-python/`**（OpenAI 官方 Agents SDK） | 多 agent 編排（handoffs / agents-as-tools / sub-agent state）、tool guardrails、structured tracing、sandbox tool execution、session abstraction、retry semantics、MCP 整合 | 目前 RAG agent 為單 agent + 線性 tool loop；要往「多 agent handoff RAG」「sandboxed tool 執行」「結構化 tracing」走，這份是現成藍圖 |

「單 agent 深度」+「多 agent 廣度」。

---

## 內容 / Contents

| 項目 | 說明 |
|---|---|
| `README.md` | 本檔（繁中 primary）— reference 用途、雙 codebase 概覽、AgenticRAG 強化對應地圖、移植工作流、維護規則 |
| `README.en.md` | English mirror |
| `claude-code-src/`（**gitignored**） | TypeScript Claude Code CLI source tree 本機快照。關鍵子模組：`QueryEngine.ts`（7-stage turn loop）、`query/`（config / deps / stopHooks / tokenBudget）、`tools/`（43 個 tool，每個一資料夾，含 `AgentTool/`）、`services/compact/`（micro / auto / sessionMemory）、`services/extractMemories/`、`memdir/`、`coordinator/`、`hooks/toolPermission/`。`ink/` `components/` `screens/` 為 terminal UI（**不要移植**）。 |
| `openai-agents-python/`（**gitignored**） | OpenAI Agents SDK source tree 本機快照。關鍵子模組：`src/agents/handoffs/`（多 agent 控制權移轉）、`tool_guardrails.py` / `guardrail.py`、`tracing/`、`sandbox/`、`memory/`（`session.py` / `sqlite_session.py` / `*_compaction_session.py`）、`models/`（provider abstraction + retry）、`mcp/`、`lifecycle.py`、`stream_events.py`、`run.py`。`realtime/` 為 voice 路徑（多半 RAG 不需要）。 |

> 兩個 source tree 僅存在於本機（gitignored）。`ls runtime_logic/` 在 repo 上只會看到三個被追蹤的檔：`README.md`、`README.en.md`。完整檔案結構請看本機副本。

---

## 用途與讀者 / Purpose & audience

- **讀者**：要在 `anila-core/` 或 RAG agent template 上長新 agent-runtime 能力的工程師。
- **何時讀**：每當要新增能力（multi-agent handoff、tracing、guardrails、session、PTL retry…）時，先到下方「強化對應地圖」查「哪份 reference 的哪個檔有現成 pattern」，再讀 contract、自己用 Python 重寫。
- **不是**：部署清單、執行手冊、或可直接 import 的套件。

### 強化對應地圖（節選）/ Enhancement map (excerpt)

#### 已落地（`anila-core` 已實作）

| 能力 | 來源 reference | 目前位置 |
|---|---|---|
| 7-stage turn loop | `claude-code-src/src/QueryEngine.ts` + `query/config.ts` | `anila-core/src/anila_core/engine/query_engine.py` |
| BudgetTracker + diminishing returns | `claude-code-src/src/query/tokenBudget.ts` | `anila-core/src/anila_core/engine/budget_tracker.py` |
| ExtractMemories + cursor | `claude-code-src/src/services/extractMemories/` | `anila-core/src/anila_core/memory/extract_memories.py` |
| AutoCompact / MicroCompact / SessionMemory | `claude-code-src/src/services/compact/` | `anila-core/src/anila_core/compact/` |
| Memdir 4-type taxonomy | `claude-code-src/src/memdir/memoryTypes.ts` | `anila-core/src/anila_core/memory/memdir.py` |
| Coordinator XML notifications | `claude-code-src/src/coordinator/coordinatorMode.ts` | `anila-core/src/anila_core/coordinator/coordinator.py` |

#### 強化 backlog（priority；⭐ = 高 ROI）

| P | 能力 | reference 位置 | 預估 |
|---|---|---|---|
| P0 ⭐ | Multi-agent handoff（retrieve → answer → cite-verify pipeline） | `openai-agents-python/src/agents/handoffs/` + `extensions/handoff_filters.py` | 3–5 天 |
| P0 ⭐ | Tracing 框架 | `openai-agents-python/src/agents/tracing/` | 2 天 |
| P0 ⭐ | Tool guardrails | `openai-agents-python/src/agents/tool_guardrails.py` + `guardrail.py` | 1.5 天 |
| P0 | PTL (Prompt Too Long) retry | `claude-code-src/src/services/compact/compact.ts`（`truncateHeadForPTLRetry`, `MAX_PTL_RETRIES=3`） | 0.5 天 |
| P0 | `stripImagesFromMessages` for compact | `claude-code-src/src/services/compact/compact.ts` | 0.3 天 |
| P0 | `preventContinuation` stop hook semantic | `claude-code-src/src/query/stopHooks.ts` | 0.5 天 |
| P1 | Session abstraction（auto-compact persistence） | `openai-agents-python/src/agents/memory/session.py` 等 | 2 天 |
| P1 | MCP server 作為 tool source | `openai-agents-python/src/agents/mcp/` | 2 天 |
| P1 | AgentTool fork — byte-identical prefix（prompt-cache 共享） | `claude-code-src/src/tools/AgentTool/` | 1 天 |
| P1 | Lifecycle hooks（`on_start`/`on_tool_start`/`on_handoff`/`on_end`） | `openai-agents-python/src/agents/lifecycle.py` | 1.5 天 |
| P2 | Magic Docs / PromptSuggestion / AwaySummary / Realtime | `claude-code-src/src/services/*`、`openai-agents-python/src/agents/realtime/` | 視需求 |

#### 明確不適合移植

- `claude-code-src/src/ink/`、`components/`、`screens/`、`buddy/`、`voice/`、`keybindings/` — terminal UI / CLI bound。
- `openai-agents-python/src/agents/realtime/` — voice / WebSocket，本案非目標。

### 移植工作流 / Porting workflow

1. **查對應地圖** — 「我們要加 X」→ 翻 backlog 表 → 找到 reference 模組。
2. **讀 contract，不讀 implementation** — 看函式簽名 / type shape / 配置選項 / 錯誤處理 / 模組依賴；不逐字翻譯邏輯、不搬 internal helper。
3. **設計 ANILA 自己的版本** — 考量 pgvector / 多租戶 / 中文 corpus / on-prem 約束；reference 常 over-design，可更貼合。
4. **寫 Python 版** — 自己重寫、自己命名、自己寫 docstring；注釋可引用 reference 路徑作 design provenance（如 `# Pattern from runtime_logic/openai-agents-python/src/agents/handoffs/`），但**不引用具體 code**。
5. **跨 reference verify** — 兩份都有的 module（compact / memory）比對它們如何處理同一問題，差異即真正的 design decision。

---

## 啟動與部署 / Setup & Run

**N/A（參考/設計目錄）。** 本目錄沒有 `package.json` / `Dockerfile` / `pyproject.toml`，不可建置、不可部署、不可 import。它是設計參考與移植工作流的文件目錄；實際可執行碼在 `../anila-core/`。

---

## 相關文件 / Related docs

- 平台總覽：[`../README.md`](../README.md)
- Python runtime（移植目的地）：[`../anila-core/README.md`](../anila-core/README.md)
- `openai-agents-python` 深入分析（12 條 subsystem 拆解 + P0–P2 起點檔）：[`../docs/agent-framework/runtime-logic-openai-agents-deep-dive.md`](../docs/agent-framework/runtime-logic-openai-agents-deep-dive.md)
- Agent framework 架構：[`../docs/agent-framework/anila-agent-framework-architecture.md`](../docs/agent-framework/anila-agent-framework-architecture.md)
- Parent-child RAG design：[`../docs/ingestion/parent-child-rag-design.md`](../docs/ingestion/parent-child-rag-design.md)
- Service-token cutover plan：[`../docs/runbooks/service-token-cutover.md`](../docs/runbooks/service-token-cutover.md)

> `claude-code-src` 不另寫深入分析 — 該 codebase 自帶完整 `docs/` 與 `docs.zip`（離線快照），需要時 grep 本機副本即可。

---

## 維護規則 / Maintenance

- **加新 reference codebase**：放到 `runtime_logic/<name>/` → 在 `.gitignore` 加 `runtime_logic/<name>/` → 在本 README 的「內容」與「對應地圖」加一節 → commit 只動 README + `.gitignore`，source tree 永不進 repo。
- **移植 pattern**：PR description 註明來源 reference 路徑；**不引用**具體 code chunk；移植完在本 README「已落地」表加一行。
- **移除 reference**：確認所有想要的 pattern 都已落地 → 把 codebase 搬到本機 archive → 更新 `.gitignore` 與本 README。

---

**Status**: 兩份 source tree gitignored；repo 只追蹤 `README.md` 與 `README.en.md`。
