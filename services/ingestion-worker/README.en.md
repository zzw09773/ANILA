# ingestion-worker

> The async background worker for ANILA document ingestion: it runs one uploaded file through **parse → chunk → embed → index**, extracts cross-document relations, and ships a chunking-strategy evaluator. It is the engine behind the "My Knowledge Base" (`我的知識庫`) entry that actually turns documents into retrievable vectors.

> 中文版本：[`README.md`](./README.md)

---

## Where it sits in the monorepo

This service lives at:

```
services/ingestion-worker/     ← this service (Arq worker, no HTTP surface)
packages/anila-core/           ← shared SDK (parser / chunker / vector store / security helpers)
infra/compose/platform.yml     ← compose definition (root compose.yaml is a shim that includes it)
```

Bring-up always goes through the root compose shim: `compose.yaml` → `infra/compose/platform.yml` (prod stack, project `anila`); `compose.dev.yaml` → `infra/compose/dev.yml` (dev stack). Deployment scripts live under `infra/deployment/{scripts,intranet}/`.

---

## This is not an HTTP service

`ingestion-worker` has **no HTTP endpoint and no health route**. It is a worker process driven by [Arq](https://arq-docs.helpmanual.io/) (a Redis-backed async job queue): CSP enqueues a job, the worker pulls it and runs it. Entry point:

```bash
arq ingestion_worker.main.WorkerSettings
```

`main.py`'s `WorkerSettings` registers **three** job functions (CSP dispatches them via `pool.enqueue_job(<name>, <arg>)`, see `services/csp/app/services/ingestion_queue.py`):

```text
ingest_document(document_id)                    # full ingest of one document
evaluate_strategies(eval_run_id)                # score one chunking eval run
reresolve_collection_relations(collection_id)   # re-extract a collection's relations
```

Arq retry / timeout policy (`main.py`): `max_tries=3`, `job_timeout=300` (s), `keep_result=3600` (so CSP can poll completion within an hour). `on_startup` opens one shared `PgPool` + builds an `Embedder`; `on_shutdown` drains both.

### Where the light on the governance dashboard comes from (ops)

No HTTP health route does not mean no health signal. The worker uses **arq's default heartbeat**: every 3600 s it writes a status line into the Redis key `arq:queue:health-check` with TTL = 3601 s. CSP's **service health overview** reads two signals together — that key, and whether docker DNS still resolves the name `ingestion-worker` (**it does not when the container is not running**). Four cases from those two signals, plus two where Redis itself cannot be asked:

| Name resolves | Key | Card | When you see it |
|---|---|---|---|
| yes | present | green (`ok`) | working normally (including mid-parse on a large document) |
| yes | absent | red (`unreachable`) | container is up but the process has been wedged for over 3601 s |
| no | present | red (`unreachable`) | **the container stopped or crashed**, within the last 3601 s |
| no | absent | amber (`not deployed`) | this deployment does not run ingestion-worker; **or** it has been gone for over 3601 s |
| (either) | unaskable | amber (`probe failed`) | Redis is unreachable, so the worker is unknown — check the Redis card |
| (either) | no answer | amber (`probe timeout`) | Redis did not answer within 4 s; still "could not ask", not "it is fine" |

**Detection times each state actually delivers (measured behaviour, not a target):**

- **Container stopped / crashed** → **red the next time the overview is opened** (the DNS record disappears at once while the key is still alive). This is the common death, and the only fast case.
- **Container up but process wedged** → up to **3601 s (~1 hour)** before it turns red, because only key expiry can reveal it.
- **Gone for more than 3601 s** → degrades to amber "not deployed". By then the key has expired too, and from CSP's side "long gone" and "never deployed" are the same observation; guessing either way would be a guess.
- **Not deployed** → amber throughout, correctly.

> 📌 **A planned shutdown also shows red.** After you run `docker compose stop ingestion-worker` yourself, this card stays red for **up to 3601 s** before degrading to amber "not deployed". The card cannot tell "you stopped it" from "it died" — red during maintenance is expected, not something to chase.

Scanned-PDF text recognition is Docling's job (EasyOCR on the remote document parser). The native parser says Docling is required when a PDF has no usable text layer. It does not send pages to a vision model.
>
> And it is not cleanly cut off either: `extract_text` is a **synchronous** call (`handlers.py:736`) that occupies arq's event loop per the table below, and arq enforces `job_timeout` with `asyncio.wait_for` (`arq/worker.py:591`) — **which cannot interrupt blocking synchronous code, and whose timer cannot even fire while the loop is blocked**. So the outcome is one of two bad ones, indeterminately: either it runs to completion and the 300 s budget is silently exceeded (with **every other queued ingest waiting** behind it), or the timer fires the moment the loop is released and discards 1800 s of already-paid VLM work, then retries (`max_tries=3`). Neither is what "5 minutes per ingest" promises.
>
> 📌 **Before 2026-08-07 that cost had never once been charged.** The trigger measured the `[[IMAGE:<id>]]` placeholders the parser itself inserts as "extracted text" — 24 characters each, so **a two-page pure scan already had 48**, cleared the 40-character floor, and was declared "has text, no OCR needed". With the flag on, no scan ever reached OCR. **These numbers are what you start paying now; they were not already happening.**
>
> **The rule now** (`packages/anila-core/src/anila_core/ingestion/ocr.py`): measure after placeholders are stripped, **drop short lines that repeat on most pages** (page numbers, document ids, watermarks, and the classification marking every page here carries — length is not what identifies furniture, **repetition is**), then ask how many pages still carry text; **fewer than half ⇒ it is a scan**. The old per-page *average* was defeated by one 10 000-character index page hiding a 200-page scan, so it counts pages instead.
>
> ⚠ **This rule trades one family of defeats for another.** The old rule (per-page average) was killed outright by one long-enough marking per page; the new one does not care about length, but it does care about how much a line repeats. **Every row below is measured, not reasoned** (data fed straight to `needs_ocr_fallback`):
>
> **A. Misses: a scan reads as "has text" ⇒ it is not asked to use Docling**
>
> | Case | Example | Why it slips through |
> |---|---|---|
> | Running head whose **words** change per page | `Page 3 of 50 - Section Environmental Limits` (section name changes) | a different line is not a repeated line, so it is not furniture |
> | Rotating reviewer / sign-off token | `Reviewed by inspector A - internal` (A–G in rotation) | same |
> | Furniture **longer than 80 characters** | measured with an 85-char classification + distribution-list header | a repeated line over 80 chars is treated as content, never as furniture |
> | **Two** alternating odd/even headers | each on exactly 50% of pages | below the "on 60% of pages" threshold |
> | Marking stamped on **some** pages only | 59 of 100 pages | same, 59% < 60% |
>
> 📌 **Digits changing is not a miss**: `Page 3 of 250 - Section 4.2`, where only the *numbers* vary, normalises to one line and is still caught. What defeats it is changing **words**, not changing numbers.
>
> **B. False triggers: a real-text document read as a scan ⇒ parsing stops and says Docling is required**
>
> | Case | Measured | Consequence |
> |---|---|---|
> | Many **fixed-layout forms** (labels repeat, filled values differ) | 250 of them ⇒ classified as a scan | the labels are dropped as furniture and the remaining values are too short to clear 20 chars a page |
> | **Uniform slide deck** | 60 slides ⇒ classified as a scan | each slide is a title plus a line or two; take out the repeated chrome and there is not enough left |
>
The table above is what gets classified as a scan. The result is a message that Docling is required.

> ⚠ **A separate single gap**: a **single-page** scan whose own marking exceeds 40 characters. Nothing repeats on a one-page document, so furniture cannot be told from content. Example: a one-page scan stamped `CONFIDENTIAL - NCSIST Internal Use Only - Page 1 of 1` reads as having text and is not asked to use Docling. Multi-page documents are unaffected.
>
> The decision is pinned by tests (`packages/anila-core/tests/test_pdf_ocr_trigger.py`).
>
Preview and ingest share the parser. A scan gets the same Docling message on both paths.

> ⚠ **Why the heartbeat is not made faster.** All three handlers do synchronous work on arq's event loop: `extract_text` was measured occupying it for 33–89 s on a 400-page PDF and 81–103 s on 1000 pages (the spread is host load, not code), and `evaluate_strategies` and `reresolve_collection_relations` call it **once per document in a loop** (a collection can hold hundreds). Once the heartbeat's TTL is shorter than that, **a worker doing its job correctly gets painted as dead** — the one mistake this card must never make. 3601 s clears any document the platform will accept (50 MB per file).
>
> Making the heartbeat faster requires first moving **every blocking path in every handler** off the event loop (table below), not just `ingest_document`'s parse. `tests/test_worker_liveness.py` guards this.

#### Synchronous work on the event loop (clear this table before touching the heartbeat)

> This table is a **point-in-time audit taken 2026-08-05, not a guarantee**. It was assembled by hand and nothing checks it automatically, so a newly added blocking call will not announce itself — whoever edits a handler owns keeping this list true. The eighth row (`split_segments`) was found in review after the first pass missed it.

| Handler | Where | What blocks |
|---|---|---|
| `ingest_document` | `handlers.py` parse step | `open().read()` + `extract_text` (measured 33–102 s) |
| `ingest_document` | `handlers.py` chunk step | `chunker.chunk` (measured 0.03–0.6 s) |
| `ingest_document` | `handlers.py` semantic pre-split | `SemanticChunker.split_segments` (0.12 s on a 934k-char document) |
| `ingest_document` | `handlers.py` `_uniform_color` | PIL `Image.open` + `convert` + sampled `getpixel`, **per embedded image** |
| `ingest_document` | `handlers.py` `_persist_images` | `open(...,"wb").write()` + `os.chmod`, **per image** |
| `evaluate_strategies` | `evaluator.py` `_load_sample_docs` | `open().read()` + `extract_text`, **per sample document** |
| `evaluate_strategies` | `evaluator.py` `_chunk_doc` | `chunker.chunk` + `SemanticChunker.split_segments`, **per strategy × per document** |
| `reresolve_collection_relations` | `handlers.py` tail loop | `open().read()` + `extract_text`, **per indexed document in the collection** |

### `ingest_document` pipeline (with progress pct)

| pct | Stage | Notes |
|---|---|---|
| 5 | start | mark `running` |
| 15 | Parse | `anila_core.ingestion.parsers.extract_text` via the parser registry |
| 22 | Caption + land images | optional VLM: replace `[[IMAGE:<id>]]` placeholders with captions, write `ingestion_images` + caption embeddings |
| 30 | Chunk | split into parent / leaf per `chunking_config` (default `hierarchical`) |
| 60 | Embed leaf | one embedding call per chunk |
| 85 | Index | `CollectionScopedPgVectorStore`: `add_parent_chunks` then `index_chunks` |
| 100 | done | update counts, mark `indexed` |

**Relation extraction (all best-effort, all toggleable)** runs at the tail: (1) regex citation edges (`relations.py`), (2) LLM edges (`llm_relations.py`), (3) embedding topic-similarity edges (`similarity_relations.py`). `reresolve_collection_relations` re-runs all three across every `indexed` document in a collection (per-doc parse failure is skipped).

---

## Architecture & stack

- Language / runtime: Python `>=3.11`; packaged with `hatchling`, source under `src/ingestion_worker`.
- Job queue: `arq>=0.26` (Redis backend).
- Shared SDK: `anila-core[rag]>=0.14.0` — parser registry, chunking plugins, `PgPool`, `CollectionScopedPgVectorStore`, `VisionProvider`, the `IngestionError` taxonomy, and security helpers (credential decrypt `decrypt_credential`, SSRF guard `validate_outbound_url`). The `[rag]` extra pulls in the parser stack (pymupdf4llm, python-docx, odfpy, striprtf, Pillow).
- DB / vectors: `asyncpg>=0.29` + `pgvector>=0.3`, writing to csp-db; chunk vector column is `halfvec(4000)` (migration 0015).
- HTTP client: `httpx>=0.27` (embedding / VLM / relation-LLM / judge).
- Config: `pydantic-settings>=2.0` (`WorkerSettings`, env-loaded, `case_sensitive=False`).

### `.doc` needs antiword (installed in the Dockerfile)

Legacy `.doc` (binary Word) is converted to plain text by `anila-core`'s `DocParser`, which shells out to **antiword**; without it a `.doc` upload fails at parse with `antiword is required for .doc parsing`. The Dockerfile installs it via `apt-get install antiword`; `DocParser` invokes it as `["antiword", "--", file_path]`, using `--` to block argument injection from filenames starting with `-`.

### Vector-dimension contract (important)

The embedding endpoint returns NV-embed-V2's native 4096-d and does **not** honour OpenAI-style `dimensions` truncation, so the worker truncates **client-side** to `EMBEDDING_DIM` (default 4000, aligned to `halfvec(4000)` — halfvec HNSW caps at 4000-d) and asserts the dimension on every response. A mismatch raises `E_EMBED_DIM_MISMATCH` (fail-fast, instead of surfacing at the asyncpg INSERT).

---

## Directory layout

```
services/ingestion-worker/
├── Dockerfile            # build context = repo root; installs packages/anila-core[rag] then this worker; apt installs antiword
├── pyproject.toml
├── src/ingestion_worker/
│   ├── main.py           # Arq WorkerSettings + the three job functions + on_startup/shutdown + retry
│   ├── settings.py       # env-loaded settings (DB / Redis / embedding / vision / relation / similarity)
│   ├── handlers.py       # ingest_document + reresolve_collection_relations core flow
│   ├── embedder.py       # OpenAI-compatible embedding client (dim truncation + assert)
│   ├── evaluator.py      # evaluate_strategies handler
│   ├── judge.py          # LLM-as-judge scoring (credential decrypt + SSRF guard)
│   ├── relations.py      # regex citation edges (rule-based)
│   ├── llm_relations.py  # LLM relation extraction
│   ├── similarity_relations.py  # embedding topic-similarity edges
│   └── parsers.py        # compat re-export of anila_core.ingestion.parsers.extract_text
└── tests/                # 8 test files (parsers / uniform_color / llm_relations / handlers_helpers /
                          #   embedder / judge / settings / evaluator_metrics) — 144 tests total
```

---

## Run & test

```bash
# In the stack (recommended) — root compose shim
docker compose up -d --build ingestion-worker            # → infra/compose/platform.yml
# dev stack: docker compose -f compose.dev.yaml up -d --build ingestion-worker

# Local dev / test (install anila-core first, then this worker)
cd services/ingestion-worker
.venv/bin/python -m pytest         # 144 tests; asyncio_mode=auto; testpaths=tests
.venv/bin/ruff check src tests
```

> For a fresh venv, follow the Dockerfile's install order: `pip install -e 'packages/anila-core[rag]'` first, then `pip install -e 'services/ingestion-worker[dev]'`.

In compose (`infra/compose/platform.yml`): build context = repo root; `depends_on` (all `service_healthy`) `csp-db` / `redis` / `csp`; volume `share/uploads/ingestion` (host) → `/var/anila/ingestion-uploads`; CMD `arq ingestion_worker.main.WorkerSettings`; `restart: unless-stopped`. **`docker restart` does not reload `.env`/compose; apply config with `up -d`.**

### Environment variables (from `settings.py`; compose overrides noted)

| Variable | Default | Notes |
|------|------|------|
| `DATABASE_URL` | `postgresql://csp_app:csp@csp-db:5432/csp` | asyncpg DSN; **must** be the `csp_app` role (RLS-bound, non-superuser) |
| `REDIS_URL` | `redis://redis:6379` | Arq queue backend |
| `EMBEDDING_BASE_URL` | `http://csp:8000/v1` (compose default; `host.docker.internal` is a structural url_guard deny) | embedding endpoint |
| `EMBEDDING_MODEL` / `EMBEDDING_API_KEY` | `nvidia/NV-embed-V2` / `not-set` | model / Bearer token |
| `EMBEDDING_DIM` / `EMBEDDING_TIMEOUT_SECONDS` | `4000` / `30.0` | truncation dim (aligned to halfvec(4000)) / timeout |
| `UPLOAD_DIR` | `/var/anila/ingestion-uploads` | shared upload-blob dir with CSP |
| `SSL_CERT_FILE` | unset | optional CA bundle for internal HTTPS VLM/OCR; verification remains enabled |
| `PG_POOL_MIN` / `PG_POOL_MAX` | `1` / `5` | connection pool (also caps concurrency) |
| `ENABLE_IMAGE_CAPTIONS` | `true` | VLM caption master switch |
| `VISION_URL` | `""` (compose `http://csp:8000/v1`) | VLM endpoint; empty string disables captioning |
| `VISION_API_KEY` | `not-set` | token for CSP. Which model writes captions is the Console vision role |
| `VISION_CONCURRENCY` / `VISION_TIMEOUT_SECONDS` / `VISION_MAX_IMAGE_BYTES` | `4` / `60.0` / `8 MiB` | parallelism / timeout / skip caption above size |
| `ENABLE_RELATION_LLM` | `true` | LLM relation-extraction master switch |
| `RELATION_LLM_URL` | `""` | empty string disables LLM edges |
| `RELATION_LLM_API_KEY` / `RELATION_LLM_VERIFY_SSL` | `not-set` / `true` | token / TLS. The model is the Console summary role; unset skips LLM relations |
| `RELATION_LLM_TIMEOUT_SECONDS` / `RELATION_LLM_MAX_CHARS` / `RELATION_LLM_MAX_CANDIDATES` | `120.0` / `12000` / `200` | timeout / input cap / candidate cap |
| `ENABLE_SIMILARITY_EDGES` | `true` | embedding similarity-edge master switch |
| `SIMILARITY_TOP_K` / `SIMILARITY_MIN` / `SIMILARITY_MAX_DOCS` | `3` / `0.75` / `500` | K nearest neighbours per doc / cosine floor / skip recompute above N |
| `DOCLING_OCR_LANGS` | `ch_tra,en` | OCR languages for the remote parser. Whether to call Docling comes from the console, not `DOC_PARSER` |

> `SECRET_KEY` and `ANILA_ALLOW_*` are consumed by `anila-core` security modules (credential decrypt / SSRF / http-endpoint fail-closed); compose injects them from the environment.

---

## Relationship to other services

- **CSP (governance center)**: upstream. Enqueues jobs + polls progress. **Embedding / VLM / relation-LLM calls are all routed through the CSP `/v1` proxy** (compose points at `http://csp:8000/v1`), so CSP's `proxy_service` writes `token_usage` centrally and the worker never meters itself (the `user_id` handed to `embed()` is immediately `del`'d). It authenticates to CSP with the **`ingestion-worker` system API key** (CSP writes the credential file; the worker only reads it). Before every outbound call, `anila-core` applies http-endpoint fail-closed + SSRF checks keyed on `ANILA_ALLOW_*`.
- **csp-db**: connects as `csp_app` (RLS-bound); RLS-scoped writes use `SET LOCAL anila.collection_id`. Reads documents / collections / eval_runs / user_llm_credentials; writes chunks / images / `document_relations` / status / counts.
- **Redis**: the Arq queue backend.
- **Shared upload dir**: CSP writes, worker reads; captioned images land in `<UPLOAD_DIR>/anila-images/<doc_id>/`.
- **User credentials for judge / relation LLM**: user-supplied credentials (`user_llm_credentials`, AES) are decrypted just-in-time; `validate_outbound_url` (SSRF) runs before egress, and the credential object's `__repr__` masks the key.

> This worker does not read `X-ANILA-Task-Id`, does not emit trace spans, and does not set a classification level. Model calls use the CSP Model Gateway key. Outbound calls are limited by `ANILA_ALLOW_*`.

---

## Related docs

- Current state: [`docs/CURRENT-STATUS.md`](../../docs/CURRENT-STATUS.md).
- Platform overview: [`../../README.md`](../../README.md)
