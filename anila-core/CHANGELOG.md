# anila-core CHANGELOG

All notable changes to this package. anila-core is **not yet 1.0** — internal
breaking changes are acceptable but always documented here. SemVer kicks in
once we cut v1.0 (no concrete date).

## v0.7.0 (2026-04-27) — Collection-as-first-class (Sprint 4, Chunks O–T)

### BREAKING

The Sprint 1–3 architecture treated every collection as the property of
exactly one agent (``ingestion_collections.agent_id NOT NULL``). Smoke-
testing on real workflows showed this was over-coupling: ANILA's posture
is "platform = pgvector infrastructure", agent backends just configure
``DB_URL + COLLECTION_ID`` and the platform doesn't care which agent
reads what. v0.7 drops the agent coupling entirely.

#### Schema

| Concept | v0.6 | v0.7 |
|---|---|---|
| Collection ownership | `agent_id` FK to agents | `created_by` FK to users (NOT NULL) |
| Chunk RLS scope | `anila.agent_id` GUC | `anila.collection_id` GUC |
| Chunk `agent_id` column | denormalised | dropped |
| LLM credentials FK | agents | users (table renamed `agent_llm_credentials` → `user_llm_credentials`) |

Migration 0019 (CSP) handles all of the above plus a "csp lifespan
fallback ``Base.metadata.create_all`` re-creates orphan tables" gotcha
discovered during the refactor.

#### SDK

- `AgentScopedPgVectorStore` → **`CollectionScopedPgVectorStore`**.
  Constructor takes `collection_id: int` (positive int guard kept).
  ``_acquire`` sets ``anila.collection_id`` GUC.
- ``index_chunks(document_id, chunks, embeddings)`` — dropped redundant
  ``collection_id`` per-call argument.
- ``similarity_search`` / ``keyword_search`` — dropped optional
  ``collection_id`` per-call arguments. RLS does the scoping.
- ``list_in_collection(limit, offset)`` — Sprint 4 rename of
  ``list_by_collection``; parameter is implicit now.
- ``delete_all()`` — Sprint 4 rename of ``delete_collection``.
- All SQL paths drop ``agent_id`` from SELECT projections.
- ``IngestionChunk`` Pydantic model: ``agent_id`` field removed.

#### Back-compat

- ``AgentScopedPgVectorStore`` aliases ``CollectionScopedPgVectorStore``
  for one transition cycle. Old callers fail at the call site with
  the constructor kwarg name change (``agent_id`` → ``collection_id``)
  rather than an obvious import error — by design, the forcing function
  for them to update.

### Tests

- ``test_collection_scoped_pgvector_store.py`` — 13 constructor-guard
  tests rewritten around ``collection_id``. New test pins the
  back-compat alias invariant.
- ``test_g1_collection_isolation.py`` — Sprint 1 G1 rebase; 5
  collections × 50 chunks × 30 random queries = 750 leakage probes.
- ``test_g2_rls_bypass.py`` — Sprint 1 G2 rebase; FORCE RLS posture
  + collection-scoped GUC bypass attempts (4 paths).
- Old ``test_g1_agent_isolation.py`` and
  ``test_agent_scoped_pgvector_store.py`` deleted.

### Sprint 4 G1/G2/G3 results

| Gate | v0.6 scope | v0.7 scope | Result |
|---|---|---|---|
| G1 random workload, zero leakage | 5 agents | **5 collections** | ✅ 1.96s |
| G2 raw asyncpg without GUC sees 0 rows | `anila.agent_id` | **`anila.collection_id`** | ✅ |
| G3 single SQL entry point | unchanged | unchanged | ✅ |

### Migration

| If you …                                         | Do this                                                                                                |
|---|---|
| Were using ``AgentScopedPgVectorStore``           | Switch to ``CollectionScopedPgVectorStore``; constructor kwarg ``agent_id`` → ``collection_id``.       |
| Had ``RAG_AGENT_ID`` env on AgenticRAG / forks    | Switch to ``RAG_COLLECTION_ID``. The collection it points at must already exist in CSP UI.            |
| Had per-tenant `agent_llm_credentials` rows       | They became ``user_llm_credentials`` rows scoped to ``created_by``. Re-issue if FK chain was broken. |
| Were calling `index_chunks(collection_id=..., document_id=..., ...)` | Drop the redundant `collection_id` kwarg. The store already knows.                                    |

---

## v0.6.0 (2026-04-25) — Ingestion Platform foundation (Sprint 1, Chunks A–G)

### Added

The boundary v0.5.0 left was "anila-core is a pure runtime". v0.6.0 adds
the ingestion-platform SDK layer back on top, this time as a thin,
agent-scoped facade rather than the per-deployment runtime that was
deleted. Sprint 1 ships:

- **`anila_core.ingestion`** — ingestion support layer for the central
  worker service.
  - `errors.IngestionError` taxonomy (5 codes: `E_PARSE_FORMAT_UNSUPPORTED`,
    `E_PARSE_CORRUPT`, `E_EMBED_TIMEOUT`, `E_PG_CONNECT`,
    `E_PG_RLS_VIOLATION`). Each carries `retryable` / `severity`.
    `E_PG_RLS_VIOLATION` is hard-coded as `severity=critical, retryable=False`
    — RLS bypass is a security incident, never auto-recovered.
  - `chunking_plugins` — Protocol + idempotent registry + 3 built-in
    strategies (`hierarchical`, `fixed`, `markdown-aware`). The 3
    remaining strategies from the design doc (`pdf-page`, `cjk-sentence`,
    `semantic`) live in the worker service alongside their heavier deps.

- **`anila_core.storage.adapters`** — agent-scoped pgvector access.
  - `PgPool` returns. Same name as the v0.5.0-deleted class but the new
    one auto-registers `vector` + `halfvec` + `sparsevec` + `jsonb`
    codecs on every connection (the legacy adapter only did `vector`).
  - `AgentScopedPgVectorStore` is the only sanctioned read/write path
    into `document_chunks`. Constructor refuses non-positive int
    `agent_id` (rejects None / str / float / bool / 0 / negative).
    Every method wraps work in `BEGIN ... SET LOCAL anila.agent_id = N
    ... COMMIT` — without the explicit transaction, asyncpg autocommits
    each statement and Layer 2 RLS is silently bypassed.
  - Methods: `index_chunks`, `similarity_search`,
    **`keyword_search`** (FTS via `plainto_tsquery` against
    `content_tsv`), `list_by_document`, `list_by_collection`,
    `delete_document`, `delete_collection`.

- **`anila_core.models.ingestion`** — `IngestionChunk` + `SearchHit`
  Pydantic models for the new schema. The legacy
  `models.storage.DocumentChunk` (TEXT chunk_id, user_id/project_id)
  remains for back-compat but is no longer the canonical chunk type.

### BREAKING (since v0.5.0)

- `pyproject.toml`: `asyncpg>=0.29` and `pgvector>=0.3` are core deps
  again (v0.5.0 demoted them to optional). The central SDK needs them.
- Dependency footprint up by ~15 MB installed; v0.5.0's clean-runtime
  promise is intentionally relaxed because the central SDK lives here now.

### Tests

- 35 unit tests added (errors / chunking_plugins / store constructor
  guards / G3 static gate) — total 209 passing on this branch.
- 6 integration tests under `tests/integration/` (G1 random workload,
  G2 RLS bypass × 4) — runtime ~2s against a live pgvector. Auto-skip
  when no DB is reachable.

### Sprint 1 G1/G2/G3 gates

| Gate | Result |
|---|---|
| G1: 5 agents × random workload, zero cross-agent leakage | ✅ |
| G2: raw asyncpg without GUC sees 0 rows; RLS holds | ✅ |
| G3: actual SQL on `document_chunks` lives in 1 file (the SDK) | ✅ |

### Migration

| If you …                                              | Do this                                                                                                                  |
|---|---|
| Already shipped a fork on v0.5.0                       | Add `RAG_AGENT_ID` env to the deployment; switch to `csp_app` runtime DSN; drop your own `pgvector_store.py` if cloned. |
| Were importing from `anila_core.storage.adapters` v0.5 | Imports still work. `AgentScopedPgVectorStore` and `PgPool` are new. The MemoryFileStore re-export is unchanged.            |
| Were calling old `models.DocumentChunk`               | Still there. New code uses `models.ingestion.IngestionChunk`.                                                            |

---

## v0.5.0 (2026-04-25) — Boundary cleanup (Sprint 1)

### BREAKING

Sprint 1 of the Phase 2 boundary cleanup ([anila-core-boundary spec](../docs/anila-core-boundary.md)) split the RAG-flavour runtime out of core. anila-core is now a strictly chat / agent / memory / dispatch runtime; everything RAG-flavour now lives in [AgenticRAG](../AgenticRAG/) (per-agent template) or, for the future centralised path, the [Ingestion Platform](../docs/ingestion-platform-design.md).

#### Modules removed

| Path | Replacement |
|---|---|
| `anila_core.ingestion.*` | AgenticRAG ships a 2017-line pipeline (`docling_parser` + `parsers` + `chunker` + `ocr` + `tokenize_zh` + `service`). For multi-agent shared ingestion, the Ingestion Platform service supersedes anila-core's local pipeline. |
| `anila_core.storage.adapters.pg_pool` | Use `agentic_rag.storage.adapters.pg_pool_v2` once the Ingestion Platform's v2 pool lands. |
| `anila_core.storage.adapters.pgvector_store` | Use `agentic_rag.storage.adapters.pgvector_store_v2`. |
| `anila_core.storage.adapters.postgres_store` (PgSessionStore / PgMessageStore / PgRetrievalTraceStore + `initialize_schema`) | Same — RAG schema bootstrap belongs in the ingestion service, not the agent runtime. |
| `anila_core.providers.embedding_nvidia.NvidiaEmbeddingProvider` | Embedding now happens inside the ingestion-worker; agents do not embed inline. |
| `anila_core.engine.rag_preprocessor.RagPreprocessor` | Pre-process injection pattern is dead. The new model is **tool-driven retrieval**: the LLM calls `vector_search` / `keyword_search` as registered tools when it decides to. |
| `anila_core.api.documents` (`/upload` `/ingest` `/status` endpoints) | CSP `/api/ingestion/*` endpoints (Ingestion Platform). |
| `anila_core.api.search` (`POST /search`) | Same. |
| `anila_core.tools.{create_vector_search_tool, create_keyword_search_tool, create_read_document_tool}` | These factories now live in `agentic_rag.tools` only. anila-core does not register any tools by default — callers wire whatever they need. |
| `anila_core.tools.prompts.AGENTIC_RAG_SYSTEM_PROMPT` | AgenticRAG carries its own system prompt; `/agentic-chat` no longer ships a RAG default. |

#### `storage.ports` Protocols — UNCHANGED

`anila_core.storage.ports.{DocumentStore, RetrievalProvider, ...}` Protocol definitions stay in core. They are the interface contract any future backend (qdrant, milvus, chroma, pgvector v2) implements.

#### `storage.adapters.MemoryFileStore` — KEPT

Filesystem `MemoryStore` implementation used by `anila_core.memory.*`. Not RAG-specific; this is platform memory infra. A `PostgresMemoryStore` impl will land in Phase 3+ for production deploys.

#### `app_factory.build_app()` — slimmed (143 → 60 lines)

The factory now does:

```python
llm_provider  = OpenAICompatProvider(...)
tool_registry = ToolRegistry()         # caller registers tools
return create_app(provider=llm_provider, tool_registry=tool_registry, ...)
```

The previous lifespan (PG pool init, pgvector schema bootstrap), `LazyStoreProxy` plumbing, `IngestionService` composition, `NvidiaEmbeddingProvider` wiring, chunker construction — all gone. Forks like AgenticRAG carry their own `app_factory.py` with the full ingestion stack.

#### `create_app()` signature — 6 RAG kwargs removed

```diff
 def create_app(
     provider: Provider,
     tool_registry: ToolRegistry,
     away_summary_fn: Optional[Any] = None,
-    ingestion_service: Optional[Any] = None,
-    document_store: Optional[Any] = None,
-    embedding_provider: Optional[Any] = None,
-    retrieval_provider: Optional[Any] = None,
-    db_pool: Optional[Any] = None,
     api_key: Optional[str] = None,
     api_dev_mode: bool = False,
-    upload_dir: str = "/tmp/anila_uploads",
 ) -> FastAPI:
```

#### `/agentic-chat` endpoint — RAG wiring removed (Grey Zone B resolution)

The endpoint stays. Inside, it no longer imports the RAG factories or wires per-request RAG tools. It just runs the agent loop with whatever ToolRegistry the host configured at app-factory time. `request.system_prompt` is now **required** (422 on missing) — anila-core no longer ships a RAG default.

#### `query_engine.QueryEngine.__init__` — `rag_preprocessor` arg removed

The optional `rag_preprocessor: Optional[RagPreprocessor] = None` constructor arg is gone, along with the `_pre_process` injection path. `_pre_process` stays as a passthrough hook for future preprocessing concerns (token gates, redaction).

#### `config.Settings` — 11 fields removed

```diff
-embedding_url, embedding_api_key, embedding_model,
-embedding_dimension, embedding_verify_ssl,
-database_url, pg_pool_min, pg_pool_max, pg_ssl,
-chunk_size, chunk_overlap,
-rag_top_k, rag_min_score,
-upload_dir
```

Settings now has 8 fields covering LLM provider, CSP plumbing, and auth.

### Migration guide

| If you …                                                | Do this                                       |
|---|---|
| Were building a RAG agent on top of anila-core directly | Fork the [AgenticRAG](../AgenticRAG/) template; it carries the full ingestion + tool wiring. |
| Were using `pip install anila-core[rag]`                | The `[rag]` extras no longer exist (the dependencies they pulled — pgvector / docling / pypdf — moved to AgenticRAG's `pyproject.toml`). |
| Were importing `anila_core.tools.create_vector_search_tool` etc. | Switch to `agentic_rag.tools.create_vector_search_tool`. AgenticRAG's version is also slightly more recent (383 vs 274 lines). |
| Hit `/agentic-chat` without a `system_prompt`           | Now returns 422. Pass your prompt explicitly. |
| Were calling `create_app(embedding_provider=…)` etc.     | Drop the RAG kwargs. Pre-register your tools in the `tool_registry` you pass in. |

### Verification

- `pytest anila-core/tests/`: 166 passed, 5 pre-existing failures (test_router_runtime_contract + test_dispatch_tool — middleware / router-server issues unrelated to this work). 0 regressions.
- G3 gate: `grep document_chunks anila-core/` = **0 hits**. The RAG schema reference is now concentrated in AgenticRAG (3 hits) + Ingestion Platform docs.
- Footprint: −3998 lines of RAG dead code removed across Chunks 1+2+3.

### Commits

| Chunk | Days | Commit | What |
|---|---|---|---|
| 1 | 1–3 | `afc3c9f` | RAG tool factories + AGENTIC_RAG_SYSTEM_PROMPT + the 2 RAG-only test files |
| 2 | 4–6 | `371881d` | `ingestion/` + `api/{documents,search}.py` + `app_factory` slim + `query_engine` rag_preprocessor cleanup |
| 3 | 7–9 | `d7ae6b5` | pg adapters + embedding_nvidia + rag_preprocessor.py file + config RAG fields |
| 4 | 10  | (this commit) | README rewrite + CHANGELOG + G3 gate verification |

---

## v0.4.x and earlier

See `README.md` § Release Notes for Wave A / Wave B history.
