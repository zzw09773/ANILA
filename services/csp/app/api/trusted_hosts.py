"""``/api/trusted-hosts`` — admin allow-list for SSRF guard bypass.

Wraps :mod:`app.services.trusted_host_service`. Authorisation policy:

* **GET** (list / get-by-id): admin-tier (admin + owner). Knowing which
  hosts are trusted is a transparency requirement — admins debugging
  why a model registration failed should be able to see the list.
* **POST / DELETE** (mutations): **owner-only**. Growing the allow-list
  loosens the SSRF guard's protection, so it stays at the same tier as
  other platform-altering ops (auth-provider editing, model purge, raw
  audit-log access).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.user import User
from app.schemas.trusted_host import TrustedHostCreate, TrustedHostResponse
from app.services import trusted_host_service
from app.services.auth_service import require_admin, require_owner

router = APIRouter(prefix="/api/trusted-hosts", tags=["受信任主機"])


def _serialize(row) -> dict:
    created_by_username = row.created_by.username if row.created_by else None
    return {
        "id": row.id,
        "host": row.host,
        "note": row.note,
        "created_by_user_id": row.created_by_user_id,
        "created_by_username": created_by_username,
        "created_at": row.created_at,
    }


@router.get("", response_model=list[TrustedHostResponse])
def list_trusted_hosts(
    _: User = Depends(require_admin),  # admin-tier visibility (admin + owner)
    db: Session = Depends(get_db),
):
    rows = trusted_host_service.list_hosts(db)
    return [_serialize(r) for r in rows]


@router.post("", response_model=TrustedHostResponse, status_code=status.HTTP_201_CREATED)
def create_trusted_host(
    payload: TrustedHostCreate,
    request: Request,  # noqa: ARG001 — reserved for future audit IP enrichment
    owner: User = Depends(require_owner),
    db: Session = Depends(get_db),
):
    row = trusted_host_service.add_host(
        db,
        host=payload.host,
        note=payload.note,
        actor=owner,
    )
    return _serialize(row)


@router.delete("/{host_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_trusted_host(
    host_id: int,
    request: Request,  # noqa: ARG001 — see above
    owner: User = Depends(require_owner),
    db: Session = Depends(get_db),
):
    removed = trusted_host_service.remove_host(db, host_id=host_id, actor=owner)
    if not removed:
        raise HTTPException(status_code=404, detail="trusted host 不存在")
    return None
