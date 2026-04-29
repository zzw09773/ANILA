"""Fork + abuse report + admin quarantine endpoints.

Marketplace model is "everything is visible to all roles, but writes
require role checks":

* fork: developer+; can only fork enabled functions; new copy starts
  as draft owned by the forker
* report: any logged-in user; insert report row + audit_logs entry
  so admins see it in the existing audit dashboard
* quarantine / unquarantine: admin only; quarantine hides code from
  non-author developers; unquarantine drops to disabled (author has
  to explicitly re-publish to enabled)
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.auth import get_current_user
from app.database import get_db
from app.models import (
    ActionFunctionReport,
    ActionFunctionReportStatus,
    ActionFunctionStatus,
    User,
)
from app.schemas.action_function import (
    ForkRequest,
    FunctionRead,
    QuarantineRequest,
    ReportRequest,
)
from app.services.action_function import crud as fn_crud
from app.services.audit_service import log_audit_event


router = APIRouter(prefix="/api/functions", tags=["action-functions"])


@router.post("/{slug}/fork", response_model=FunctionRead, status_code=201)
def fork(
    slug: str,
    payload: ForkRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if user.role not in ("developer", "admin"):
        raise HTTPException(status_code=403)
    src = fn_crud.get_function_by_slug(db, slug)
    if src is None:
        raise HTTPException(status_code=404)
    if src.status != ActionFunctionStatus.ENABLED:
        raise HTTPException(
            status_code=403,
            detail="can only fork enabled functions",
        )
    new_slug = payload.new_slug or f"{src.slug}-fork-{user.id}"
    if fn_crud.get_function_by_slug(db, new_slug):
        raise HTTPException(
            status_code=409, detail="new_slug already exists"
        )
    src_version = fn_crud.get_latest_version(db, src.id)
    fork_fn = fn_crud.create_function(
        db,
        author_user_id=user.id,
        slug=new_slug,
        title=src.title,
        description=src.description,
        icon_data_url=src.icon_data_url,
        code=src_version.code if src_version else "",
        tags=list(src.tags),
        actions_meta=src_version.actions_meta_json if src_version else [],
        valves_schema=src_version.valves_schema_json if src_version else {},
        metadata=src_version.metadata_json if src_version else {},
    )
    fork_fn.forked_from_id = src.id
    db.commit()
    db.refresh(fork_fn)
    return fork_fn


@router.post("/{slug}/report", status_code=201)
def report(
    slug: str,
    payload: ReportRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    fn = fn_crud.get_function_by_slug(db, slug)
    if fn is None:
        raise HTTPException(status_code=404)
    rep = ActionFunctionReport(
        function_id=fn.id,
        reporter_user_id=user.id,
        reason=payload.reason,
        status=ActionFunctionReportStatus.OPEN,
    )
    db.add(rep)
    log_audit_event(
        db,
        action="FUNCTION_REPORT",
        resource_type="action_function",
        resource_id=fn.slug,
        actor=user,
        metadata={"reason": payload.reason},
    )
    db.commit()
    db.refresh(rep)
    return {"id": rep.id}


@router.post("/{slug}/quarantine", status_code=204)
def quarantine(
    slug: str,
    payload: QuarantineRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if user.role != "admin":
        raise HTTPException(status_code=403)
    fn = fn_crud.get_function_by_slug(db, slug)
    if fn is None:
        raise HTTPException(status_code=404)
    fn.status = ActionFunctionStatus.QUARANTINED
    fn.disabled_reason = payload.reason
    log_audit_event(
        db,
        action="FUNCTION_QUARANTINE",
        resource_type="action_function",
        resource_id=fn.slug,
        actor=user,
        metadata={"reason": payload.reason},
    )
    db.commit()


@router.post("/{slug}/unquarantine", status_code=204)
def unquarantine(
    slug: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if user.role != "admin":
        raise HTTPException(status_code=403)
    fn = fn_crud.get_function_by_slug(db, slug)
    if fn is None:
        raise HTTPException(status_code=404)
    if fn.status != ActionFunctionStatus.QUARANTINED:
        raise HTTPException(status_code=400, detail="not quarantined")
    # Drop to disabled, NOT enabled — author has to deliberately re-publish
    fn.status = ActionFunctionStatus.DISABLED
    fn.disabled_reason = None
    log_audit_event(
        db,
        action="FUNCTION_UNQUARANTINE",
        resource_type="action_function",
        resource_id=fn.slug,
        actor=user,
    )
    db.commit()
