# anila-core

> **ANILA Core** — Python agent runtime foundation (SDK). The in-process runtime base shared by every ANILA agent and the Router, plus the shared infrastructure used across the whole backend fleet.

> 中文版本：[`README.md`](./README.md)

> 🌿 **Branch note**: This SDK exists on every ANILA deployment branch and is identical across branches (the runtime base does not vary by deployment context). See the root [`README.md`](../../README.md) branch matrix and [`docs/branch-sync-backlog.md`](../../docs/branch-sync-backlog.md). New features always land in `main` first, then sync downstream.

---

## Overview

`anila-core` is ANILA's **Python runtime base**, positioned as pure runtime — not tied to a specific RAG pipeline, vector store, or model provider. It carries two pillars:

- **Pillar 1 · Agent runtime** — the in-process components the Router and each agent service need to handle a single chat turn (QueryEngine, Coordinator, providers, memory, tools, context, API server…).
- **Pillar 2 · Shared infrastructure** — base components not specific to one agent process, shared across the whole backend fleet (Router, agents, ingestion-worker, future batch workers): credential crypto, SSRF guard, pg / pgvector adapters, chunking plugins, ingestion error taxonomy.

How each role relates to anila-core:

- **Router deployment** ([`anila-core-router`](../../services/anila-core-router/)): directly `import`s Pillar 1 + Pillar 2.
- **Agent developers**: `pip install "anila-core[rag]"` and fork [`anila-agent`](../anila-agent/) as the official RAG agent starter template. The `[rag]` extra provides the heavyweight document-parsing packages.
- **ingestion-worker** (Arq async pipeline): consumes only Pillar 2 (`chunking_plugins`, `IngestionError`, `pg_pool`, `pgvector_store`, `credential_crypto`); never touches Pillar 1.

> Repo-root positioning: [`../../README.md`](../../README.md).

---

## Architecture & Stack

- **Language**: Python `>=3.11`
- **Build**: `hatchling` (package source `src/anila_core`)
- **Package / version**: `anila-core` v0.14.0 (`pyproject.toml` is authoritative for version & extras)

### Core dependencies (`pyproject.toml`)

| Package | Use |
|------|------|
| `pydantic>=2.0` / `pydantic-settings>=2.0` | DTO models + Settings (router-mode env loading) |
| `fastapi>=0.110` / `uvicorn[standard]>=0.29` | API server (Router / agent server) |
| `httpx>=0.27` | provider upstream calls |
| `sse-starlette>=2.0` | streaming (SSE turn streaming) |
| `python-frontmatter>=1.1` / `pyyaml>=6.0` | agent definition / config loading |
| `anyio>=4.0` / `aiofiles>=23.0` | async IO |
| `asyncpg>=0.29` / `pgvector>=0.3` | Pillar 2 `CollectionScopedPgVectorStore` (central ingestion vector store) |
| `cryptography>=42` | `security.credential_crypto` (AES-GCM + PBKDF2, encrypts `user_llm_credentials`) |
| `aiosqlite>=0.20` | default `sqlite_session` short-term session adapter |

### Optional extras

- **`[rag]`** — heavyweight parsing stack: `pymupdf4llm` / `pymupdf` / `python-docx` / `odfpy` / `striprtf` / `Pillow`. Since v0.14.0 `anila-agent/` is a pure starter template (no production-code dependency); the heavyweight parser implementations live in `anila_core.ingestion.parser_registry` and `anila_core.providers.vision`.
- **`[dev]`** — `pytest` / `pytest-asyncio` / `pytest-cov` / `respx` / `ruff` / `mypy`.

### Console script

```
anila-core = anila_core.cli.main:main
```

---

## Layout

Key modules under `src/anila_core/` (grouped by pillar):

```
packages/anila-core/
├── pyproject.toml            # name=anila-core, v0.14.0
├── README.md / README.en.md
├── CHANGELOG.md              # detailed sprint release notes
├── e2e_smoke.py              # manual e2e smoke (needs OPENAI_API_KEY)
├── examples/                 # router-mode / simple-agent
├── tests/                    # pytest (incl. integration markers)
└── src/anila_core/
    ├── config.py · app_factory.py
    │
    ├── ──── Pillar 1 · agent runtime ────
    ├── api/                  # server / router_server (create_router_app) + events
    │   ├── session_owner.py · caller_context.py   # resume session→agent table + Phase-3 CallerContext
    │   └── middleware/auth.py   # CSP service-token + rotating token
    ├── engine/               # query_engine (multi-stage turn loop) + budget_tracker
    │                         #   + approvals / guardrails / handoff / lifecycle
    ├── coordinator/          # multi-step decomposition + sub-agent dispatch
    ├── router/               # tool_router (ToolRegistry, plan-mode / permission gate)
    ├── tools/                # dispatch · ask_user · plan_mode · todo_write
    │                         #   · agent_as_tool · files · shell · apply_patch
    ├── providers/            # base · openai_compat · cspplatform_provider · vision · mock · embedding_mock
    ├── memory/               # short_term/ (Session Protocol + in_memory / sqlite)
    │                         # long_term/ (adapter · embedding · extraction
    │                         #   · backends/{filesystem,postgres} · clients)
    │                         # + memdir · consolidation · relevance_selector · user
    ├── compact/              # micro / auto / session_memory / sliding_window
    ├── context/ · post_turn/ · tracing/ · workspace/
    ├── registry/             # agent_registry + remote_agent_manifest
    ├── runtime_config/       # snapshot · poller · apply (hot-reload)
    ├── models/ · cli/        # pydantic DTOs · init/register/status/bootstrap + templates/
    │
    └── ──── Pillar 2 · shared infrastructure ────
        ├── security/         # credential_crypto (AES-GCM + PBKDF2) + url_guard (SSRF)
        ├── storage/          # ports.py (Protocol) + adapters/ (pg_pool · pgvector_store ...)
        └── ingestion/        # errors (IngestionError taxonomy) · parser_registry · parsers
            │                 #   · docling_parser · ocr · citation_extractor · relation_resolution
            └── chunking_plugins/  # base · registry (@register_chunker) · builtins
```

> Full module responsibilities & boundaries: [`../../docs/anila-core/anila-core-boundary.md`](../../docs/anila-core/anila-core-boundary.md).

---

## Setup & Run

anila-core is a **library / SDK**, not a long-running service. It's consumed by import (Router / agents / ingestion-worker) and exposes a FastAPI app via `create_router_app()` / `create_app()` for uvicorn.

### Install

```bash
pip install -e "./packages/anila-core"          # full Pillar 1 + Pillar 2 core deps
pip install -e "./packages/anila-core[rag]"     # + heavyweight parsing stack
pip install -e "./packages/anila-core[rag,dev]" # + pytest / ruff / mypy (needed to run the full test suite)
```

### Router mode (OpenAI-compatible dispatcher)

```python
# main.py
from anila_core.api.router_server import create_router_app
app = create_router_app()
```

```bash
export CSP_BASE_URL=http://localhost:8000
uvicorn main:app --host 0.0.0.0 --port 9000
```

### Drive one QueryEngine turn directly

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

# run() drives the full turn loop and returns a TurnResult; stream deltas arrive via the on_stream_delta callback
result = await engine.run([UserMessage(content="say hi")], on_stream_delta=on_delta)
print(result.stop_reason, result.turn_count)
```

> ⚠️ QueryEngine has **no** `run_stream()`; the entrypoint is `await engine.run(messages, on_stream_delta=...)` (see [`e2e_smoke.py`](./e2e_smoke.py)).

### Scaffold a new agent + tests

```bash
anila-core init my-agent     # uses cli/templates/agent-template
pytest                       # testpaths=["tests"], asyncio_mode=auto
pytest -m integration        # needs a live pgvector + RLS database
```

---

## Integration

| Consumer | Scope | What it gets |
|--------|----------|----------|
| **anila-core-router** | Pillar 1 + Pillar 2 | `create_router_app()`, QueryEngine, Coordinator, agent registry, service-token middleware |
| **anila-agent template** (fork point) | Pillar 1 + Pillar 2 + `[rag]` | full runtime + parsing / vision provider |
| **ingestion-worker** (Arq + Redis) | Pillar 2 only | `chunking_plugins`, `IngestionError`, `pg_pool`, `CollectionScopedPgVectorStore`, `credential_crypto` |
| **services/csp** (CSP backend) | Pillar 2 (partial) | `credential_crypto` (encrypts `user_llm_credentials`) and other shared primitives |

Capabilities offered: storage adapters (pg pool / pgvector store / in-memory test store), ingestion primitives (error taxonomy / chunker registry / parser / OCR / vision — the latter needs `[rag]`), tools (dispatch / ask_user / plan_mode / todo_write / agent_as_tool / files / shell / apply_patch), api (Router + agent server + event / auth middleware).

---

## Related docs

- anila-core boundary: [`../../docs/anila-core/anila-core-boundary.md`](../../docs/anila-core/anila-core-boundary.md)
- runtime design: [`../../docs/anila-core/anila-core-runtime-design.md`](../../docs/anila-core/anila-core-runtime-design.md)
- Ingestion platform design: [`../../docs/ingestion/ingestion-platform-design.md`](../../docs/ingestion/ingestion-platform-design.md)
- Release notes: [`CHANGELOG.md`](./CHANGELOG.md)
- RAG agent template: [`../anila-agent/README.md`](../anila-agent/README.md) · Router shell: [`../../services/anila-core-router/README.md`](../../services/anila-core-router/README.md)
- Platform: [`../../README.md`](../../README.md) · Branch strategy: [`../../docs/branch-sync-backlog.md`](../../docs/branch-sync-backlog.md)

> Version authority is `pyproject.toml` (v0.14.0, `[rag]` extra restored); the latest `CHANGELOG.md` entry is v0.13.0. ⚠️ Known inconsistency: `src/anila_core/__init__.py` still hard-codes `__version__ = "0.7.0"` (a code bug, not a README issue); reading `anila_core.__version__` programmatically returns the stale value. The CLI template's `requirements.txt` also still pins `anila-core>=0.1.0`.
