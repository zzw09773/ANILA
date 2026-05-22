# anila-core

**ANILA Core** — Python agent runtime foundation（SDK）。ANILA 平台所有 agent 與 Router 共用的 in-process runtime 基座，外加被整個後端 fleet 共用的 shared infrastructure。

---

## 簡介 / Overview

`anila-core` 是 ANILA 平台的 **Python runtime 基座**，定位為純 runtime — 不綁特定 RAG pipeline、不綁特定向量庫、不綁特定模型供應商。它同時承擔兩個責任：

- **Pillar 1 · Agent runtime** — Router 與每支 agent 服務處理單一聊天 turn 所需的 in-process 元件（QueryEngine、Coordinator、providers、memory、tools、context、API server…）。
- **Pillar 2 · Shared infrastructure** — 不專屬於某個 agent process、被整個 ANILA 後端 fleet（Router、agent、ingestion-worker 及未來各種 batch worker）共用的基底元件（credential crypto、SSRF guard、pg / pgvector adapters、chunking plugins、ingestion error taxonomy）。

平台中各角色與 anila-core 的關係：

- **Router 部署**（`anila-core-router`）：直接 `import` Pillar 1 + Pillar 2。
- **Agent 開發者**：`pip install "anila-core[rag]"` 並 fork [`anila-agent`](../anila-agent/) 作為官方 RAG agent starter template。`[rag]` extra 提供文件解析所需的重量級套件（pymupdf / python-docx / odfpy / striprtf / Pillow）。
- **ingestion-worker**（Arq 非同步 pipeline）：只消費 Pillar 2（`chunking_plugins`、`IngestionError`、`pg_pool`、`pgvector_store`、`credential_crypto`），不碰 Pillar 1。

> Repo 根定位請看 [`../README.md`](../README.md)。English mirror：[`README.en.md`](./README.en.md)。

---

## 架構與技術棧 / Architecture & Stack

- **語言**：Python `>=3.11`
- **建置**：`hatchling`（套件來源 `src/anila_core`）
- **套件名 / 版本**：`anila-core` v0.14.0

### Core dependencies（`pyproject.toml`）

| 套件 | 用途 |
|------|------|
| `pydantic>=2.0` / `pydantic-settings>=2.0` | DTO models + Settings（router-mode env loading） |
| `fastapi>=0.110` / `uvicorn[standard]>=0.29` | API server（Router / agent server） |
| `httpx>=0.27` | provider 上游呼叫 |
| `sse-starlette>=2.0` | streaming（SSE turn 串流） |
| `python-frontmatter>=1.1` / `pyyaml>=6.0` | agent definition / 設定載入 |
| `anyio>=4.0` / `aiofiles>=23.0` | async IO |
| `asyncpg>=0.29` / `pgvector>=0.3` | Pillar 2 的 `CollectionScopedPgVectorStore`（ingestion 平台中央向量庫） |
| `cryptography>=42` | `anila_core.security.credential_crypto`（AES-GCM + PBKDF2，加解密 `user_llm_credentials`） |
| `aiosqlite>=0.20` | 預設 `sqlite_session` short-term session adapter |

### Optional extras

- **`[rag]`** — 文件解析重量級堆疊：`pymupdf4llm` / `pymupdf` / `python-docx` / `odfpy` / `striprtf` / `Pillow`。v0.14.0 後 `anila-agent/` 是純 starter template（無 production code 依賴），重量級 parser 實作落在 `anila_core.ingestion.parser_registry` 與 `anila_core.providers.vision`，需要時以 `pip install anila-core[rag]` 啟用。
- **`[dev]`** — `pytest` / `pytest-asyncio` / `pytest-cov` / `respx` / `ruff` / `mypy`。

### Console script

```
anila-core = anila_core.cli.main:main
```

---

## 目錄結構 / Layout

`src/anila_core/` 下的關鍵模組（依 Pillar 分組）：

```
anila-core/
├── pyproject.toml            # name=anila-core, v0.14.0
├── README.md / README.en.md  # 本檔 + English mirror
├── CHANGELOG.md              # 詳細 sprint release notes
├── e2e_smoke.py              # 手動 e2e smoke（需 OPENAI_API_KEY）
├── examples/
│   ├── router-mode/main.py
│   └── simple-agent/agent.py
├── tests/                    # pytest（含 integration 標記）
└── src/anila_core/
    ├── config.py             # Settings（pydantic-settings）
    ├── app_factory.py        # FastAPI app factory
    │
    ├── ──── Pillar 1 · agent runtime ────
    ├── api/                  # server / router_server（create_router_app）
    │   ├── events.py · caller_context.py · session_owner.py
    │   └── middleware/auth.py   # CSP service-token + rotating token
    ├── engine/               # query_engine（多階段 turn loop）+ budget_tracker
    │                         # + approvals / guardrails / handoff / lifecycle
    ├── coordinator/          # multi-step decomposition + sub-agent dispatch
    ├── router/               # tool_router（ToolRegistry、plan-mode / permission gate）
    ├── tools/                # dispatch_tool · ask_user · plan_mode · todo_write
    │                         # · agent_as_tool · files · shell · apply_patch
    ├── providers/            # base · openai_compat · cspplatform_provider
    │                         # · vision · mock · embedding_mock
    ├── memory/               # short_term/（Session Protocol + in_memory / sqlite）
    │                         # long_term/（adapter · embedding · extraction
    │                         #   · backends/{filesystem,postgres} · clients）
    │                         # + memdir · consolidation · relevance_selector · user
    ├── compact/              # micro / auto / session_memory / sliding_window
    ├── context/              # AgentContext（turn-scope contextvars）
    ├── post_turn/            # prompt_suggestion（follow-up chips）
    ├── tracing/              # span · tracer · processor · hooks
    ├── workspace/            # capability-scoped sandbox（workspace + caps + safe_path）
    ├── registry/             # agent_registry + remote_agent_manifest
    ├── runtime_config/       # snapshot · poller · apply（hot-reload）
    ├── models/               # pydantic DTOs（agent · message · memory · tool ·
    │                         #   storage · handoff · interrupt · ingestion）
    ├── cli/                  # init / register / status / bootstrap + templates/
    │
    └── ──── Pillar 2 · shared infrastructure ────
        ├── security/         # credential_crypto（AES-GCM + PBKDF2）+ url_guard（SSRF）
        ├── storage/
        │   ├── ports.py      # Protocol interfaces
        │   └── adapters/     # pg_pool · pgvector_store · memory_file_store
        └── ingestion/        # errors（IngestionError taxonomy）· parser_registry
            │                 # · parsers · docling_parser · ocr
            └── chunking_plugins/  # base（ChunkerStrategy）· registry
                               #   （@register_chunker）· builtins
```

> 完整模組責任與邊界依據請參考 [`../docs/anila-core/anila-core-boundary.md`](../docs/anila-core/anila-core-boundary.md)。

---

## 啟動與部署 / Setup & Run

anila-core 是 **library / SDK**，不是獨立常駐服務。它被 Router、agent 服務、ingestion-worker 以 import 方式消費，並透過 `create_router_app()` / `create_app()` 暴露 FastAPI app 供 uvicorn 啟動。

### 安裝

```bash
# 於 repo 根，editable install
pip install -e "./anila-core"          # 完整 Pillar 1 + Pillar 2 core deps
pip install -e "./anila-core[rag]"     # + 文件解析重量級堆疊
pip install -e "./anila-core[dev]"     # + pytest / ruff / mypy
pip install -e "./anila-core[rag,dev]" # 兩者皆裝（跑完整測試所需）
```

### Router 模式（OpenAI-compatible dispatcher）

```python
# main.py
from anila_core.api.router_server import create_router_app

app = create_router_app()
```

```bash
export CSP_BASE_URL=http://localhost:8000
export MODEL=gpt-4o-mini
uvicorn main:app --host 0.0.0.0 --port 9000
```

### 直接驅動一輪 QueryEngine（不走 FastAPI）

```python
from anila_core.engine.query_engine import QueryConfig, QueryEngine
from anila_core.providers.openai_compat import OpenAICompatProvider
from anila_core.router.tool_router import ToolRegistry
from anila_core.models.message import UserMessage

provider = OpenAICompatProvider(base_url="http://csp:8000/v1", api_key="sk-...")
engine = QueryEngine(provider=provider, tool_registry=ToolRegistry(), config=QueryConfig())

async for delta in engine.run_stream([UserMessage(content="say hi")]):
    print(delta)
```

### scaffold 一支新 agent

```bash
anila-core init my-agent   # 使用 anila_core/cli/templates/agent-template
cd my-agent
# 開始實作 tools / prompts / endpoints
```

### 測試

```bash
pip install -e "./anila-core[rag,dev]"
pytest                       # 預設 testpaths=["tests"]，asyncio_mode=auto
pytest --cov=src             # + coverage
pytest -m integration        # 需要 live pgvector + RLS database 的測試
```

---

## 與其他服務的關係 / Integration

| 消費者 | 使用範圍 | 取得什麼 |
|--------|----------|----------|
| **anila-core-router** | Pillar 1 + Pillar 2 | `create_router_app()`、QueryEngine、Coordinator、agent registry、service-token middleware |
| **anila-agent template**（fork 起點） | Pillar 1 + Pillar 2 + `[rag]` | 完整 runtime + 文件解析 / vision provider；自帶 RAG agent 程式碼 |
| **ingestion-worker**（Arq + Redis） | 僅 Pillar 2 | `chunking_plugins`、`IngestionError` taxonomy、`pg_pool`、`CollectionScopedPgVectorStore`、`credential_crypto` |
| **myCSPPlatform**（CSP backend） | Pillar 2（部分） | `credential_crypto`（建立時加密 `user_llm_credentials`）等共用 primitives |

anila-core 對外提供的能力面向：

- **storage adapters** — `storage/ports.py` Protocol + `adapters/`（pg pool、collection-scoped pgvector store、in-memory test store）。
- **ingestion primitives** — error taxonomy、可插拔 chunker registry、parser registry / OCR / vision（後者需 `[rag]`）。
- **tools** — dispatch、ask_user、plan_mode、todo_write、agent_as_tool、files / shell / apply_patch（workspace 沙箱化）。
- **api** — FastAPI Router server + agent server + 事件 / 認證 middleware。

---

## 相關文件 / Related docs

- anila-core 邊界（要留什麼、要刪什麼、判定原則）：[`../docs/anila-core/anila-core-boundary.md`](../docs/anila-core/anila-core-boundary.md)
- anila-core runtime 設計（session state、subsystem 責任）：[`../docs/anila-core/anila-core-runtime-design.md`](../docs/anila-core/anila-core-runtime-design.md)
- Ingestion 平台設計（中央化 ingestion，與 Pillar 2 同步）：[`../docs/ingestion/ingestion-platform-design.md`](../docs/ingestion/ingestion-platform-design.md)
- 詳細 sprint release notes：[`CHANGELOG.md`](./CHANGELOG.md)
- 官方 RAG agent template：[`../anila-agent/README.md`](../anila-agent/README.md)
- Router 薄殼部署：[`../anila-core-router/README.md`](../anila-core-router/README.md)
- 平台總覽：[`../README.md`](../README.md)

> 注意：`pyproject.toml` 標示 v0.14.0（`[rag]` extra 已恢復），而 `CHANGELOG.md` 最新條目為 v0.13.0；以 `pyproject.toml` 為版本與 extras 的權威來源。
