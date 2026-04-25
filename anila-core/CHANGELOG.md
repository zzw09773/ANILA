# anila-core CHANGELOG

All notable changes to this package. anila-core is **not yet 1.0** — internal
breaking changes are acceptable but always documented here. SemVer kicks in
once we cut v1.0 (no concrete date).

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
