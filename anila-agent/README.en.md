# anila-agent

> Agentic RAG starter: openai-agents SDK for the runtime, with Claude Code's harness engineering (long-term memdir, hook surface, slash-command CLI) ported on top.

[繁體中文](README.md) · **English**

## 簡介 / Overview

`anila-agent` is an Agentic RAG starter project you can clone and run. The runtime is built on the [openai-agents SDK](https://github.com/openai/openai-agents-python), with the harness engineering ported wholesale from Claude Code.

Fill in your retriever / prompts / tools, point it at any OpenAI-compatible endpoint, and talk.

Within the ANILA platform this project plays two roles at once:

- **Standalone starter** — anyone clones it and turns it into their own agent.
- **Official platform template** — the whole directory is mounted into the CSP platform as a downloadable template (see §Integration).

## 架構與技術棧 / Architecture & Stack

- **Language / runtime**: Python `>=3.10`, managed via `pyproject.toml` (hatchling build backend).
- **Core dependencies** (`[project].dependencies`):
  - `openai-agents[litellm]>=0.3.0` — agent runtime (`Agent` + `Runner`) and the LiteLLM model provider.
  - `pydantic>=2.7`, `pyyaml>=6.0`, `python-dotenv>=1.0` — schemas, config files, `.env` loading.
  - `prompt-toolkit>=3.0`, `rich>=13.0` — REPL CLI and terminal output.
- **Optional dependencies**:
  - `pgvector` extra — `langchain-postgres`, `langchain-openai`, `psycopg[binary]`, enabling the two built-in pgvector retrievers.
  - `dev` extra — `pytest`, `pytest-asyncio`, `pytest-cov`, `ruff`, `mypy`.
- **Entry point**: `anila = anila_agent.main:main` (run `anila` to start the REPL once installed).

Layers and where each pattern comes from:

| Layer | Module | Source pattern |
|-------|--------|----------------|
| Runtime | `core/agent.py`, `core/runner.py` | openai-agents `Agent` + `Runner`; `AnilaRunner` adds hook firing, abort handling, an event stream |
| Hooks | `core/hooks.py` | Claude Code `PreToolUse` / `PostToolUse` / `Stop` / `SessionStart` / `UserPromptSubmit` |
| Events | `core/events.py` | In-process pub/sub (separate from openai-agents tracing) |
| Long-term memory | `memory/store.py`, `memory/long_term.py` | Direct port of Claude Code `memdir/` (file-backed `MEMORY.md` index + `*.md` topic files with YAML frontmatter, four types: `user` / `feedback` / `project` / `reference`) |
| Short-term memory | `memory/short_term.py` | Wrap of openai-agents `SQLiteSession` |
| Auto extraction | `memory/summarizer.py` | Off by default; Stop-hook driven side-LLM extractor (port of Claude Code `extractMemories`) |
| Retrieval | `retrieval/base.py`, `retrieval/dummy.py` | Protocol you implement; defaults to the in-memory token-overlap `DummyRetriever` |
| pgvector (generic) | `retrieval/pgvector.py` | `langchain_postgres`-backed; one-line env config |
| pgvector (ANILA platform) | `retrieval/anila_pgvector.py` | Native `ingestion_collections` / `document_chunks` schema with halfvec + RLS |
| Tools | `tools/base.py`, `tools/rag_tools.py`, `tools/filesystem_tools.py` | `@function_tool` + Anila metadata (`is_read_only`, `is_destructive`, …) |
| Models | `models/openai_compatible.py` | LiteLLM-backed; works with vLLM / Ollama / OpenAI / Together / etc. |
| CLI | `cli/app.py`, `cli/commands.py`, `cli/renderer.py` | `prompt_toolkit` + `rich`. Slash commands ported from Claude Code `commands.ts` |

## 目錄結構 / Layout

```
anila-agent/
├── pyproject.toml
├── README.md                    # 繁體中文 (primary)
├── README.en.md                 # English (mirror)
├── CHANGELOG.md
├── LICENSE
├── .env.example
├── configs/
│   ├── agent.yaml               # name, instructions file, max turns, tool-use behaviour
│   ├── model.yaml               # model, base URL, sampling defaults
│   ├── memory.yaml              # short-term + long-term + auto-extraction
│   └── tools.yaml               # built-in tool list, hook registration, MCP servers
├── anila_agent/
│   ├── main.py                  # CLI entrypoint (builds AnilaRunner)
│   ├── cli/
│   │   ├── app.py               # REPL loop
│   │   ├── commands.py          # slash-command parser
│   │   └── renderer.py          # terminal output
│   ├── core/
│   │   ├── agent.py             # Agent assembly (build_agent / AssembledAgent)
│   │   ├── runner.py            # Tool loop wrapper (AnilaRunner)
│   │   ├── hooks.py             # Hook surface
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
│   │   ├── dummy.py             # in-memory token-overlap (default)
│   │   ├── pgvector.py          # langchain_postgres-backed
│   │   └── anila_pgvector.py    # ANILA platform native schema
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
    ├── test_pgvector_retriever.py        # langchain flavour
    └── test_anila_pgvector_retriever.py  # platform-native flavour
```

## 啟動與部署 / Setup & Run

```bash
git clone https://github.com/zzw09773/anila-agent.git
cd anila-agent

# Install. Either uv (preferred) or pip.
uv venv && uv pip install -e ".[dev]"
# or: python -m venv .venv && source .venv/bin/activate && pip install -e ".[dev]"

# Add the pgvector extra if you'll use one of the built-in pgvector retrievers
# (otherwise stay on DummyRetriever).
uv pip install -e ".[dev,pgvector]"

# Point at your OpenAI-compatible endpoint.
cp .env.example .env
# edit .env: ANILA_BASE_URL, ANILA_API_KEY, ANILA_MODEL

# Run the REPL.
anila
# or one-shot:
anila --prompt "hello"
```

### Filling in your project

Three things to customise. Everything else can stay as-is.

**1. Prompts** — Edit `anila_agent/prompts/system.md`. The system prompt is loaded at agent assembly time and can reference retrieval and memory directly. A fresh clone ships TODO placeholders (the Anila identity line was removed in v0.2.1); fill in your own.

**2. Retriever** — Three options, in increasing order of work:

- **Option A — generic pgvector via env (zero code).** Install the extra, set `PGVECTOR_URL` + `PGVECTOR_COLLECTION`, and `build_agent()` auto-installs the retriever. Backed by `langchain_postgres.PGVector` (`langchain_pg_collection` + `langchain_pg_embedding` tables).
- **Option B — ANILA platform schema via env (zero code).** Set `PGVECTOR_URL` + `ANILA_COLLECTION_ID` to talk directly to the platform's native `ingestion_collections` + `document_chunks` tables (halfvec + RLS via the `anila.collection_id` GUC). Embedding dimension is auto-detected from the collection row. Activation precedence in `build_agent()`: `ANILA_COLLECTION_ID` → `PGVECTOR_COLLECTION` → `DummyRetriever`; half-configured deployments fail loud rather than silently fall back.
- **Option C — your own backend.** Implement `Retriever` in `anila_agent/retrieval/base.py`, install it with `set_retriever()` before constructing the agent. The built-in `search_documents` / `read_document` tools route through it automatically. See `examples/rag_agent.py`.

```python
from anila_agent.tools.rag_tools import set_retriever
set_retriever(MyRetriever())
```

**3. Tools** — Decorate a function or list it in `configs/tools.yaml`:

```python
from anila_agent.tools.base import anila_tool

@anila_tool(is_read_only=True, category="domain")
def employee_count(department: str) -> int: ...
```

```yaml
builtin:
  - mypkg.tools.employee_count
```

See `examples/custom_tool.py`.

### Hooks

Hooks fire around model and tool events. Each callback returns a `HookOutput` and is registered in `configs/tools.yaml`:

```yaml
hooks:
  pre_tool_use:
    - { matcher: "write_.*", callback: mypkg.hooks.deny_writes }
```

Available events: `pre_tool_use` (block, rewrite input, inject context), `post_tool_use` (observe output, inject context for next turn), `stop` (fires when the agent produces a final output).

### Memory

- **Long-term (memdir)** — File-backed at `<ANILA_HOME>/memory/`: a `MEMORY.md` index (capped at 200 lines / 25 KB) plus `*.md` topic files with YAML frontmatter (`name` / `description` / `type`). Recall scans the directory, hands the manifest to a small LLM call, and returns the selected files.
- **Auto extraction (off by default)** — Set `auto_memory.enabled: true` in `memory.yaml`; a Stop hook then runs an extractor side-call at end of turn and writes proposals as new memory files. Disable for predictable per-turn cost.
- **Short-term** — SQLite-backed via openai-agents `SQLiteSession`, stored at `<ANILA_HOME>/sessions/anila.db`. Reusing the same `--session` ID resumes the conversation.

### Slash commands (in the REPL)

| Command | Effect |
|---------|--------|
| `/help` | List commands |
| `/clear` | Clear short-term session history |
| `/memory list` | Show MEMORY.md index |
| `/memory scan` | Show full memory file manifest |
| `/memory extract` | Force an auto-extraction pass (when enabled) |
| `/model` | Show active model |
| `/cost` | Show session metrics |
| `/exit` | Quit |

Add your own in `anila_agent/cli/commands.py`.

### Configuration & environment

`configs/` holds four YAML files (`agent.yaml` / `model.yaml` / `memory.yaml` / `tools.yaml`). Environment overrides (in `.env` or shell):

| Variable | Purpose |
|----------|---------|
| `ANILA_BASE_URL` | OpenAI-compatible endpoint |
| `ANILA_API_KEY` | Token for the endpoint |
| `ANILA_MODEL` | Model name |
| `ANILA_HOME` | State directory (default `./.anila`) |
| `ANILA_AUTO_MEMORY` | `1` to override `memory.yaml` and enable auto extraction |
| `ANILA_LOG_LEVEL` | Logging level |
| `PGVECTOR_URL` | Postgres DSN for either pgvector retriever |
| `PGVECTOR_COLLECTION` | Collection **name** — activates the langchain-postgres retriever |
| `ANILA_COLLECTION_ID` | Collection **id** (int) — activates the ANILA-platform retriever; takes precedence over `PGVECTOR_COLLECTION` |
| `ANILA_EMBED_MODEL` | Embedding model name (default `text-embedding-3-small`) |
| `ANILA_EMBED_BASE_URL` | Embedding endpoint; falls back to `ANILA_BASE_URL` |
| `ANILA_EMBED_API_KEY` | Embedding key; falls back to `ANILA_API_KEY` |
| `ANILA_SSL_VERIFY` | `0` to skip TLS verification (self-signed certs only) |

### Tests

```bash
pytest
```

Coverage is intentionally focused on the harness layer (memdir port, hook bridge, retriever scoring) — the openai-agents primitives have their own test suite.

## 與其他服務的關係 / Integration

- **CSP platform template**: the whole `anila-agent` directory is mounted into the platform container by `docker-compose.yml` / `docker-compose-dev.yml` as `./anila-agent:/app/anila-template:ro`, pointed at by `ANILA_TEMPLATE_DIR=/app/anila-template`. The CSP backend (`myCSPPlatform/backend/app/api/agents.py`) reads this directory and, at the `/template/download` endpoint, packages it into `anila-core-template.zip` for developers — i.e. "one source tree that is both the starter and the platform template."
- **anila-core**: the CLI bootstrap command (`anila-core/src/anila_core/cli/bootstrap_cmd.py`, `templates/agent-template/`) also references the same template to scaffold new agents; CSP and anila-core share the `ANILA_TEMPLATE_DIR` convention.
- **Agent framework**: this project's v0.1 implementation aligns with the agent-framework synthesis architecture (a blend of openai-agents + Claude Code patterns) rather than a verbatim port. The runtime stays on the upstream openai-agents SDK; the harness (hooks / memory / CLI) is the ported layer.

> Updating the `anila-agent` subtree goes through git subtree pull/push (see the "Updating anila-agent" section in the platform root README).

## 相關文件 / Related docs

- [`../docs/agent-framework/anila-agent-framework-architecture.md`](../docs/agent-framework/anila-agent-framework-architecture.md) — framework architecture (synthesis design, canonical)
- [`../docs/agent-framework/anila-agent-framework-porting-decisions.md`](../docs/agent-framework/anila-agent-framework-porting-decisions.md) — porting decisions (SUPERSEDED, still useful as a source-by-source reference for each upstream subsystem)
- See [CHANGELOG.md](CHANGELOG.md) for release notes.

## License

Apache-2.0. See [`LICENSE`](LICENSE).
