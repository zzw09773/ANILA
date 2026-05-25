"""Ingestion platform API package (Phase 2 Sprint 1+).

Sprint 1 ships:
- collections CRUD (5 endpoints)
- document upload + list + detail (3 endpoints, async via Arq)

Sprint 2 adds: job progress SSE, chunking evaluator UI, agent_llm_credentials.

Studio extraction Phase 1 adds:
- POST /api/ingestion/collections/{id}/images/search  (in search router)
- GET  /api/ingestion/images/{image_id}/blob          (image_blob router)
"""

from app.api.ingestion.collections import router as collections_router
from app.api.ingestion.credentials import router as credentials_router
from app.api.ingestion.documents import router as documents_router
from app.api.ingestion.eval_runs import router as eval_runs_router
from app.api.ingestion.image_blob import router as image_blob_router
from app.api.ingestion.jobs import router as jobs_router
from app.api.ingestion.preview import router as preview_router
from app.api.ingestion.search import router as search_router

__all__ = [
    "collections_router",
    "credentials_router",
    "documents_router",
    "eval_runs_router",
    "image_blob_router",
    "jobs_router",
    "preview_router",
    "search_router",
]
