# anila-core

> **ANILA Core** — Python agent runtime foundation（SDK）。ANILA 平台所有 agent 與 Router 共用的 in-process runtime 基座，外加整個後端 fleet 共用的 shared infrastructure。

> English mirror：[`README.en.md`](./README.en.md)

> 🌿 **分支對照**：本 SDK 存在於所有 ANILA 部署分支，內容跨分支一致（runtime 基座不隨部署情境而異）。分支策略見根目錄 [`README.md`](../../README.md) 的分支對照表與 [`docs/branch-sync-backlog.md`](../../docs/branch-sync-backlog.md)。新功能一律先進 `main`，再 sync 進 downstream。

---

## 簡介

`anila-core` 是 ANILA 的 **Python runtime 基座**，定位為純 runtime — 不綁特定 RAG pipeline、不綁特定向量庫、不綁特定模型供應商。它承擔兩根支柱：

- **Pillar 1 · Agent runtime** — Router 與每支 agent 服務處理單一聊天 turn 所需的 in-process 元件（QueryEngine、Coordinator、providers、memory、tools、context、API server…）。
- **Pillar 2 · Shared infrastructure** — 不專屬於某個 agent process、被整個後端 fleet（Router、agent、ingestion-worker、未來各種 batch worker）共用的基底元件（credential crypto、SSRF guard、pg / pgvector adapters、chunking plugins、ingestion error taxonomy）。

平台各角色與 anila-core 的關係：

- **Router 部署**（[`anila-core-router`](../../services/anila-core-router/)）：直接 `import` Pillar 1 + Pillar 2。
- **Agent 開發者**：`pip install "anila-core[rag]"` 並 fork [`anila-agent`](../anila-agent/) 作為官方 RAG agent starter template。`[rag]` extra 提供文件解析的重量級套件。
- **ingestion-worker**（Arq 非同步 pipeline）：只消費 Pillar 2（`chunking_plugins`、`IngestionError`、`pg_pool`、`pgvector_store`、`credential_crypto`），不碰 Pillar 1。

> Repo 根定位見 [`../../README.md`](../../README.md)。

---

## 架構與技術棧

- **語言**：Python `>=3.11`
- **建置**：`hatchling`（套件來源 `src/anila_core`）
- **套件名 / 版本**：`anila-core` v0.14.0（以 `pyproject.toml` 為版本與 extras 的權威來源）

### Core dependencies（`pyproject.toml`）

| 套件 | 用途 |
|------|------|
| `pydantic>=2.0` / `pydantic-settings>=2.0` | DTO models + Settings（router-mode env loading） |
| `fastapi>=0.110` / `uvicorn[standard]>=0.29` | API server（Router / agent server） |
| `httpx>=0.27` | provider 上游呼叫 |
| `sse-starlette>=2.0` | streaming（SSE turn 串流） |
| `python-frontmatter>=1.1` / `pyyaml>=6.0` | agent definition / 設定載入 |
| `anyio>=4.0` / `aiofiles>=23.0` | async IO |
| `asyncpg>=0.29` / `pgvector>=0.3` | Pillar 2 `CollectionScopedPgVectorStore`（ingestion 中央向量庫） |
| `cryptography>=42` | `security.credential_crypto`（AES-GCM + PBKDF2，加解密 `user_llm_credentials`） |
| `aiosqlite>=0.20` | 預設 `sqlite_session` short-term session adapter |

### Optional extras

- **`[rag]`** — 文件解析重量級堆疊：`pymupdf4llm` / `pymupdf` / `python-docx` / `odfpy` / `striprtf` / `Pillow`。v0.14.0 後 `anila-agent/` 是純 starter template（無 production code 依賴），重量級 parser 實作落在 `anila_core.ingestion.parser_registry` 與 `anila_core.providers.vision`。
- **`[dev]`** — `pytest` / `pytest-asyncio` / `pytest-cov` / `respx` / `ruff` / `mypy`。

### Console script

```
anila-core = anila_core.cli.main:main
```

---

## 目錄結構

`src/anila_core/` 關鍵模組（依 Pillar 分組）：

```
packages/anila-core/
├── pyproject.toml            # name=anila-core, v0.14.0
├── README.md / README.en.md
├── CHANGELOG.md              # 詳細 sprint release notes
├── e2e_smoke.py              # 手動 e2e smoke（需 OPENAI_API_KEY）
├── examples/                 # router-mode / simple-agent
├── tests/                    # pytest（含 integration 標記）
└── src/anila_core/
    ├── config.py             # Settings（pydantic-settings）
    ├── app_factory.py        # FastAPI app factory
    │
    ├── ──── Pillar 1 · agent runtime ────
    ├── api/                  # server / router_server（create_router_app）+ events
    │   ├── session_owner.py · caller_context.py   # resume 用 session→agent 表 + Phase-3 CallerContext
    │   └── middleware/auth.py   # CSP service-token + rotating token
    ├── engine/               # query_engine（多階段 turn loop）+ budget_tracker
    │                         #   + approvals / guardrails / handoff / lifecycle
    ├── coordinator/          # multi-step decomposition + sub-agent dispatch
    ├── router/               # tool_router（ToolRegistry、plan-mode / permission gate）
    ├── tools/                # dispatch · ask_user · plan_mode · todo_write
    │                         #   · agent_as_tool · files · shell · apply_patch
    ├── providers/            # base · openai_compat · cspplatform_provider · vision · mock · embedding_mock
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
    ├── models/               # pydantic DTOs
    ├── cli/                  # init / register / status / bootstrap + templates/
    │
    └── ──── Pillar 2 · shared infrastructure ────
        ├── security/         # credential_crypto（AES-GCM + PBKDF2）+ url_guard（SSRF）
        ├── storage/          # ports.py（Protocol）+ adapters/（pg_pool · pgvector_store ...）
        └── ingestion/        # errors（IngestionError taxonomy）· parser_registry · parsers
            │                 #   · docling_parser · ocr · citation_extractor · relation_resolution
            └── chunking_plugins/  # base · registry（@register_chunker）· builtins
```

> 完整模組責任與邊界見 [`../../docs/anila-core/anila-core-boundary.md`](../../docs/anila-core/anila-core-boundary.md)。

---

## 啟動與部署

anila-core 是 **library / SDK**，不是常駐服務。它被 Router / agent / ingestion-worker 以 import 消費，並透過 `create_router_app()` / `create_app()` 暴露 FastAPI app 供 uvicorn 啟動。

### 安裝

```bash
pip install -e "./packages/anila-core"          # 完整 Pillar 1 + Pillar 2 core deps
pip install -e "./packages/anila-core[rag]"     # + 文件解析重量級堆疊
pip install -e "./packages/anila-core[rag,dev]" # + pytest / ruff / mypy（跑完整測試所需）
```

### Router 模式（OpenAI-compatible dispatcher）

```python
# main.py
from anila_core.api.router_server import create_router_app
app = create_router_app()
```

```bash
export CSP_BASE_URL=http://localhost:8000
uvicorn main:app --host 0.0.0.0 --port 9000
```

### 直接驅動一輪 QueryEngine

```python
from anila_core.engine.query_engine import QueryConfig, QueryEngine
from anila_core.providers.openai_compat import OpenAICompatProvider
from anila_core.router.tool_router import ToolRegistry
from anila_core.models.message import StreamDelta, UserMessage

provider = OpenAICompatProvider(base_url="http://csp:8000/v1", api_key="sk-...")
engine = QueryEngine(provider=provider, tool_registry=ToolRegistry(), config=QueryConfig())

async def on_delta(delta: StreamDelta) -> None:
    if delta.type == "text" and delta.text:
        print(delta.text, end="", flush=True)

# run() 跑完整輪迴並回傳 TurnResult；串流 delta 透過 on_stream_delta callback 送出
result = await engine.run([UserMessage(content="say hi")], on_stream_delta=on_delta)
print(result.stop_reason, result.turn_count)
```

> ⚠️ QueryEngine **沒有** `run_stream()`；入口是 `await engine.run(messages, on_stream_delta=...)`（見 [`e2e_smoke.py`](./e2e_smoke.py)）。

### scaffold 新 agent + 測試

```bash
anila-core init my-agent     # 使用 cli/templates/agent-template
pytest                       # testpaths=["tests"]，asyncio_mode=auto
pytest -m integration        # 需 live pgvector + RLS database
```

---

## 與其他服務的關係

| 消費者 | 使用範圍 | 取得什麼 |
|--------|----------|----------|
| **anila-core-router** | Pillar 1 + Pillar 2 | `create_router_app()`、QueryEngine、Coordinator、agent registry、service-token middleware |
| **anila-agent template**（fork 起點） | Pillar 1 + Pillar 2 + `[rag]` | 完整 runtime + 文件解析 / vision provider |
| **ingestion-worker**（Arq + Redis） | 僅 Pillar 2 | `chunking_plugins`、`IngestionError`、`pg_pool`、`CollectionScopedPgVectorStore`、`credential_crypto` |
| **services/csp**（CSP backend） | Pillar 2（部分） | `credential_crypto`（加密 `user_llm_credentials`）等共用 primitives |

對外能力面向：storage adapters（pg pool / pgvector store / in-memory test store）、ingestion primitives（error taxonomy / chunker registry / parser / OCR / vision，後者需 `[rag]`）、tools（dispatch / ask_user / plan_mode / todo_write / agent_as_tool / files / shell / apply_patch）、api（Router + agent server + 事件 / 認證 middleware）。

---

## 相關文件

- anila-core 邊界（留什麼、刪什麼）：[`../../docs/anila-core/anila-core-boundary.md`](../../docs/anila-core/anila-core-boundary.md)
- runtime 設計（session state / subsystem 責任）：[`../../docs/anila-core/anila-core-runtime-design.md`](../../docs/anila-core/anila-core-runtime-design.md)
- Ingestion 平台設計：[`../../docs/ingestion/ingestion-platform-design.md`](../../docs/ingestion/ingestion-platform-design.md)
- 詳細 release notes：[`CHANGELOG.md`](./CHANGELOG.md)
- 官方 RAG agent template：[`../anila-agent/README.md`](../anila-agent/README.md) · Router 薄殼：[`../../services/anila-core-router/README.md`](../../services/anila-core-router/README.md)
- 平台總覽：[`../../README.md`](../../README.md) · 分支策略：[`../../docs/branch-sync-backlog.md`](../../docs/branch-sync-backlog.md)

> 版本以 `pyproject.toml`（v0.14.0，`[rag]` extra 已恢復）為權威；`CHANGELOG.md` 最新條目為 v0.13.0。⚠️ 已知不一致：`src/anila_core/__init__.py` 的 `__version__` 仍寫死 `"0.7.0"`（程式碼 bug，非 README）；以程式設計方式讀 `anila_core.__version__` 會拿到舊值。CLI template 的 `requirements.txt` 也仍 pin `anila-core>=0.1.0`。
