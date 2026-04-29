"""Run audit list / detail endpoints.

Authorization (spec §4.6):

* GET /:slug/runs — author of the function or admin
* GET /runs/:run_id — admin OR author of the function OR the user who
  triggered the run
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.auth import get_current_user
from app.database import get_db
from app.models import ActionFunction, ActionFunctionRun, User
from app.schemas.action_function import RunDetail, RunRead
from app.services.action_function import crud as fn_crud


router = APIRouter(prefix="/api/functions", tags=["action-functions"])


@router.get("/{slug}/runs", response_model=list[RunRead])
def list_runs(
    slug: str,
    limit: int = 50,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    fn = fn_crud.get_function_by_slug(db, slug)
    if fn is None:
        raise HTTPException(status_code=404)
    if fn.author_user_id != user.id and user.role != "admin":
        raise HTTPException(status_code=403)
    return (
        db.query(ActionFunctionRun)
        .filter_by(function_id=fn.id)
        .order_by(ActionFunctionRun.started_at.desc())
        .limit(limit)
        .all()
    )


@router.get("/runs/{run_id}", response_model=RunDetail)
def get_run(
    run_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    run = (
        db.query(ActionFunctionRun)
        .filter_by(id=run_id)
        .first()
    )
    if run is None:
        raise HTTPException(status_code=404)
    fn = (
        db.query(ActionFunction)
        .filter_by(id=run.function_id)
        .first()
    )
    is_admin = user.role == "admin"
    is_author = fn is not None and fn.author_user_id == user.id
    is_runner = run.triggered_by_user_id == user.id
    if not (is_admin or is_author or is_runner):
        raise HTTPException(status_code=403)
    return run
