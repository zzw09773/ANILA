# runtime_logic — agent runtime 參考原始碼快照

> Agent runtime 的「設計參考目錄」：收兩份生產級 runtime 的原始碼快照，供 ANILA 對照、借鑑、把好的 design pattern 翻譯成 Python 後納入 `packages/anila-core/` 與 agent template。**這不是執行碼。**

> English mirror：[`README.en.md`](./README.en.md)

> 🌿 **分支對照**：本目錄存在於 `main` / `prod-intranet-card` / `prod-public-passwd` / `prod-military-passwd` / `dev-public` / `dev-military`。**`trial-military` 精簡版不含本目錄**。分支策略見根目錄 [`README.md`](../README.md) 的分支對照表與 [`docs/branch-sync-backlog.md`](../docs/branch-sync-backlog.md)。

---

## 簡介

本目錄收兩份**生產級 agent runtime 的原始碼快照**當「設計參考」：

- **不是 ANILA 執行碼**。執行碼在 `packages/anila-core/` 的 Python tree、agent template 在 [`anila-agent`](../packages/anila-agent/)。
- 目的是**對照、借鑑、把成熟的設計模式翻譯成 Python** 後納入 `packages/anila-core/`。
- 規範：**讀 pattern、學介面、自己重寫**；不可逐字複製。授權見各 codebase 自身 LICENSE。

> ⚠️ **`claude-code-src/` 與 `openai-agents-python/` 的整個 source tree 已由 `.gitignore` 排除**，不會進 repo。唯一被追蹤的檔案是本 README 與其 English mirror，原始碼在本機維護。

兩份 reference 涵蓋兩個維度：

| Reference | 長處 | 對應 ANILA 痛點 |
|---|---|---|
| **`claude-code-src/`**（TypeScript Claude Code CLI） | 長期單一 conversation 的 turn-loop 細節（compact 三層、PTL retry、background memory extraction、stop hooks、tool-as-folder） | 已移植 7-stage QueryEngine、coordinator、compact 三層、SessionMemory、Memdir；尚有 PTL retry、preventContinuation hook、prompt-cache fork prefix 待移植 |
| **`openai-agents-python/`**（OpenAI 官方 Agents SDK） | 多 agent 編排（handoffs / agents-as-tools / sub-agent state）、tool guardrails、structured tracing、sandbox tool execution、session abstraction、MCP 整合 | 現為單 agent + 線性 tool loop；要往「多 agent handoff RAG」「sandboxed tool」「結構化 tracing」走，這份是現成藍圖 |

「單 agent 深度」+「多 agent 廣度」。

---

## 內容

| 項目 | 說明 |
|---|---|
| `README.md` | 本檔（繁中 primary） |
| `README.en.md` | English mirror |
| `claude-code-src/`（**gitignored**） | TS Claude Code CLI source 本機快照。關鍵：`QueryEngine.ts`（7-stage）、`query/`、`tools/`（43 個 tool，含 `AgentTool/`）、`services/compact/`、`services/extractMemories/`、`memdir/`、`coordinator/`、`hooks/toolPermission/`。`ink/` `components/` `screens/`（terminal UI）**不要移植**。 |
| `openai-agents-python/`（**gitignored**） | OpenAI Agents SDK source 本機快照。關鍵：`handoffs/`、`tool_guardrails.py` / `guardrail.py`、`tracing/`、`sandbox/`、`memory/`、`models/`、`mcp/`、`lifecycle.py`、`run.py`。`realtime/`（voice）多半不需要。 |

> 兩個 source tree 僅存在本機（gitignored）。`ls runtime_logic/` 在 repo 上只看到 `README.md` 與 `README.en.md`。

---

## 用途與讀者

- **讀者**：要在 `packages/anila-core/` 或 RAG agent template 上長新 agent-runtime 能力的工程師。
- **何時讀**：要新增能力（multi-agent handoff、tracing、guardrails、session、PTL retry…）時，先查下方「強化對應地圖」找「哪份 reference 的哪個檔有現成 pattern」，再讀 contract、自己用 Python 重寫。
- **不是**：部署清單、執行手冊、或可直接 import 的套件。

### 強化對應地圖（節選）

#### 已落地（`anila-core` 已實作）

| 能力 | 來源 reference | 目前位置 |
|---|---|---|
| 7-stage turn loop | `claude-code-src/src/QueryEngine.ts` + `query/config.ts` | `packages/anila-core/.../engine/query_engine.py` |
| BudgetTracker + diminishing returns | `claude-code-src/src/query/tokenBudget.ts` | `.../engine/budget_tracker.py` |
| ExtractMemories + cursor | `claude-code-src/src/services/extractMemories/` | `.../memory/extract_memories.py` |
| AutoCompact / MicroCompact / SessionMemory | `claude-code-src/src/services/compact/` | `.../compact/` |
| Memdir 4-type taxonomy | `claude-code-src/src/memdir/memoryTypes.ts` | `.../memory/memdir.py` |
| Coordinator XML notifications | `claude-code-src/src/coordinator/coordinatorMode.ts` | `.../coordinator/coordinator.py` |

#### 強化 backlog（⭐ = 高 ROI）

| P | 能力 | reference 位置 | 預估 |
|---|---|---|---|
| P0 ⭐ | Multi-agent handoff | `openai-agents-python/src/agents/handoffs/` + `extensions/handoff_filters.py` | 3–5 天 |
| P0 ⭐ | Tracing 框架 | `openai-agents-python/src/agents/tracing/` | 2 天 |
| P0 ⭐ | Tool guardrails | `openai-agents-python/src/agents/tool_guardrails.py` + `guardrail.py` | 1.5 天 |
| P0 | PTL (Prompt Too Long) retry | `claude-code-src/src/services/compact/compact.ts` | 0.5 天 |
| P0 | `stripImagesFromMessages` for compact | 同上 | 0.3 天 |
| P0 | `preventContinuation` stop hook | `claude-code-src/src/query/stopHooks.ts` | 0.5 天 |
| P1 | Session abstraction（auto-compact persistence） | `openai-agents-python/src/agents/memory/session.py` | 2 天 |
| P1 | MCP server 作為 tool source | `openai-agents-python/src/agents/mcp/` | 2 天 |
| P1 | AgentTool fork — byte-identical prefix（prompt-cache 共享） | `claude-code-src/src/tools/AgentTool/` | 1 天 |
| P1 | Lifecycle hooks | `openai-agents-python/src/agents/lifecycle.py` | 1.5 天 |

#### 明確不適合移植

- `claude-code-src/src/{ink,components,screens,buddy,voice,keybindings}/` — terminal UI / CLI bound。
- `openai-agents-python/src/agents/realtime/` — voice / WebSocket，本案非目標。

### 移植工作流

1. **查對應地圖** —「我們要加 X」→ 翻 backlog → 找到 reference 模組。
2. **讀 contract，不讀 implementation** — 看簽名 / type / 配置 / 錯誤處理 / 依賴；不逐字翻譯。
3. **設計 ANILA 自己的版本** — 考量 pgvector / 多租戶 / 中文 corpus / on-prem 約束。
4. **寫 Python 版** — 自己重寫、命名、docstring；注釋可引用 reference 路徑作 design provenance，但**不引用具體 code**。
5. **跨 reference verify** — 兩份都有的 module 比對處理同一問題的差異，差異即真正的 design decision。

---

## 啟動與部署

**N/A（參考 / 設計目錄）。** 沒有 `package.json` / `Dockerfile` / `pyproject.toml`，不可建置、不可部署、不可 import。實際可執行碼在 [`../packages/anila-core/`](../packages/anila-core/)。

---

## 相關文件

- 平台總覽：[`../README.md`](../README.md) · 分支策略：[`../docs/branch-sync-backlog.md`](../docs/branch-sync-backlog.md)
- Python runtime（移植目的地）：[`../packages/anila-core/README.md`](../packages/anila-core/README.md)
- `openai-agents-python` 深入分析：[`../docs/agent-framework/runtime-logic-openai-agents-deep-dive.md`](../docs/agent-framework/runtime-logic-openai-agents-deep-dive.md)
- Agent framework 架構與移植決策：[`../docs/agent-framework/anila-agent-framework-architecture.md`](../docs/agent-framework/anila-agent-framework-architecture.md)、[`../docs/agent-framework/anila-agent-framework-porting-decisions.md`](../docs/agent-framework/anila-agent-framework-porting-decisions.md)

---

## 維護規則

- **加新 reference codebase**：放 `runtime_logic/<name>/` → 在 `.gitignore` 加 `runtime_logic/<name>/` → 在本 README 加一節 → commit 只動 README + `.gitignore`，source tree 永不進 repo。
- **移植 pattern**：PR description 註明來源 reference 路徑；**不引用**具體 code chunk；移植完在「已落地」表加一行。
- **移除 reference**：確認想要的 pattern 都已落地 → 搬到本機 archive → 更新 `.gitignore` 與本 README。

---

**Status**：兩份 source tree gitignored；repo 只追蹤 `README.md` 與 `README.en.md`。
