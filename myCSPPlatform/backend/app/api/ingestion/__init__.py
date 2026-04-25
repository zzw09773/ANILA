"""Ingestion platform API package (Phase 2 Sprint 1+).

Sprint 1 ships only the collections CRUD endpoint group. Document upload
+ async job tracking + chunking evaluator land in Sprints 2-3.
"""

from app.api.ingestion.collections import router as collections_router

__all__ = ["collections_router"]
