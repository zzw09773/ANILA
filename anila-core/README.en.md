# anila-core

**ANILA Core** — Python agent runtime foundation (SDK). The in-process runtime base shared by every ANILA agent and the Router, plus the shared infrastructure consumed by the whole backend fleet.

> 📌 **This file is on the `prod` branch (NCSIST intranet deployment).** SDK contents identical to main (not in the fork zone).

---

## Overview

`anila-core` is the **Python runtime foundation** of the ANILA platform. It is positioned as pure runtime — not tied to a specific RAG pipeline, vector store, or model provider. It carries two responsibilities:

- **Pillar 1 · Agent runtime** — the in-process components the Router and each agent service need to handle a single chat turn (QueryEngine, Coordinator, providers, memory, tools, context, API server…).
- **Pillar 2 · Shared infrastructure** — base components that do not belong to any single agent process and are shared across the ANILA backend fleet (Router, agents, ingestion-worker, and future batch workers): credential crypto, SSRF guard, pg / pgvector adapters, chunking plugins, ingestion error taxonomy.

How each platform role relates to anila-core:

- **Router deployment** (`anila-core-router`): imports Pillar 1 + Pillar 2 directly.
- **Agent developers**: `pip install "anila-core[rag]"` and fork [`anila-agent`](../anila-agent/) as the official RAG agent starter template. The `[rag]` extra pulls in the heavy document-parsing stack (pymupdf / python-docx / odfpy / striprtf / Pillow).
- **ingestion-worker** (Arq async pipeline): consumes Pillar 2 only (`chunking_plugins`, `IngestionError`, `pg_pool`, `pgvector_store`, `credential_crypto`); it does not touch Pillar 1.

> For repo-root positioning see [`../README.md`](../README.md). Traditional Chinese primary: [`README.md`](./README.md).

---

## Architecture & Stack

- **Language**: Python `>=3.11`
- **Build**: `hatchling` (package source `src/anila_core`)
- **Package / version**: `anila-core` v0.14.0

### Core dependencies (`pyproject.toml`)

| Package | Purpose |
|---------|---------|
| `pydantic>=2.0` / `pydantic-settings>=2.0` | DTO models + Settings (router-mode env loading) |
| `fastapi>=0.110` / `uvicorn[standard]>=0.29` | API server (Router / agent server) |
| `httpx>=0.27` | upstream provider calls |
| `sse-starlette>=2.0` | streaming (SSE turn streaming) |
| `python-frontmatter>=1.1` / `pyyaml>=6.0` | agent definition / config loading |
| `anyio>=4.0` / `aiofiles>=23.0` | async IO |
| `asyncpg>=0.29` / `pgvector>=0.3` | Pillar 2 `CollectionScopedPgVectorStore` (ingestion platform central vector store) |
| `cryptography>=42` | `anila_core.security.credential_crypto` (AES-GCM + PBKDF2, encrypts/decrypts `user_llm_credentials`) |
| `aiosqlite>=0.20` | default `sqlite_session` short-term session adapter |

### Optional extras

- **`[rag]`** — heavy document-parsing stack: `pymupdf4llm` / `pymupdf` / `python-docx` / `odfpy` / `striprtf` / `Pillow`. As of v0.14.0 `anila-agent/` is a pure starter template (no production code depends on it); the heavy parser implementation lives at `anila_core.ingestion.parser_registry` and `anila_core.providers.vision`, enabled on demand via `pip install anila-core[rag]`.
- **`[dev]`** — `pytest` / `pytest-asyncio` / `pytest-cov` / `respx` / `ruff` / `mypy`.

### Console script

```
anila-core = anila_core.cli.main:main
```

---

## Layout

Key modules under `src/anila_core/`, grouped by Pillar:

```
anila-core/
├── pyproject.toml            # name=anila-core, v0.14.0
├── README.md / README.en.md  # zh-TW primary + English mirror
├── CHANGELOG.md              # detailed sprint release notes
├── e2e_smoke.py              # manual e2e smoke (needs OPENAI_API_KEY)
├── examples/
│   ├── router-mode/main.py
│   └── simple-agent/agent.py
├── tests/                    # pytest (includes integration marker)
└── src/anila_core/
    ├── config.py             # Settings (pydantic-settings)
    ├── app_factory.py        # FastAPI app factory
    │
    ├── ──── Pillar 1 · agent runtime ────
    ├── api/                  # server / router_server (create_router_app)
    │   ├── events.py · caller_context.py · session_owner.py
    │   └── middleware/auth.py   # CSP service-token + rotating token
    ├── engine/               # query_engine (multi-stage turn loop) + budget_tracker
    │                         # + approvals / guardrails / handoff / lifecycle
    ├── coordinator/          # multi-step decomposition + sub-agent dispatch
    ├── router/               # tool_router (ToolRegistry, plan-mode / permission gate)
    ├── tools/                # dispatch_tool · ask_user · plan_mode · todo_write
    │                         # · agent_as_tool · files · shell · apply_patch
    ├── providers/            # base · openai_compat · cspplatform_provider
    │                         # · vision · mock · embedding_mock
    ├── memory/               # short_term/ (Session Protocol + in_memory / sqlite)
    │                         # long_term/ (adapter · embedding · extraction
    │                         #   · backends/{filesystem,postgres} · clients)
    │                         # + memdir · consolidation · relevance_selector · user
    ├── compact/              # micro / auto / session_memory / sliding_window
    ├── context/              # AgentContext (turn-scope contextvars)
    ├── post_turn/            # prompt_suggestion (follow-up chips)
    ├── tracing/              # span · tracer · processor · hooks
    ├── workspace/            # capability-scoped sandbox (workspace + caps + safe_path)
    ├── registry/             # agent_registry + remote_agent_manifest
    ├── runtime_config/       # snapshot · poller · apply (hot-reload)
    ├── models/               # pydantic DTOs (agent · message · memory · tool ·
    │                         #   storage · handoff · interrupt · ingestion)
    ├── cli/                  # init / register / status / bootstrap + templates/
    │
    └── ──── Pillar 2 · shared infrastructure ────
        ├── security/         # credential_crypto (AES-GCM + PBKDF2) + url_guard (SSRF)
        ├── storage/
        │   ├── ports.py      # Protocol interfaces
        │   └── adapters/     # pg_pool · pgvector_store · memory_file_store
        └── ingestion/        # errors (IngestionError taxonomy) · parser_registry
            │                 # · parsers · docling_parser · ocr
            └── chunking_plugins/  # base (ChunkerStrategy) · registry
                               #   (@register_chunker) · builtins
```

> For full module responsibilities and boundary rationale, see [`../docs/anila-core/anila-core-boundary.md`](../docs/anila-core/anila-core-boundary.md).

---

## Setup & Run

anila-core is a **library / SDK**, not a standalone long-running service. It is consumed by the Router, agent services, and ingestion-worker via import, and exposes FastAPI apps through `create_router_app()` / `create_app()` for uvicorn to serve.

### Install

```bash
# editable install from repo root
pip install -e "./anila-core"          # full Pillar 1 + Pillar 2 core deps
pip install -e "./anila-core[rag]"     # + heavy document-parsing stack
pip install -e "./anila-core[dev]"     # + pytest / ruff / mypy
pip install -e "./anila-core[rag,dev]" # both (required to run the full test suite)
```

### Router mode (OpenAI-compatible dispatcher)

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

### Drive one QueryEngine turn directly (no FastAPI)

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

### Scaffold a new agent

```bash
anila-core init my-agent   # uses anila_core/cli/templates/agent-template
cd my-agent
# start implementing tools / prompts / endpoints
```

### Tests

```bash
pip install -e "./anila-core[rag,dev]"
pytest                       # default testpaths=["tests"], asyncio_mode=auto
pytest --cov=src             # + coverage
pytest -m integration        # tests requiring a live pgvector + RLS database
```

---

## Integration

| Consumer | Scope | What it gets |
|----------|-------|--------------|
| **anila-core-router** | Pillar 1 + Pillar 2 | `create_router_app()`, QueryEngine, Coordinator, agent registry, service-token middleware |
| **anila-agent template** (fork starting point) | Pillar 1 + Pillar 2 + `[rag]` | full runtime + document parsing / vision provider; ships its own RAG agent code |
| **ingestion-worker** (Arq + Redis) | Pillar 2 only | `chunking_plugins`, `IngestionError` taxonomy, `pg_pool`, `CollectionScopedPgVectorStore`, `credential_crypto` |
| **myCSPPlatform** (CSP backend) | Pillar 2 (partial) | `credential_crypto` (encrypt `user_llm_credentials` at create time) and other shared primitives |

Capabilities anila-core provides outward:

- **storage adapters** — `storage/ports.py` Protocols + `adapters/` (pg pool, collection-scoped pgvector store, in-memory test store).
- **ingestion primitives** — error taxonomy, pluggable chunker registry, parser registry / OCR / vision (the latter requires `[rag]`).
- **tools** — dispatch, ask_user, plan_mode, todo_write, agent_as_tool, files / shell / apply_patch (workspace-sandboxed).
- **api** — FastAPI Router server + agent server + event / auth middleware.

---

## Related docs

- anila-core boundary (what stays, what is removed, decision rules): [`../docs/anila-core/anila-core-boundary.md`](../docs/anila-core/anila-core-boundary.md)
- anila-core runtime design (session state, subsystem responsibilities): [`../docs/anila-core/anila-core-runtime-design.md`](../docs/anila-core/anila-core-runtime-design.md)
- Ingestion platform design (centralized ingestion, synced with Pillar 2): [`../docs/ingestion/ingestion-platform-design.md`](../docs/ingestion/ingestion-platform-design.md)
- Detailed sprint release notes: [`CHANGELOG.md`](./CHANGELOG.md)
- Official RAG agent template: [`../anila-agent/README.md`](../anila-agent/README.md)
- Router thin-shell deployment: [`../anila-core-router/README.md`](../anila-core-router/README.md)
- Platform overview: [`../README.md`](../README.md)

> Note: `pyproject.toml` declares v0.14.0 (the `[rag]` extra has been restored), while the latest `CHANGELOG.md` entry is v0.13.0; treat `pyproject.toml` as the authoritative source for version and extras.

---

**Last updated**: 2026-05-26 (sync PR #16 + add prod banner; SDK contents identical to main)
