# Changelog

All notable changes to this project are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versions follow [SemVer](https://semver.org/).

## [0.2.1] — 2026-05-11

### Changed
- Cleared template-specific defaults so a fresh clone starts neutral:
  - `configs/agent.yaml` `name`: `anila` → `my-agent` (this string is what
    other agents see when this one is exposed via `Agent.as_tool()`).
  - `anila_agent/prompts/system.md` and `prompts/agent.md` no longer carry
    the `Anila` identity line; they ship as TODO-marked placeholders with
    the generic RAG operating principles intact.
  - `examples/rag_agent.py` seeded corpus changed from project-internal docs
    to three domain-neutral example facts (astronomy / geography / physics).
  - `.env.example` reorganised into Flavour A (langchain) / Flavour B (ANILA
    platform) blocks with placeholder values throughout.
  - `anila_pgvector.py` docstring + README Option B examples now use
    `<host>` / `<port>` / `<collection_id>` placeholders instead of the
    specific values from the development machine.

## [0.2.0] — 2026-05-11

### Added
- **`anila_agent/retrieval/pgvector.py`** — drop-in retriever for the generic
  `langchain_postgres.PGVector` schema. Activated automatically when
  `PGVECTOR_URL` + `PGVECTOR_COLLECTION` are set; falls through silently when
  not. Embedding endpoint reuses `ANILA_BASE_URL` / `ANILA_API_KEY` unless
  `ANILA_EMBED_BASE_URL` / `ANILA_EMBED_API_KEY` are set explicitly.
- **`anila_agent/retrieval/anila_pgvector.py`** — retriever for the ANILA
  platform's native pgvector schema (`ingestion_collections` +
  `document_chunks` with halfvec + RLS via `anila.collection_id` GUC).
  Activated via `ANILA_COLLECTION_ID`; embedding dimension auto-detected
  from `ingestion_collections.embedding_dim` so the same code works against
  collections with different embed widths.
- Optional `[pgvector]` extra (`langchain-postgres>=0.0.15,<0.1`,
  `langchain-openai>=1.0,<2`, `psycopg[binary]>=3.1,<4`). Both retrievers
  use lazy imports — install only when needed.
- 35 new unit tests covering both retrievers' configuration parsing,
  helpers (DSN normalisation, halfvec text format, JSONB metadata decode),
  constructor validation, env-var precedence, and Protocol conformance.
  Total suite now at 65 tests, all green.
- `ANILA_SSL_VERIFY=0` env knob for self-signed certs on private endpoints.
- `.env.example` documents every new env var with intent comments.

### Changed
- `build_agent()` ([anila_agent/core/agent.py](anila_agent/core/agent.py))
  now installs a retriever from environment automatically. Precedence:
  ANILA-native → langchain-postgres → keep `DummyRetriever`.
- Half-configured deployments fail loud rather than silently fall back to
  `DummyRetriever` — `PGVECTOR_URL` without `PGVECTOR_COLLECTION` raises,
  and `ANILA_COLLECTION_ID` without `PGVECTOR_URL` raises.
- `configs/model.yaml` `max_tokens` lowered from 4096 → 1024 — gemma4-class
  models emit chain-of-thought before the final answer; 1024 is enough head
  room without inflating per-turn cost on every call.

### Fixed
- `configs/agent.yaml` pointed at `prompts/system.md` but the file lives at
  `anila_agent/prompts/system.md`. Loading the CLI raised `FileNotFoundError`.
- `LongTermMemory.recall_async` always returned `[]` (hard-coded `query=""`,
  `k=0`) and `recall_sync` called `asyncio.run()` from inside an event loop.
  Both removed — they were never called and would have crashed if used.

### Removed
- Dead code: `recall_async` / `recall_sync` and an unused `import asyncio`
  in `anila_agent/memory/long_term.py`.

## [0.1.0] — 2026-05-06

### Added
- Initial scaffold of the agentic-RAG starter on `openai-agents` SDK with a
  Claude-Code-inspired harness.
- Core: `Agent` assembly, `AnilaRunner`, hook surface
  (`PreToolUse` / `PostToolUse` / `Stop` / `SessionStart` / `UserPromptSubmit`),
  in-process event bus.
- Memory: file-backed memdir port (`MEMORY.md` index + typed topic files),
  `SQLiteSession`-backed short-term, opt-in Stop-hook auto-extractor.
- Tools: `@anila_tool` decorator with metadata (`is_read_only` /
  `is_destructive` / `requires_confirmation`), built-in `search_documents`
  and `read_document` RAG tools, sandboxed `read_file` / `list_dir`.
- Retrieval: `Retriever` Protocol + `DummyRetriever` (token-overlap).
- Models: `LitellmModel` adapter for any OpenAI-compatible endpoint.
- CLI: REPL with prompt_toolkit + rich, slash commands
  (`/help` `/clear` `/memory` `/model` `/cost` `/exit`).
- 30 unit tests for memdir CRUD, hook bridge, retriever scoring.
