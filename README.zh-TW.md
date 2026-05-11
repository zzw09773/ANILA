# anila-agent

[English](README.md) · **繁體中文**

Agentic RAG 起手樣板。runtime 以 [openai-agents SDK](https://github.com/openai/openai-agents-python) 為基礎，harness 工程（長期 memdir、hook 介面、slash command CLI）整套從 Claude Code 移植而來。

clone 下來、填上你的 retriever / prompts / tools，連到你的 OpenAI-compatible endpoint 就能跑。

## 裡面有什麼

| 分層 | 模組 | 出處 |
|------|------|------|
| Runtime | `core/agent.py`, `core/runner.py` | openai-agents 的 `Agent` + `Runner` |
| Hooks | `core/hooks.py` | Claude Code 的 `PreToolUse` / `PostToolUse` / `Stop` / `SessionStart` / `UserPromptSubmit` |
| 事件匯流排 | `core/events.py` | 程序內 pub/sub（與 openai-agents tracing 分離） |
| 長期記憶 | `memory/store.py`, `memory/long_term.py` | Claude Code `memdir/` 的直接移植（檔案式 `MEMORY.md` 索引 + `*.md` 主題檔搭配 YAML frontmatter，四種 type：`user` / `feedback` / `project` / `reference`） |
| 短期記憶 | `memory/short_term.py` | 包裝 openai-agents 的 `SQLiteSession` |
| 自動抽取 | `memory/summarizer.py` | 預設關閉；以 Stop hook 觸發的 side-LLM 抽取器（移植自 Claude Code `extractMemories`） |
| Retrieval | `retrieval/base.py`, `retrieval/dummy.py` | 你自己實作的 Protocol |
| pgvector（通用） | `retrieval/pgvector.py` | 以 langchain_postgres 為底；一行環境變數啟動 |
| pgvector（ANILA 平台） | `retrieval/anila_pgvector.py` | 直接打平台原生 `ingestion_collections` / `document_chunks` schema（halfvec + RLS） |
| Tools | `tools/base.py`, `tools/rag_tools.py`, `tools/filesystem_tools.py` | `@function_tool` + Anila metadata（`is_read_only`, `is_destructive`, …） |
| Models | `models/openai_compatible.py` | LiteLLM 為底；vLLM / Ollama / OpenAI / Together 等都通 |
| CLI | `cli/app.py`, `cli/commands.py`, `cli/renderer.py` | `prompt_toolkit` + `rich`，slash 指令移植自 Claude Code `commands.ts` |

## 快速開始

```bash
git clone https://github.com/zzw09773/anila-agent.git
cd anila-agent

# 安裝。uv（推薦）或 pip 都可。
uv venv && uv pip install -e ".[dev]"
# 或者：python -m venv .venv && .venv/Scripts/activate && pip install -e ".[dev]"

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

## 把專案填起來

只有三件事要客製，其他維持原樣即可。

### 1. Prompts

編輯 `anila_agent/prompts/system.md`。System prompt 在組裝 agent 時載入，可以直接引用 retrieval 與 memory 的概念。

### 2. Retriever

三種選擇，工作量由小到大。

**選項 A — 通用 pgvector，純環境變數（零程式碼）。** 裝好 optional extra 之後設兩個環境變數，`build_agent()` 會自動安裝 retriever：

```bash
uv pip install -e ".[pgvector]"
```

```env
PGVECTOR_URL=postgresql+psycopg2://user:pass@host:5432/db
PGVECTOR_COLLECTION=my_docs
# 選填 — embed endpoint 預設 fallback 到 ANILA_BASE_URL / ANILA_API_KEY
ANILA_EMBED_MODEL=text-embedding-3-small
```

底層是 `langchain_postgres.PGVector`。資料是用 langchain 灌進去（`langchain_pg_collection` + `langchain_pg_embedding` 兩張表）的話用這個。

**選項 B — ANILA 平台 schema，純環境變數（零程式碼）。** 一樣只設環境變數，但直接打平台的原生 `ingestion_collections` + `document_chunks` 表（halfvec + 透過 `anila.collection_id` GUC 做 RLS）。Embedding 維度會從 collection 那一列自動偵測。

```env
PGVECTOR_URL=postgresql://<user>:<password>@<host>:<port>/<db>
ANILA_COLLECTION_ID=<int collection id>
ANILA_EMBED_MODEL=nvidia/NV-embed-V2
ANILA_SSL_VERIFY=0   # embed endpoint 是自簽憑證才需要
```

`build_agent()` 啟動順序：`ANILA_COLLECTION_ID` → `PGVECTOR_COLLECTION` → `DummyRetriever`。半設定狀態（例如設了 `ANILA_COLLECTION_ID` 但漏了 `PGVECTOR_URL`）會直接 raise，不會默默 fallback。

**選項 C — 自己寫後端。** 在 `anila_agent/retrieval/base.py` 實作 `Retriever`：

```python
from anila_agent.retrieval.base import Retriever
from anila_agent.models.schemas import Document

class MyRetriever:
    @property
    def name(self) -> str: return "mine"
    async def search(self, query: str, k: int = 5) -> list[Document]: ...
    async def fetch(self, doc_id: str) -> Document | None: ...
```

在組 agent 之前安裝：

```python
from anila_agent.tools.rag_tools import set_retriever
set_retriever(MyRetriever())
```

內建的 `search_documents` 與 `read_document` 工具會自動透過它路由。範例見 `examples/rag_agent.py`。

### 3. Tools

裝飾函式：

```python
from anila_agent.tools.base import anila_tool

@anila_tool(is_read_only=True, category="domain")
def employee_count(department: str) -> int: ...
```

⋯或寫進 `configs/tools.yaml`：

```yaml
builtin:
  - mypkg.tools.employee_count
```

範例見 `examples/custom_tool.py`。

## Hooks

Hook 在 model 與 tool 事件前後觸發。每個 callback 回 `HookOutput`：

```python
from anila_agent.core.hooks import HookOutput, PreToolUseInput

async def deny_writes(payload: PreToolUseInput) -> HookOutput:
    if payload.tool_name.startswith("write_"):
        return HookOutput(decision="block", reason="read-only mode")
    return HookOutput()
```

註冊在 `configs/tools.yaml`：

```yaml
hooks:
  pre_tool_use:
    - { matcher: "write_.*", callback: mypkg.hooks.deny_writes }
```

可用事件：
- `pre_tool_use` — 攔截、改寫輸入、注入 context
- `post_tool_use` — 觀察輸出、為下一輪注入 context
- `stop` — agent 產生最終輸出時觸發

## Memory

### 長期（memdir）

檔案式儲存於 `<ANILA_HOME>/memory/`，結構：

```
memory/
  MEMORY.md              ← 索引，硬上限 200 行 / 25 KB
  user_role.md           ← 帶 YAML frontmatter 的主題檔
  feedback_testing.md
  project_release.md
```

每個主題檔：

```markdown
---
name: 短標題
description: 一行描述，給 recall selector 判斷相關性用
type: user|feedback|project|reference
---

自由格式的 markdown 內容
```

Recall 流程：掃目錄 → 把 manifest 丟給一個小 LLM 呼叫 → 回傳被選中的檔案。

### 自動抽取（預設關閉）

設定 `memory.yaml`：

```yaml
auto_memory:
  enabled: true
  min_messages_between_runs: 4
```

開啟後，每輪結束的 Stop hook 會跑一個側邊抽取呼叫，把提案寫成新的 memory 檔。關掉可確保每輪成本固定。

### 短期

由 openai-agents 的 `SQLiteSession` 提供，存在 `<ANILA_HOME>/sessions/anila.db`。重複使用同一個 `--session` ID 可以續接對話。

## Slash 指令

REPL 中可用：

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

## 設定

`configs/` 下四個 YAML：

- `agent.yaml` — 名稱、instructions 檔、最大輪數、tool 使用行為
- `model.yaml` — model、base URL、sampling 預設
- `memory.yaml` — 短期 + 長期 + 自動抽取
- `tools.yaml` — 內建工具清單、hook 註冊、MCP servers

環境變數覆蓋（寫在 `.env` 或 shell 都可）：

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

## 測試

```bash
pytest
```

覆蓋面刻意聚焦在 harness 層（memdir port、hook bridge、retriever scoring）— openai-agents 的原語有自己的測試套件。

## 目錄結構

```
anila-agent/
├── pyproject.toml
├── README.md
├── README.zh-TW.md
├── CHANGELOG.md
├── .env.example
├── configs/
│   ├── agent.yaml
│   ├── model.yaml
│   ├── memory.yaml
│   └── tools.yaml
├── anila_agent/
│   ├── main.py                 # CLI 入口
│   ├── cli/
│   │   ├── app.py              # REPL loop
│   │   ├── commands.py         # slash 指令解析
│   │   └── renderer.py         # 終端輸出
│   ├── core/
│   │   ├── agent.py            # Agent 組裝
│   │   ├── runner.py           # Tool loop 包裝
│   │   ├── hooks.py            # Hook 介面
│   │   └── events.py           # Event bus
│   ├── models/
│   │   ├── openai_compatible.py
│   │   └── schemas.py
│   ├── memory/
│   │   ├── short_term.py
│   │   ├── long_term.py
│   │   ├── store.py
│   │   └── summarizer.py
│   ├── retrieval/
│   │   ├── base.py            # Retriever Protocol
│   │   ├── dummy.py           # 記憶體內 token-overlap（預設）
│   │   ├── pgvector.py        # langchain_postgres
│   │   ├── anila_pgvector.py  # ANILA 平台原生 schema
│   │   └── examples/
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
    ├── test_tool_loop.py
    ├── test_memory.py
    ├── test_retriever.py
    ├── test_pgvector_retriever.py        # langchain 版本
    └── test_anila_pgvector_retriever.py  # 平台原生版本
```

## Changelog

版本歷程見 [CHANGELOG.md](CHANGELOG.md)。

## License

Apache-2.0，見 `LICENSE`。
