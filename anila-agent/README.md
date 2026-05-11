# anila-agent

**English** · [繁體中文](README.zh-TW.md)

Agentic RAG starter project. Built on the [openai-agents SDK](https://github.com/openai/openai-agents-python) for the runtime, with the harness engineering (long-term memdir, hook surface, slash-command CLI) ported from Claude Code.

Clone, fill in your retriever / prompts / tools, talk to your local OpenAI-compatible endpoint.

## What's inside

| Layer | Module | Source pattern |
|-------|--------|----------------|
| Runtime | `core/agent.py`, `core/runner.py` | openai-agents `Agent` + `Runner` |
| Hooks | `core/hooks.py` | Claude Code `PreToolUse` / `PostToolUse` / `Stop` / `SessionStart` / `UserPromptSubmit` |
| Events | `core/events.py` | In-process pub/sub (separate from openai-agents tracing) |
| Long-term memory | `memory/store.py`, `memory/long_term.py` | Direct port of Claude Code `memdir/` (file-backed `MEMORY.md` index + `*.md` topic files with YAML frontmatter, four types: `user` / `feedback` / `project` / `reference`) |
| Short-term memory | `memory/short_term.py` | Wrap of openai-agents `SQLiteSession` |
| Auto extraction | `memory/summarizer.py` | Off by default; Stop-hook driven side-LLM extractor (port of Claude Code `extractMemories`) |
| Retrieval | `retrieval/base.py`, `retrieval/dummy.py` | Protocol you implement |
| pgvector (generic) | `retrieval/pgvector.py` | langchain_postgres-backed; one-line env config |
| pgvector (ANILA platform) | `retrieval/anila_pgvector.py` | Native `ingestion_collections` / `document_chunks` schema with halfvec + RLS |
| Tools | `tools/base.py`, `tools/rag_tools.py`, `tools/filesystem_tools.py` | `@function_tool` + Anila metadata (`is_read_only`, `is_destructive`, …) |
| Models | `models/openai_compatible.py` | LiteLLM-backed; works with vLLM / Ollama / OpenAI / Together / etc. |
| CLI | `cli/app.py`, `cli/commands.py`, `cli/renderer.py` | `prompt_toolkit` + `rich`. Slash commands ported from Claude Code `commands.ts` |

## Quickstart

```bash
git clone https://github.com/zzw09773/anila-agent.git
cd anila-agent

# Install. Either uv (preferred) or pip.
uv venv && uv pip install -e ".[dev]"
# or: python -m venv .venv && .venv/Scripts/activate && pip install -e ".[dev]"

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

## Filling in your project

Three things to customise. Everything else can stay as-is.

### 1. Prompts

Edit `anila_agent/prompts/system.md`. The system prompt is loaded at agent assembly time; it can reference retrieval and memory directly.

### 2. Retriever

Three options, in increasing order of work.

**Option A — generic pgvector via env (zero code).** Install the optional
extra and set two env vars; `build_agent()` auto-installs the retriever:

```bash
uv pip install -e ".[pgvector]"
```

```env
PGVECTOR_URL=postgresql+psycopg2://user:pass@host:5432/db
PGVECTOR_COLLECTION=my_docs
# optional — embed endpoint defaults to ANILA_BASE_URL / ANILA_API_KEY
ANILA_EMBED_MODEL=text-embedding-3-small
```

Backed by `langchain_postgres.PGVector`. Use this when your data was
ingested with langchain (`langchain_pg_collection` + `langchain_pg_embedding`
tables).

**Option B — ANILA platform schema via env (zero code).** Same idea, but
talks directly to the platform's native `ingestion_collections` +
`document_chunks` tables (halfvec + RLS via `anila.collection_id` GUC).
Embedding dimension is auto-detected from the collection row.

```env
PGVECTOR_URL=postgresql://<user>:<password>@<host>:<port>/<db>
ANILA_COLLECTION_ID=<int collection id>
ANILA_EMBED_MODEL=nvidia/NV-embed-V2
ANILA_SSL_VERIFY=0   # only if your embed endpoint uses a self-signed cert
```

Activation precedence in `build_agent()`: `ANILA_COLLECTION_ID` →
`PGVECTOR_COLLECTION` → `DummyRetriever`. Half-configured deployments
(e.g. `ANILA_COLLECTION_ID` set but `PGVECTOR_URL` missing) fail loud
rather than silently fall back.

**Option C — your own backend.** Implement `Retriever` in
`anila_agent/retrieval/base.py`:

```python
from anila_agent.retrieval.base import Retriever
from anila_agent.models.schemas import Document

class MyRetriever:
    @property
    def name(self) -> str: return "mine"
    async def search(self, query: str, k: int = 5) -> list[Document]: ...
    async def fetch(self, doc_id: str) -> Document | None: ...
```

Install it before constructing the agent:

```python
from anila_agent.tools.rag_tools import set_retriever
set_retriever(MyRetriever())
```

The built-in `search_documents` and `read_document` tools route through it automatically. See `examples/rag_agent.py`.

### 3. Tools

Either decorate a function:

```python
from anila_agent.tools.base import anila_tool

@anila_tool(is_read_only=True, category="domain")
def employee_count(department: str) -> int: ...
```

…or list it in `configs/tools.yaml`:

```yaml
builtin:
  - mypkg.tools.employee_count
```

See `examples/custom_tool.py`.

## Hooks

Hooks fire around model and tool events. Each callback returns a `HookOutput`:

```python
from anila_agent.core.hooks import HookOutput, PreToolUseInput

async def deny_writes(payload: PreToolUseInput) -> HookOutput:
    if payload.tool_name.startswith("write_"):
        return HookOutput(decision="block", reason="read-only mode")
    return HookOutput()
```

Register in `configs/tools.yaml`:

```yaml
hooks:
  pre_tool_use:
    - { matcher: "write_.*", callback: mypkg.hooks.deny_writes }
```

Available events:
- `pre_tool_use` — block, rewrite input, inject context
- `post_tool_use` — observe output, inject context for next turn
- `stop` — fires when the agent produces a final output

## Memory

### Long-term (memdir)

File-backed at `<ANILA_HOME>/memory/`. Layout:

```
memory/
  MEMORY.md              ← index, capped at 200 lines / 25 KB
  user_role.md           ← topic file with YAML frontmatter
  feedback_testing.md
  project_release.md
```

Each topic file:

```markdown
---
name: short title
description: one-line description used by the recall selector
type: user|feedback|project|reference
---

free-form markdown content
```

Recall scans the directory, hands the manifest to a small LLM call, and returns the selected files.

### Auto extraction (off by default)

Set `memory.yaml`:

```yaml
auto_memory:
  enabled: true
  min_messages_between_runs: 4
```

When enabled, a Stop hook runs an extractor side-call at end of turn and writes proposals as new memory files. Disable for predictable per-turn cost.

### Short-term

SQLite-backed via openai-agents `SQLiteSession`. Stored at `<ANILA_HOME>/sessions/anila.db`. Reusing the same `--session` ID resumes the conversation.

## Slash commands

In the REPL:

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

## Configuration

Four YAML files in `configs/`:

- `agent.yaml` — name, instructions file, max turns, tool-use behaviour
- `model.yaml` — model, base URL, sampling defaults
- `memory.yaml` — short-term + long-term + auto-extraction
- `tools.yaml` — built-in tool list, hook registration, MCP servers

Environment overrides (in `.env` or shell):

| Variable | Purpose |
|----------|---------|
| `ANILA_BASE_URL` | OpenAI-compatible endpoint |
| `ANILA_API_KEY` | Token for the endpoint |
| `ANILA_MODEL` | Model name |
| `ANILA_HOME` | State directory (default `./.anila`) |
| `ANILA_AUTO_MEMORY` | `1` to override `memory.yaml` and enable auto extraction |
| `ANILA_LOG_LEVEL` | Logging level |
| `PGVECTOR_URL` | Postgres DSN for either pgvector retriever |
| `PGVECTOR_COLLECTION` | Collection *name* — activates the langchain-postgres retriever |
| `ANILA_COLLECTION_ID` | Collection *id* (int) — activates the ANILA-platform retriever; takes precedence over `PGVECTOR_COLLECTION` |
| `ANILA_EMBED_MODEL` | Embedding model name (default `text-embedding-3-small`) |
| `ANILA_EMBED_BASE_URL` | Embedding endpoint; falls back to `ANILA_BASE_URL` |
| `ANILA_EMBED_API_KEY` | Embedding key; falls back to `ANILA_API_KEY` |
| `ANILA_SSL_VERIFY` | `0` to skip TLS verification (self-signed certs only) |

## Tests

```bash
pytest
```

Coverage is intentionally focused on the harness layer (memdir port, hook bridge, retriever scoring) — the openai-agents primitives have their own test suite.

## Layout

```
anila-agent/
├── pyproject.toml
├── README.md
├── .env.example
├── configs/
│   ├── agent.yaml
│   ├── model.yaml
│   ├── memory.yaml
│   └── tools.yaml
├── anila_agent/
│   ├── main.py                 # CLI entrypoint
│   ├── cli/
│   │   ├── app.py              # REPL loop
│   │   ├── commands.py         # slash-command parser
│   │   └── renderer.py         # terminal output
│   ├── core/
│   │   ├── agent.py            # Agent assembly
│   │   ├── runner.py           # Tool loop wrapper
│   │   ├── hooks.py            # Hook surface
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
│   │   ├── dummy.py           # in-memory token-overlap (default)
│   │   ├── pgvector.py        # langchain_postgres-backed
│   │   ├── anila_pgvector.py  # ANILA platform native schema
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
    ├── test_pgvector_retriever.py        # langchain flavour
    └── test_anila_pgvector_retriever.py  # platform-native flavour
```

## Changelog

See [CHANGELOG.md](CHANGELOG.md) for release notes.

## License

Apache-2.0. See `LICENSE`.
