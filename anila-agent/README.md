# anila-agent

> Agentic RAG 起手樣板：以 openai-agents SDK 為 runtime，把 Claude Code 的 harness 工程（長期 memdir、hook 介面、slash command CLI）整套移植過來。

**繁體中文** · [English](README.en.md)

> 📌 **此檔屬 `prod` 分支(中科院內網部署版)**。anila-agent 本身是 sub-agent 模板,跟 prod 部署模式無耦合。內容與 main 一致。

## 簡介 / Overview

`anila-agent` 是一個可以 clone 下來就開跑的 Agentic RAG 樣板專案。runtime 以 [openai-agents SDK](https://github.com/openai/openai-agents-python) 為基礎，harness 工程則整套從 Claude Code 移植而來。

填上你的 retriever / prompts / tools，連到任一 OpenAI-compatible endpoint 就能跑。

在 ANILA 平台中，這份專案同時扮演兩個角色：

- **獨立樣板** — 任何人 clone 下來改成自己的 agent。
- **平台官方模板** — 整個目錄被 mount 進 CSP 平台作為下載模板（見 §與其他服務的關係）。

## 架構與技術棧 / Architecture & Stack

- **語言／執行環境**：Python `>=3.10`，以 `pyproject.toml`（hatchling build backend）管理。
- **核心依賴**（`[project].dependencies`）：
  - `openai-agents[litellm]>=0.3.0` — agent runtime（`Agent` + `Runner`）與 LiteLLM model provider。
  - `pydantic>=2.7`、`pyyaml>=6.0`、`python-dotenv>=1.0` — schema、設定檔、`.env` 載入。
  - `prompt-toolkit>=3.0`、`rich>=13.0` — REPL CLI 與終端輸出。
- **選用依賴**：
  - `pgvector` extra — `langchain-postgres`、`langchain-openai`、`psycopg[binary]`，啟用內建的兩種 pgvector retriever。
  - `dev` extra — `pytest`、`pytest-asyncio`、`pytest-cov`、`ruff`、`mypy`。
- **進入點**：`anila = anila_agent.main:main`（裝好後直接 `anila` 啟動 REPL）。

各分層與其出處：

| 分層 | 模組 | 出處 |
|------|------|------|
| Runtime | `core/agent.py`, `core/runner.py` | openai-agents 的 `Agent` + `Runner`；`AnilaRunner` 加上 hook 觸發、abort 處理、event stream |
| Hooks | `core/hooks.py` | Claude Code 的 `PreToolUse` / `PostToolUse` / `Stop` / `SessionStart` / `UserPromptSubmit` |
| 事件匯流排 | `core/events.py` | 程序內 pub/sub（與 openai-agents tracing 分離） |
| 長期記憶 | `memory/store.py`, `memory/long_term.py` | Claude Code `memdir/` 的直接移植（檔案式 `MEMORY.md` 索引 + `*.md` 主題檔搭配 YAML frontmatter，四種 type：`user` / `feedback` / `project` / `reference`） |
| 短期記憶 | `memory/short_term.py` | 包裝 openai-agents 的 `SQLiteSession` |
| 自動抽取 | `memory/summarizer.py` | 預設關閉；以 Stop hook 觸發的 side-LLM 抽取器（移植自 Claude Code `extractMemories`） |
| Retrieval | `retrieval/base.py`, `retrieval/dummy.py` | 你自己實作的 Protocol，預設用 in-memory token-overlap 的 `DummyRetriever` |
| pgvector（通用） | `retrieval/pgvector.py` | 以 `langchain_postgres` 為底；一行環境變數啟動 |
| pgvector（ANILA 平台） | `retrieval/anila_pgvector.py` | 直接打平台原生 `ingestion_collections` / `document_chunks` schema（halfvec + RLS） |
| Tools | `tools/base.py`, `tools/rag_tools.py`, `tools/filesystem_tools.py` | `@function_tool` + Anila metadata（`is_read_only`, `is_destructive`, …） |
| Models | `models/openai_compatible.py` | LiteLLM 為底；vLLM / Ollama / OpenAI / Together 等都通 |
| CLI | `cli/app.py`, `cli/commands.py`, `cli/renderer.py` | `prompt_toolkit` + `rich`，slash 指令移植自 Claude Code `commands.ts` |

## 目錄結構 / Layout

```
anila-agent/
├── pyproject.toml
├── README.md                    # 繁體中文（主）
├── README.en.md                 # English（副）
├── CHANGELOG.md
├── LICENSE
├── .env.example
├── configs/
│   ├── agent.yaml               # 名稱、instructions 檔、最大輪數、tool 使用行為
│   ├── model.yaml               # model、base URL、sampling 預設
│   ├── memory.yaml              # 短期 + 長期 + 自動抽取
│   └── tools.yaml               # 內建工具清單、hook 註冊、MCP servers
├── anila_agent/
│   ├── main.py                  # CLI 入口（建構 AnilaRunner）
│   ├── cli/
│   │   ├── app.py               # REPL loop
│   │   ├── commands.py          # slash 指令解析
│   │   └── renderer.py          # 終端輸出
│   ├── core/
│   │   ├── agent.py             # Agent 組裝（build_agent / AssembledAgent）
│   │   ├── runner.py            # Tool loop 包裝（AnilaRunner）
│   │   ├── hooks.py             # Hook 介面
│   │   └── events.py            # Event bus
│   ├── models/
│   │   ├── openai_compatible.py
│   │   └── schemas.py
│   ├── memory/
│   │   ├── short_term.py
│   │   ├── long_term.py
│   │   ├── store.py
│   │   └── summarizer.py
│   ├── retrieval/
│   │   ├── base.py              # Retriever Protocol
│   │   ├── dummy.py             # 記憶體內 token-overlap（預設）
│   │   ├── pgvector.py          # langchain_postgres
│   │   └── anila_pgvector.py    # ANILA 平台原生 schema
│   ├── tools/
│   │   ├── base.py
│   │   ├── registry.py
│   │   ├── rag_tools.py
│   │   └── filesystem_tools.py
│   ├── prompts/
│   │   ├── system.md
│   │   ├── agent.md
│   │   └── tool_policy.md
│   └── utils/
│       ├── config.py
│       └── logging.py
├── examples/
│   ├── basic_chat.py
│   ├── rag_agent.py
│   └── custom_tool.py
└── tests/
    ├── conftest.py
    ├── test_tool_loop.py
    ├── test_memory.py
    ├── test_retriever.py
    ├── test_pgvector_retriever.py        # langchain 版本
    └── test_anila_pgvector_retriever.py  # 平台原生版本
```

## 啟動與部署 / Setup & Run

```bash
git clone https://github.com/zzw09773/anila-agent.git
cd anila-agent

# 安裝。uv（推薦）或 pip 都可。
uv venv && uv pip install -e ".[dev]"
# 或者：python -m venv .venv && source .venv/bin/activate && pip install -e ".[dev]"

# 要用內建的 pgvector retriever 時加裝這個 extra
# （不用就維持 DummyRetriever）。
uv pip install -e ".[dev,pgvector]"

# 連到你的 OpenAI-compatible endpoint。
cp .env.example .env
# 編輯 .env：ANILA_BASE_URL、ANILA_API_KEY、ANILA_MODEL

# 跑 REPL。
anila
# 或單次執行：
anila --prompt "hello"
```

### 把專案填起來

只有三件事要客製，其他維持原樣即可。

**1. Prompts** — 編輯 `anila_agent/prompts/system.md`。System prompt 在組裝 agent 時載入，可直接引用 retrieval 與 memory 的概念。fresh clone 出貨的 prompts 為 TODO 佔位（v0.2.1 起移除了 Anila 身分行），請填上自己的設定。

**2. Retriever** — 三種選擇，工作量由小到大：

- **選項 A — 通用 pgvector，純環境變數（零程式碼）。** 裝好 extra 後設 `PGVECTOR_URL` + `PGVECTOR_COLLECTION`，`build_agent()` 會自動安裝 retriever。底層是 `langchain_postgres.PGVector`（`langchain_pg_collection` + `langchain_pg_embedding` 兩張表）。
- **選項 B — ANILA 平台 schema，純環境變數（零程式碼）。** 設 `PGVECTOR_URL` + `ANILA_COLLECTION_ID`，直接打平台原生 `ingestion_collections` + `document_chunks` 表（halfvec + 透過 `anila.collection_id` GUC 做 RLS）。Embedding 維度從 collection 那一列自動偵測。`build_agent()` 啟動順序：`ANILA_COLLECTION_ID` → `PGVECTOR_COLLECTION` → `DummyRetriever`；半設定狀態會直接 raise，不會默默 fallback。
- **選項 C — 自己寫後端。** 在 `anila_agent/retrieval/base.py` 實作 `Retriever`，於組 agent 前以 `set_retriever()` 安裝。內建的 `search_documents` / `read_document` 會自動路由。範例見 `examples/rag_agent.py`。

```python
from anila_agent.tools.rag_tools import set_retriever
set_retriever(MyRetriever())
```

**3. Tools** — 裝飾函式或寫進 `configs/tools.yaml`：

```python
from anila_agent.tools.base import anila_tool

@anila_tool(is_read_only=True, category="domain")
def employee_count(department: str) -> int: ...
```

```yaml
builtin:
  - mypkg.tools.employee_count
```

範例見 `examples/custom_tool.py`。

### Hooks

Hook 在 model 與 tool 事件前後觸發，每個 callback 回 `HookOutput`，註冊在 `configs/tools.yaml`：

```yaml
hooks:
  pre_tool_use:
    - { matcher: "write_.*", callback: mypkg.hooks.deny_writes }
```

可用事件：`pre_tool_use`（攔截、改寫輸入、注入 context）、`post_tool_use`（觀察輸出、為下一輪注入 context）、`stop`（agent 產生最終輸出時觸發）。

### Memory

- **長期（memdir）** — 檔案式儲存於 `<ANILA_HOME>/memory/`：`MEMORY.md` 索引（硬上限 200 行 / 25 KB）+ 帶 YAML frontmatter 的 `*.md` 主題檔（`name` / `description` / `type`）。Recall 流程：掃目錄 → 把 manifest 丟給小 LLM → 回傳被選中的檔案。
- **自動抽取（預設關閉）** — 在 `memory.yaml` 設 `auto_memory.enabled: true` 後，每輪結束的 Stop hook 會跑一次側邊抽取呼叫，把提案寫成新的 memory 檔。關掉可確保每輪成本固定。
- **短期** — 由 openai-agents 的 `SQLiteSession` 提供，存在 `<ANILA_HOME>/sessions/anila.db`，重複使用同一個 `--session` ID 可續接對話。

### Slash 指令（REPL 中可用）

| 指令 | 作用 |
|------|------|
| `/help` | 列出指令 |
| `/clear` | 清掉短期 session 歷史 |
| `/memory list` | 顯示 MEMORY.md 索引 |
| `/memory scan` | 顯示完整的 memory 檔 manifest |
| `/memory extract` | 強制執行一次自動抽取（需先啟用） |
| `/model` | 顯示目前使用的 model |
| `/cost` | 顯示這個 session 的指標 |
| `/exit` | 離開 |

可在 `anila_agent/cli/commands.py` 加自己的指令。

### 設定與環境變數

`configs/` 下有四個 YAML（`agent.yaml` / `model.yaml` / `memory.yaml` / `tools.yaml`）。環境變數覆蓋（寫在 `.env` 或 shell 都可）：

| 變數 | 用途 |
|------|------|
| `ANILA_BASE_URL` | OpenAI-compatible endpoint |
| `ANILA_API_KEY` | endpoint 的 token |
| `ANILA_MODEL` | model 名稱 |
| `ANILA_HOME` | 狀態目錄（預設 `./.anila`） |
| `ANILA_AUTO_MEMORY` | 設 `1` 可覆蓋 `memory.yaml` 強制開啟自動抽取 |
| `ANILA_LOG_LEVEL` | log 等級 |
| `PGVECTOR_URL` | 兩種 pgvector retriever 共用的 Postgres DSN |
| `PGVECTOR_COLLECTION` | Collection **名稱** — 啟用 langchain-postgres retriever |
| `ANILA_COLLECTION_ID` | Collection **id**（int）— 啟用 ANILA 平台 retriever，優先序高於 `PGVECTOR_COLLECTION` |
| `ANILA_EMBED_MODEL` | Embedding model 名稱（預設 `text-embedding-3-small`） |
| `ANILA_EMBED_BASE_URL` | Embedding endpoint，未設時 fallback 到 `ANILA_BASE_URL` |
| `ANILA_EMBED_API_KEY` | Embedding key，未設時 fallback 到 `ANILA_API_KEY` |
| `ANILA_SSL_VERIFY` | 設 `0` 可跳過 TLS 驗證（僅用於自簽憑證） |

### 測試

```bash
pytest
```

覆蓋面刻意聚焦在 harness 層（memdir port、hook bridge、retriever scoring）— openai-agents 的原語有自己的測試套件。

## 與其他服務的關係 / Integration

- **CSP 平台模板**：整個 `anila-agent` 目錄被 platform 的 `docker-compose.yml` / `docker-compose-dev.yml` 以 `./anila-agent:/app/anila-template:ro` 掛載進容器，並透過 `ANILA_TEMPLATE_DIR=/app/anila-template` 指向它。CSP 後端（`myCSPPlatform/backend/app/api/agents.py`）讀取此目錄，於 `/template/download` 端點打包成 `anila-core-template.zip` 供開發者下載——即「一份原始碼，既是樣板也是平台模板」。
- **anila-core**：CLI 的 bootstrap 指令（`anila-core/src/anila_core/cli/bootstrap_cmd.py`、`templates/agent-template/`）也參照同一份模板來腳手架新 agent；CSP 與 anila-core 兩條路徑共用 `ANILA_TEMPLATE_DIR` 約定。
- **agent framework**：本專案 v0.1 實作對齊 agent-framework 的綜合架構（openai-agents + Claude Code 模式合成），而非逐檔移植。runtime 仍以上游 openai-agents SDK 為底，harness（hook / memory / CLI）為移植層。

> 更新 `anila-agent` 子樹的方式走 git subtree pull/push（見 platform 根目錄 README 的 “Updating anila-agent” 一節）。

## 相關文件 / Related docs

- [`../docs/agent-framework/anila-agent-framework-architecture.md`](../docs/agent-framework/anila-agent-framework-architecture.md) — 框架架構（綜合設計，canonical）
- [`../docs/agent-framework/anila-agent-framework-porting-decisions.md`](../docs/agent-framework/anila-agent-framework-porting-decisions.md) — 移植決策（已 SUPERSEDED，仍可作各上游子系統的逐源參考）
- 版本歷程見 [CHANGELOG.md](CHANGELOG.md)。

## License

Apache-2.0，見 [`LICENSE`](LICENSE)。

---

**Last updated**: 2026-05-26(同步 PR #16 + 加 prod banner;subtree 內容與 main 一致)
