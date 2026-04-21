# anila-core

**ANILA Core** — Python agent runtime foundation（SDK）。

這是 ANILA 平台所有 agent 與 Router 共用的 **runtime 基座**。純 runtime，不綁 RAG、不綁特定向量庫、不綁特定模型供應商。RAG 相關的檔案解析、pgvector、向量檢索是 **樣板（AgenticRAG）** 才會用到的能力，透過 optional extras 提供。

- **Router 部署**：只裝 `anila-core`（不加 `[rag]`），image 精簡
- **Agent 開發者**：`pip install "anila-core[rag]"` + fork [`AgenticRAG`](../AgenticRAG/) 作為 RAG sample 起點
- **SDK 消費者**：`from anila_core.api.router_server import create_router_app`、`from anila_core.engine.query_engine import QueryEngine` 等

> Repo 根定位請看 [`../README.md`](../README.md)。Agent 開發 workflow 與 RAG 樣板請看 [`../AgenticRAG/README.md`](../AgenticRAG/README.md)。

---

## 套件邊界

```
┌─────────────────────────────────────────────────────────┐
│                    anila-core (本 package)               │
│                                                         │
│   api/             router_server / server / events /    │
│                    middleware（CSP service-token 驗證） │
│                                                         │
│   registry/        LocalAgentDefinition registry +      │
│                    RemoteAgentRegistry（從 CSP /v1/     │
│                    agents 同步 manifest）                │
│                                                         │
│   engine/          QueryEngine（7-stage turn loop）+    │
│                    budget_tracker                        │
│                                                         │
│   coordinator/     Multi-step task decomposition /      │
│                    sub-agent dispatch                    │
│                                                         │
│   tools/           dispatch_tool（agent 分派）+ prompts │
│                                                         │
│   providers/       Abstract base + OpenAICompat +       │
│                    CSPPlatform + mock                    │
│                                                         │
│   storage/         Protocol (ports.py) + in-memory      │
│                    adapter（MemoryFileStore）            │
│                                                         │
│   memory/          Memdir + extract_memories +          │
│                    relevance_selector + consolidation   │
│                                                         │
│   compact/         micro / auto / session_memory /      │
│                    sliding_window                        │
│                                                         │
│   context/         AgentContext（turn-scope 注入）      │
│                                                         │
│   models/          Pydantic models（agent / memory /    │
│                    message / storage / tool）            │
│                                                         │
│   router/          tool_router（ToolRegistry）          │
│                                                         │
│   cli/             `anila-core` dev CLI（init / status /│
│                    register）含 agent 模板              │
│                                                         │
│   config.py        Settings（pydantic-settings，env）   │
└─────────────────────────────────────────────────────────┘
       │                                   │
       │  import                            │  optional extras [rag]
       ▼                                   ▼
┌──────────────────────┐       ┌──────────────────────────┐
│  anila-core-router   │       │   AgenticRAG sample      │
│  (薄殼部署入口)       │       │   - api.py (RAG agent)   │
│  main.py:            │       │   - ingestion/ + pgvector│
│    app =             │       │   - index_documents.py   │
│    create_router_app │       │   - 下載作為新 agent 起點 │
└──────────────────────┘       └──────────────────────────┘
```

---

## 安裝

### 從 monorepo 來源安裝（推薦）

```bash
# 於 repo 根
pip install -e "./anila-core"                  # pure runtime
pip install -e "./anila-core[rag]"             # + 檔案解析 / pgvector / asyncpg
pip install -e "./anila-core[dev]"             # + pytest / ruff / mypy
pip install -e "./anila-core[rag,dev]"         # 全部
```

### 從 wheel 安裝（日後 CI 推到內部 PyPI 後）

```bash
pip install anila-core
```

---

## 最小使用範例

### 1. Router 模式（OpenAI-compatible dispatcher）

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

### 2. QueryEngine 直接跑一輪（不走 FastAPI）

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

### 3. 做自己的 agent（fork AgenticRAG 樣板）

細節見 [`../AgenticRAG/README.md`](../AgenticRAG/README.md)。anila-core 提供的 CLI 可以 scaffold：

```bash
anila-core init my-agent   # 用 anila_core/cli/templates/agent-template
cd my-agent
# 開始實作 tools / prompts / endpoints
```

---

## 執行測試

```bash
pip install -e ".[rag,dev]"
pytest                       # 預設 testpaths=["tests"]
pytest --cov=src             # + coverage
```

`tests/` 涵蓋：QueryEngine、Coordinator、Compact、Memory、Registry、Router server、CLI scaffolding、RAG tools、Chunker、Parsers、Ingestion service、Embedding mock、Dispatch tool。

---

## 檔案結構

```
anila-core/
├── pyproject.toml            # name=anila-core
├── README.md                 # 本檔
├── e2e_smoke.py              # 手動 e2e（需 OPENAI_API_KEY）
├── src/
│   └── anila_core/
│       ├── __init__.py
│       ├── config.py
│       ├── app_factory.py    # （含 RAG 預設 wiring；Task 3 會拆）
│       ├── api/
│       ├── cli/
│       ├── compact/
│       ├── context/
│       ├── coordinator/
│       ├── engine/
│       ├── ingestion/        # RAG（[rag] extras 才會用到）
│       ├── memory/
│       ├── models/
│       ├── providers/
│       ├── registry/
│       ├── router/
│       ├── storage/
│       └── tools/
├── tests/                    # pytest
└── examples/
    ├── router-mode/
    └── simple-agent/
```

> **Note (Task 3 pending)**：現階段 `ingestion/`、`storage/adapters/{pg_pool,pgvector_store,postgres_store}.py`、`providers/embedding_nvidia.py`、`engine/rag_preprocessor.py` 等仍留在 anila-core tree 裡（透過 `[rag]` extras 啟用）。下一輪會把這些 RAG-specific 檔案搬回 AgenticRAG 樣板，讓 core 真正成為 pure runtime。

---

## 相關文件

- 平台總覽：[`../README.md`](../README.md)
- Router 薄殼部署：[`../anila-core-router/README.md`](../anila-core-router/README.md)
- RAG sample agent 樣板：[`../AgenticRAG/README.md`](../AgenticRAG/README.md)
- 決策與路線圖：[`../anila_plan.md`](../anila_plan.md)

---

## License

見 repo 根 [`LICENSE`](../LICENSE)。
