"""
ANILA Core — shared Python library for the ANILA platform

Two pillars
===========

1. **Agent runtime** — what an in-process agent or the Router needs to
   serve a single chat turn end-to-end:

     - api/                FastAPI factory + middleware (CSP service-token)
     - engine/             7-stage QueryEngine + BudgetTracker
     - coordinator/        multi-worker / multi-step orchestration
     - registry/           local + remote agent manifest cache
     - context/            AgentContext (turn-scope contextvars)
     - tools/              dispatch_tool + prompts
     - router/             tool router
     - providers/          OpenAI-compat + CSP-platform + mocks
     - memory/             extract / select / consolidate (Memdir)
     - compact/            micro / auto / sliding window / session memory
     - models/             pydantic message / tool / agent / memory dtos
     - cli/                ``anila-core init / register / status /
                            agent bootstrap`` developer CLI
     - config.py           pydantic-settings entry

2. **Shared infrastructure** — primitives that don't belong to a single
   agent process; consumed by Router, agents, AND batch workers
   (ingestion-worker today, future PII / scoring / refresh workers
   tomorrow):

     - security/           credential_crypto (AES-GCM + PBKDF2 600k) +
                           url_guard (SSRF deny-list)
     - storage/adapters/   PgPool (asyncpg) + CollectionScopedPgVectorStore
                           + MemoryFileStore for tests
     - ingestion/          IngestionError taxonomy + chunking_plugins
                           registry. Plugin pattern so multiple workers
                           can `@register_chunker` their own strategy
                           without code churn in this package.

Out of scope (intentionally NOT here)
=====================================
  - H100 deployment / vLLM process management
  - Company auth, monitoring, alerting (those live in CSP)
  - Production ingestion orchestration (that's CSP's
    ``app/services/ingestion_*`` + ingestion-worker)
  - Document parsing / OCR / vision (AgenticRAG owns its own pipeline,
    forks free to swap)

Storage key structure
=====================
All persistent data is indexed with the three-layer key:
    user_id + project_id + session_id

This maps naturally to OpenWebUI: account → workspace → conversation.

Boundary history
================
v0.5.0's release notes claimed `ingestion/`, `storage/adapters/pg_pool`,
and `storage/adapters/pgvector_store` had been removed. They were not —
ingestion-worker imports the chunking plugins and pg adapters directly.
The Sprint 8 X audit re-affirmed they belong here as shared
infrastructure (Pillar 2 above) and the v0.5.0 framing was aspirational
rather than executed. See README "Release Notes" for the correction.
"""

__version__ = "0.7.0"
