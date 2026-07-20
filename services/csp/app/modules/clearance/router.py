# -*- coding: utf-8 -*-
"""Owner/admin clearance-management API.

These endpoints mutate governance records only.  ``require_admin`` grants no
data-read bypass; runtime access must call the canonical evaluator in
``service.py``.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.clearance import ClearanceGrant, SecurityCompartment
from app.models.user import User
from app.modules.clearance.service import (
    add_grant_compartment,
    assign_collection_required_compartment,
    assign_document_required_compartment,
    create_security_compartment,
    grant_collection_access,
    issue_clearance_grant,
    revoke_clearance_grant,
    revoke_collection_access,
)
from app.schemas.contracts.clearance import (
    ClearanceGrantCreate,
    ClearanceGrantOut,
    CollectionAccessGrantCreate,
    CollectionAccessGrantOut,
    GrantCompartmentOut,
    RequiredCompartmentAssign,
    RequiredCompartmentOut,
    SecurityCompartmentCreate,
    SecurityCompartmentOut,
)
from app.services.auth_service import require_admin

router = APIRouter(prefix="/api/clearance", tags=["資料 clearance"])


def _not_found_or_invalid(exc: Exception) -> HTTPException:
    if isinstance(exc, LookupError):
        return HTTPException(status_code=404, detail=str(exc))
    return HTTPException(status_code=422, detail=str(exc))


@router.get("/compartments", response_model=list[SecurityCompartmentOut])
def list_compartments(
    include_inactive: bool = False,
    _manager: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> list[SecurityCompartment]:
    query = db.query(SecurityCompartment).order_by(SecurityCompartment.code.asc())
    if not include_inactive:
        query = query.filter(SecurityCompartment.is_active.is_(True))
    return query.all()


@router.post(
    "/compartments", response_model=SecurityCompartmentOut, status_code=201
)
def create_compartment(
    body: SecurityCompartmentCreate,
    manager: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> SecurityCompartment:
    try:
        return create_security_compartment(
            db,
            actor=manager,
            code=body.code,
            name=body.name,
            description=body.description,
        )
    except (LookupError, PermissionError, ValueError) as exc:
        raise _not_found_or_invalid(exc) from None


@router.get("/grants", response_model=list[ClearanceGrantOut])
def list_clearance_grants(
    subject_user_id: int | None = Query(None, gt=0),
    _manager: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> list[ClearanceGrant]:
    query = db.query(ClearanceGrant).order_by(ClearanceGrant.id.desc())
    if subject_user_id is not None:
        query = query.filter(ClearanceGrant.subject_user_id == subject_user_id)
    return query.all()


@router.post("/grants", response_model=ClearanceGrantOut, status_code=201)
def create_clearance_grant(
    body: ClearanceGrantCreate,
    manager: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> ClearanceGrant:
    try:
        return issue_clearance_grant(
            db,
            actor=manager,
            subject_user_id=body.subject_user_id,
            max_classification_level=body.max_classification_level,
            valid_from=body.valid_from,
            expires_at=body.expires_at,
            basis_ticket=body.basis_ticket,
        )
    except (LookupError, PermissionError, ValueError) as exc:
        raise _not_found_or_invalid(exc) from None


@router.delete("/grants/{grant_id}", response_model=ClearanceGrantOut)
def delete_clearance_grant(
    grant_id: int,
    manager: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> ClearanceGrant:
    try:
        return revoke_clearance_grant(
            db, actor=manager, clearance_grant_id=grant_id
        )
    except (LookupError, PermissionError, ValueError) as exc:
        raise _not_found_or_invalid(exc) from None


@router.post(
    "/grants/{grant_id}/compartments/{compartment_id}",
    response_model=GrantCompartmentOut,
    status_code=201,
)
def create_grant_compartment(
    grant_id: int,
    compartment_id: int,
    manager: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    try:
        return add_grant_compartment(
            db,
            actor=manager,
            clearance_grant_id=grant_id,
            compartment_id=compartment_id,
        )
    except (LookupError, PermissionError, ValueError) as exc:
        raise _not_found_or_invalid(exc) from None


@router.post(
    "/grants/{grant_id}/collections/{collection_id}",
    response_model=CollectionAccessGrantOut,
    status_code=201,
)
def create_collection_access_grant(
    grant_id: int,
    collection_id: int,
    body: CollectionAccessGrantCreate,
    manager: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    try:
        return grant_collection_access(
            db,
            actor=manager,
            clearance_grant_id=grant_id,
            collection_id=collection_id,
            membership_granted=body.membership_granted,
            need_to_know=body.need_to_know,
            basis_ticket=body.basis_ticket,
        )
    except (LookupError, PermissionError, ValueError) as exc:
        raise _not_found_or_invalid(exc) from None


@router.delete(
    "/collection-access-grants/{access_grant_id}",
    response_model=CollectionAccessGrantOut,
)
def delete_collection_access_grant(
    access_grant_id: int,
    manager: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    try:
        return revoke_collection_access(
            db,
            actor=manager,
            collection_access_grant_id=access_grant_id,
        )
    except (LookupError, PermissionError, ValueError) as exc:
        raise _not_found_or_invalid(exc) from None


@router.post(
    "/collections/{collection_id}/required-compartments/{compartment_id}",
    response_model=RequiredCompartmentOut,
    status_code=201,
)
def create_collection_requirement(
    collection_id: int,
    compartment_id: int,
    body: RequiredCompartmentAssign,
    manager: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    try:
        return assign_collection_required_compartment(
            db,
            actor=manager,
            collection_id=collection_id,
            compartment_id=compartment_id,
            basis_ticket=body.basis_ticket,
        )
    except (LookupError, PermissionError, ValueError) as exc:
        raise _not_found_or_invalid(exc) from None


@router.post(
    "/documents/{document_id}/required-compartments/{compartment_id}",
    response_model=RequiredCompartmentOut,
    status_code=201,
)
def create_document_requirement(
    document_id: int,
    compartment_id: int,
    body: RequiredCompartmentAssign,
    manager: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    try:
        return assign_document_required_compartment(
            db,
            actor=manager,
            document_id=document_id,
            compartment_id=compartment_id,
            basis_ticket=body.basis_ticket,
        )
    except (LookupError, PermissionError, ValueError) as exc:
        raise _not_found_or_invalid(exc) from None


__all__ = ["router"]
