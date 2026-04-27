"""``/api/ingestion/eval-runs`` — Chunking Evaluator orchestration.

Sprint 3 first cut. Per docs/ingestion-platform-design.md §6 the
evaluator is the platform's "what's the best chunking strategy for
this corpus?" answer. Dev provides:

- ``sample_document_ids``: the docs to chunk under each candidate
  strategy (kept small — typically 5–10 docs out of the collection).
- ``strategies_tried``: list of ``{name, params}`` to compare.
- ``queries``: list of ``{query, expected_doc_id}`` pairs that act as
  the golden set for Hit@k / MRR scoring.

The worker reads this row, runs every (strategy × document) chunking
in memory, embeds via the platform's embedder, scores each strategy
against the queries, and writes the results back into ``results``
JSONB. The recommended strategy (best by Hit@1) lands in
``recommended_strategy``.

LLM-as-judge scoring is deferred to a follow-up — needs
agent_llm_credentials wiring + judge prompt design.

Endpoints:
- POST ``/api/ingestion/eval-runs`` — kick off; returns 202 + row id.
- GET  ``/api/ingestion/eval-runs/{id}`` — poll status / results.
- GET  ``/api/ingestion/eval-runs?collection_id=N`` — list runs for a
  collection.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.api.ingestion.collections import _require_collection_access
from app.database import get_db
from app.models.ingestion import (
    IngestionCollection,
    IngestionDocument,
    IngestionEvalRun,
)
from app.models.user import User
from app.services.audit_service import log_audit_event
from app.services.auth_service import get_current_user
from app.services.ingestion_queue import enqueue_evaluator_run


router = APIRouter(tags=["Ingestion / Evaluator"])


# ── Request / response shapes ──────────────────────────────────────────────


class StrategySpec(BaseModel):
    """One strategy + its params to evaluate."""

    name: str = Field(..., min_length=1, max_length=64)
    params: dict[str, Any] = Field(default_factory=dict)


class QuerySpec(BaseModel):
    """One golden-set query + the document the dev expects to surface."""

    query: str = Field(..., min_length=1, max_length=2000)
    expected_doc_id: int


class EvalRunCreate(BaseModel):
    collection_id: int
    name: str = Field(..., min_length=1, max_length=200)
    sample_document_ids: list[int] = Field(..., min_length=1, max_length=50)
    strategies_tried: list[StrategySpec] = Field(..., min_length=1, max_length=10)
    queries: list[QuerySpec] = Field(..., min_length=1, max_length=200)


class EvalRunResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    collection_id: int
    name: str
    sample_document_ids: list[int]
    strategies_tried: list[dict[str, Any]]
    queries: list[dict[str, Any]]
    arq_job_id: str | None
    status: str
    results: dict[str, Any] | None
    recommended_strategy: str | None
    error_code: str | None
    error_message: str | None
    created_by: int | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None


# ── Endpoints ──────────────────────────────────────────────────────────────


@router.post(
    "/api/ingestion/eval-runs",
    response_model=EvalRunResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_eval_run(
    payload: EvalRunCreate,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> EvalRunResponse:
    # Sprint 4: collection-scoped access (admin OR owner).
    coll = _require_collection_access(db, current_user, payload.collection_id)

    # Validate every sample document belongs to this collection — guards
    # against a buggy frontend or a malicious caller mixing in another
    # collection's doc ids.
    docs = (
        db.query(IngestionDocument)
        .filter(
            IngestionDocument.id.in_(payload.sample_document_ids),
            IngestionDocument.collection_id == payload.collection_id,
        )
        .all()
    )
    if len(docs) != len(set(payload.sample_document_ids)):
        raise HTTPException(
            status_code=400,
            detail=(
                "sample_document_ids contains documents that don't belong to "
                "this collection"
            ),
        )

    # Validate every expected_doc_id is in sample_document_ids — otherwise
    # the metric is uncomputable (the doc isn't even in the test pool).
    sample_set = set(payload.sample_document_ids)
    bad_queries = [q for q in payload.queries if q.expected_doc_id not in sample_set]
    if bad_queries:
        raise HTTPException(
            status_code=400,
            detail=(
                f"{len(bad_queries)} queries reference doc ids not in "
                f"sample_document_ids — pick from the sampled set."
            ),
        )

    run = IngestionEvalRun(
        collection_id=payload.collection_id,
        name=payload.name,
        sample_document_ids=payload.sample_document_ids,
        strategies_tried=[s.model_dump() for s in payload.strategies_tried],
        queries=[q.model_dump() for q in payload.queries],
        status="queued",
        created_by=current_user.id,
    )
    db.add(run)
    db.commit()
    db.refresh(run)

    arq_job_id = await enqueue_evaluator_run(run.id)
    run.arq_job_id = arq_job_id
    db.commit()
    db.refresh(run)

    log_audit_event(
        db,
        actor=current_user,
        action="ingestion_eval_run_create",
        resource_type="ingestion_eval_run",
        resource_id=run.id,
        metadata={
            "collection_id": payload.collection_id,
            "strategies": [s.name for s in payload.strategies_tried],
            "n_queries": len(payload.queries),
            "n_docs": len(payload.sample_document_ids),
        },
    )
    return EvalRunResponse.model_validate(run)


@router.get(
    "/api/ingestion/eval-runs/{run_id}",
    response_model=EvalRunResponse,
)
def get_eval_run(
    run_id: int,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> EvalRunResponse:
    run = db.query(IngestionEvalRun).filter(IngestionEvalRun.id == run_id).first()
    if run is None:
        raise HTTPException(status_code=404, detail="Eval run not found")
    _require_collection_access(db, current_user, run.collection_id)
    return EvalRunResponse.model_validate(run)


@router.get(
    "/api/ingestion/eval-runs",
    response_model=list[EvalRunResponse],
)
def list_eval_runs(
    collection_id: int = Query(..., description="必填：列出此 collection 的 eval runs"),
    db: Annotated[Session, Depends(get_db)] = None,
    current_user: Annotated[User, Depends(get_current_user)] = None,
) -> list[EvalRunResponse]:
    _require_collection_access(db, current_user, collection_id)
    rows = (
        db.query(IngestionEvalRun)
        .filter(IngestionEvalRun.collection_id == collection_id)
        .order_by(IngestionEvalRun.id.desc())
        .all()
    )
    return [EvalRunResponse.model_validate(r) for r in rows]
