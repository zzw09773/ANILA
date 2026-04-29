"""Function CRUD + version endpoints.

Code visibility per spec §3.6 / §7.1:

* admin: any status
* author: own function any status (including draft / quarantined)
* developer (non-author): enabled / disabled (NOT quarantined, NOT draft)
* user role: never

The :func:`_can_view_code` helper is the single source of truth for
that ACL — every endpoint that returns ``code`` or ``versions[].code``
must consult it.

Save (POST /versions) flow:

  1. RBAC (author or admin)
  2. WorkerClient.extract_meta(code) — Sprint 1 stub returns canned
     metadata; Sprint 2 wires real worker call
  3. crud.save_version with advisory lock
  4. commit
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.auth import get_current_user
from app.database import get_db
from app.models import (
    ActionFunction,
    ActionFunctionStatus,
    ActionFunctionVersion,
    User,
)
from app.schemas.action_function import (
    FunctionCreate,
    FunctionPatch,
    FunctionRead,
    FunctionReadWithCode,
    VersionCreate,
    VersionRead,
)
from app.services.action_function import crud as fn_crud
from app.services.action_function.worker_client import WorkerClient


router = APIRouter(prefix="/api/functions", tags=["action-functions"])


def _require_developer(user: User) -> None:
    if user.role not in ("developer", "admin"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="developer role required",
        )


def _can_view_code(user: User, fn: ActionFunction) -> bool:
    """Per spec §3.6 + §7.1.

    - admin: any status
    - author: own function any status (incl. draft / quarantined)
    - developer (non-author): enabled / disabled only — quarantined
      deliberately excluded so admin-disabled-due-to-abuse code stays
      hidden from peers
    - user role: never
    """
    if user.role == "admin":
        return True
    if fn.author_user_id == user.id:
        return True
    if user.role == "developer" and fn.status in (
        ActionFunctionStatus.ENABLED,
        ActionFunctionStatus.DISABLED,
    ):
        return True
    return False


def _author_or_admin(user: User, fn: ActionFunction) -> bool:
    return user.role == "admin" or fn.author_user_id == user.id


@router.get("", response_model=list[FunctionRead])
def list_functions(
    author: str | None = None,
    status: str | None = None,
    tag: str | None = None,
    q: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    author_id = user.id if author == "me" else None
    return fn_crud.list_functions(
        db, author_user_id=author_id, status=status, tag=tag, q=q
    )


@router.post("", response_model=FunctionRead, status_code=201)
async def create_function(
    payload: FunctionCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    _require_developer(user)
    if fn_crud.get_function_by_slug(db, payload.slug):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="slug already exists",
        )
    client = WorkerClient()
    extract = await client.extract_meta(payload.code)
    if extract.get("errors"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"extract_errors": extract["errors"]},
        )
    fn = fn_crud.create_function(
        db,
        author_user_id=user.id,
        slug=payload.slug,
        title=payload.title,
        description=payload.description,
        icon_data_url=payload.icon_data_url,
        code=payload.code,
        tags=payload.tags,
        actions_meta=extract["actions_meta_json"],
        valves_schema=extract["valves_schema_json"],
        metadata=extract["metadata_json"],
    )
    db.commit()
    db.refresh(fn)
    return fn


@router.get("/{slug}", response_model=FunctionReadWithCode)
def get_function(
    slug: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    fn = fn_crud.get_function_by_slug(db, slug)
    if fn is None:
        raise HTTPException(status_code=404)
    response = FunctionReadWithCode.model_validate(fn).model_dump()
    if _can_view_code(user, fn):
        latest = fn_crud.get_latest_version(db, fn.id)
        if latest is not None:
            response["code"] = latest.code
            response["valves_schema_json"] = latest.valves_schema_json
            response["actions_meta_json"] = latest.actions_meta_json
    return response


@router.patch("/{slug}", response_model=FunctionRead)
def patch_function(
    slug: str,
    payload: FunctionPatch,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    fn = fn_crud.get_function_by_slug(db, slug)
    if fn is None:
        raise HTTPException(status_code=404)
    if not _author_or_admin(user, fn):
        raise HTTPException(status_code=403)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(fn, field, value)
    db.commit()
    db.refresh(fn)
    return fn


@router.delete("/{slug}", status_code=204)
def delete_function(
    slug: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    fn = fn_crud.get_function_by_slug(db, slug)
    if fn is None:
        raise HTTPException(status_code=404)
    if not _author_or_admin(user, fn):
        raise HTTPException(status_code=403)
    # v1 soft delete: mark disabled instead of dropping the row
    fn.status = ActionFunctionStatus.DISABLED
    db.commit()


@router.post(
    "/{slug}/versions", response_model=VersionRead, status_code=201
)
async def save_version(
    slug: str,
    payload: VersionCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    fn = fn_crud.get_function_by_slug(db, slug)
    if fn is None:
        raise HTTPException(status_code=404)
    if not _author_or_admin(user, fn):
        raise HTTPException(status_code=403)
    client = WorkerClient()
    extract = await client.extract_meta(payload.code)
    if extract.get("errors"):
        raise HTTPException(
            status_code=400, detail={"extract_errors": extract["errors"]}
        )
    version = fn_crud.save_version(
        db,
        fn.id,
        editor_user_id=user.id,
        code=payload.code,
        commit_message=payload.commit_message,
        actions_meta=extract["actions_meta_json"],
        valves_schema=extract["valves_schema_json"],
        metadata=extract["metadata_json"],
    )
    db.commit()
    db.refresh(version)
    return version


@router.get(
    "/{slug}/versions", response_model=list[VersionRead]
)
def list_versions(
    slug: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    fn = fn_crud.get_function_by_slug(db, slug)
    if fn is None:
        raise HTTPException(status_code=404)
    if not _can_view_code(user, fn):
        raise HTTPException(status_code=403)
    return fn_crud.list_versions(db, fn.id)


@router.get(
    "/{slug}/versions/{version_no}", response_model=VersionRead
)
def get_version(
    slug: str,
    version_no: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    fn = fn_crud.get_function_by_slug(db, slug)
    if fn is None:
        raise HTTPException(status_code=404)
    if not _can_view_code(user, fn):
        raise HTTPException(status_code=403)
    version = fn_crud.get_version(db, fn.id, version_no)
    if version is None:
        raise HTTPException(status_code=404)
    return version
