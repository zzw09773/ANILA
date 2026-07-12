# -*- coding: utf-8 -*-
"""Gate 2 G2a clearance authority and canonical data-access evaluator.

The evaluator never combines authority across grants.  One active grant must
simultaneously satisfy classification, every required compartment, collection
membership, and need-to-know.  Platform ``admin``/``owner`` roles are relevant
only to management APIs; they are not data clearance.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone

from anila_contracts import Classification
from sqlalchemy.orm import Session

from app.models.audit_log import AuditLog
from app.models.clearance import (
    ClearanceGrant,
    ClearanceGrantCompartment,
    CollectionAccessGrant,
    CollectionRequiredCompartment,
    DocumentRequiredCompartment,
    SecurityCompartment,
)
from app.models.ingestion import IngestionCollection, IngestionDocument
from app.models.user import User
from app.services.audit_service import log_audit_event


class ClearancePolicyDataError(ValueError):
    """Persisted policy data is unknown/incomplete; callers must deny access."""


_COMPARTMENT_CODE_RE = re.compile(r"^[A-Z0-9][A-Z0-9_.-]{0,63}$")


@dataclass(frozen=True, slots=True)
class DataAccessContext:
    user_id: int
    collection_id: int
    document_id: int | None
    required_classification: Classification
    required_compartment_ids: frozenset[int]
    is_collection_owner: bool
    evaluated_at: datetime

    def __post_init__(self) -> None:
        if self.user_id <= 0 or self.collection_id <= 0:
            raise ValueError("DataAccessContext IDs 必須為正整數")
        if self.document_id is not None and self.document_id <= 0:
            raise ValueError("document_id 必須為正整數")
        if not isinstance(self.required_classification, Classification):
            raise TypeError("required_classification 必須是 canonical Classification")
        if not isinstance(self.required_compartment_ids, frozenset) or any(
            compartment_id <= 0
            for compartment_id in self.required_compartment_ids
        ):
            raise ValueError("required_compartment_ids 必須是正整數 frozenset")
        if not isinstance(self.is_collection_owner, bool):
            raise TypeError("is_collection_owner 必須是 bool")
        _required_aware_datetime(self.evaluated_at, field_name="evaluated_at")


@dataclass(frozen=True, slots=True)
class DataAccessDecision:
    allowed: bool
    reason_code: str
    context: DataAccessContext
    clearance_grant_id: int | None = None
    collection_access_grant_id: int | None = None


def _stored_datetime_as_utc(value: object, *, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        raise ClearancePolicyDataError(f"{field_name} 缺失或不是 datetime")
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _required_aware_datetime(value: datetime, *, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} 必須帶時區")
    return value.astimezone(timezone.utc)


def _classification_from_storage(
    raw: object, *, field_name: str
) -> Classification:
    if isinstance(raw, Classification):
        return raw
    if not isinstance(raw, str):
        raise ClearancePolicyDataError(f"{field_name} classification 為 NULL/非字串")
    try:
        return Classification.from_storage(raw)
    except ValueError as exc:
        raise ClearancePolicyDataError(
            f"{field_name} 含未知 classification:{raw!r}"
        ) from exc


def _assert_manager(actor: User) -> None:
    if (
        actor.role not in ("admin", "owner")
        or not actor.is_active
        or not isinstance(actor.id, int)
        or actor.id <= 0
    ):
        raise PermissionError("clearance 管理只允許 admin/owner")


def _basis_ticket(value: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError("basis_ticket 必填")
    if len(normalized) > 255:
        raise ValueError("basis_ticket 不得超過 255 字元")
    return normalized


def _audit_required(
    db: Session,
    *,
    action: str,
    resource_type: str,
    resource_id: int,
    actor: User,
    detail: str,
    metadata: dict[str, object],
) -> AuditLog:
    event = log_audit_event(
        db,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        actor=actor,
        detail=detail,
        metadata=metadata,
    )
    if event is None:
        raise RuntimeError("clearance audit event 寫入失敗")
    return event


def create_security_compartment(
    db: Session,
    *,
    actor: User,
    code: str,
    name: str,
    description: str | None = None,
) -> SecurityCompartment:
    _assert_manager(actor)
    normalized_code = code.strip().upper()
    normalized_name = name.strip()
    if _COMPARTMENT_CODE_RE.fullmatch(normalized_code) is None:
        raise ValueError("compartment code 必須是 1-64 字元的大寫識別碼")
    if not normalized_name:
        raise ValueError("compartment name 必填")
    if len(normalized_name) > 200:
        raise ValueError("compartment name 不得超過 200 字元")
    if db.query(SecurityCompartment).filter_by(code=normalized_code).first():
        raise ValueError(f"compartment code 已存在:{normalized_code}")
    row = SecurityCompartment(
        code=normalized_code,
        name=normalized_name,
        description=description,
        created_by_user_id=actor.id,
    )
    db.add(row)
    db.flush()
    _audit_required(
        db,
        action="clearance.compartment_created",
        resource_type="security_compartment",
        resource_id=row.id,
        actor=actor,
        detail=f"建立 compartment {normalized_code}",
        metadata={"compartment_id": row.id, "code": normalized_code},
    )
    db.commit()
    db.refresh(row)
    return row


def issue_clearance_grant(
    db: Session,
    *,
    actor: User,
    subject_user_id: int,
    max_classification_level: str | Classification,
    valid_from: datetime,
    expires_at: datetime,
    basis_ticket: str,
) -> ClearanceGrant:
    _assert_manager(actor)
    subject = db.get(User, subject_user_id)
    if subject is None or not subject.is_active:
        raise LookupError("clearance subject 使用者不存在或未啟用")
    level = _classification_from_storage(
        max_classification_level,
        field_name="clearance_grant.max_classification_level",
    )
    starts = _required_aware_datetime(valid_from, field_name="valid_from")
    expires = _required_aware_datetime(expires_at, field_name="expires_at")
    if expires <= starts:
        raise ValueError("expires_at 必須晚於 valid_from")
    ticket = _basis_ticket(basis_ticket)
    row = ClearanceGrant(
        subject_user_id=subject.id,
        max_classification_level=level.to_storage(),
        valid_from=starts,
        expires_at=expires,
        basis_ticket=ticket,
        issued_by_user_id=actor.id,
    )
    db.add(row)
    db.flush()
    _audit_required(
        db,
        action="clearance.grant_issued",
        resource_type="clearance_grant",
        resource_id=row.id,
        actor=actor,
        detail=f"核發 clearance grant 給 user#{subject.id}",
        metadata={
            "clearance_grant_id": row.id,
            "subject_user_id": subject.id,
            "max_classification_level": level.to_storage(),
            "basis_ticket": ticket,
        },
    )
    db.commit()
    db.refresh(row)
    return row


def revoke_clearance_grant(
    db: Session,
    *,
    actor: User,
    clearance_grant_id: int,
    now: datetime | None = None,
) -> ClearanceGrant:
    _assert_manager(actor)
    row = db.get(ClearanceGrant, clearance_grant_id)
    if row is None:
        raise LookupError("找不到 clearance grant")
    if row.revoked_at is not None:
        return row
    revoked_at = _required_aware_datetime(
        now or datetime.now(timezone.utc), field_name="revoked_at"
    )
    row.revoked_at = revoked_at
    row.revoked_by_user_id = actor.id
    _audit_required(
        db,
        action="clearance.grant_revoked",
        resource_type="clearance_grant",
        resource_id=row.id,
        actor=actor,
        detail=f"撤銷 clearance grant#{row.id}",
        metadata={
            "clearance_grant_id": row.id,
            "subject_user_id": row.subject_user_id,
        },
    )
    db.commit()
    db.refresh(row)
    return row


def add_grant_compartment(
    db: Session,
    *,
    actor: User,
    clearance_grant_id: int,
    compartment_id: int,
) -> ClearanceGrantCompartment:
    _assert_manager(actor)
    grant = db.get(ClearanceGrant, clearance_grant_id)
    if grant is None or grant.revoked_at is not None:
        raise LookupError("clearance grant 不存在或已撤銷")
    compartment = db.get(SecurityCompartment, compartment_id)
    if compartment is None or not compartment.is_active:
        raise LookupError("compartment 不存在或未啟用")
    existing = db.get(
        ClearanceGrantCompartment,
        {"clearance_grant_id": grant.id, "compartment_id": compartment.id},
    )
    if existing is not None:
        return existing
    row = ClearanceGrantCompartment(
        clearance_grant_id=grant.id,
        compartment_id=compartment.id,
    )
    db.add(row)
    _audit_required(
        db,
        action="clearance.grant_compartment_added",
        resource_type="clearance_grant",
        resource_id=grant.id,
        actor=actor,
        detail=f"grant#{grant.id} 加入 compartment#{compartment.id}",
        metadata={
            "clearance_grant_id": grant.id,
            "compartment_id": compartment.id,
        },
    )
    db.commit()
    db.refresh(row)
    return row


def grant_collection_access(
    db: Session,
    *,
    actor: User,
    clearance_grant_id: int,
    collection_id: int,
    membership_granted: bool,
    need_to_know: bool,
    basis_ticket: str,
) -> CollectionAccessGrant:
    _assert_manager(actor)
    if not membership_granted and not need_to_know:
        raise ValueError("collection grant 至少要包含 membership 或 need-to-know")
    grant = db.get(ClearanceGrant, clearance_grant_id)
    if grant is None or grant.revoked_at is not None:
        raise LookupError("clearance grant 不存在或已撤銷")
    collection = db.get(IngestionCollection, collection_id)
    if collection is None:
        raise LookupError("collection 不存在")
    existing = (
        db.query(CollectionAccessGrant)
        .filter_by(clearance_grant_id=grant.id, collection_id=collection.id)
        .first()
    )
    if existing is not None:
        raise ValueError("此 clearance grant 已有該 collection 的 access row")
    ticket = _basis_ticket(basis_ticket)
    row = CollectionAccessGrant(
        clearance_grant_id=grant.id,
        collection_id=collection.id,
        membership_granted=membership_granted,
        need_to_know=need_to_know,
        basis_ticket=ticket,
        issued_by_user_id=actor.id,
    )
    db.add(row)
    db.flush()
    _audit_required(
        db,
        action="clearance.collection_access_granted",
        resource_type="collection_access_grant",
        resource_id=row.id,
        actor=actor,
        detail=f"grant#{grant.id} 綁定 collection#{collection.id}",
        metadata={
            "collection_access_grant_id": row.id,
            "clearance_grant_id": grant.id,
            "collection_id": collection.id,
            "membership_granted": membership_granted,
            "need_to_know": need_to_know,
            "basis_ticket": ticket,
        },
    )
    db.commit()
    db.refresh(row)
    return row


def revoke_collection_access(
    db: Session,
    *,
    actor: User,
    collection_access_grant_id: int,
    now: datetime | None = None,
) -> CollectionAccessGrant:
    _assert_manager(actor)
    row = db.get(CollectionAccessGrant, collection_access_grant_id)
    if row is None:
        raise LookupError("找不到 collection access grant")
    if row.revoked_at is not None:
        return row
    row.revoked_at = _required_aware_datetime(
        now or datetime.now(timezone.utc), field_name="revoked_at"
    )
    row.revoked_by_user_id = actor.id
    _audit_required(
        db,
        action="clearance.collection_access_revoked",
        resource_type="collection_access_grant",
        resource_id=row.id,
        actor=actor,
        detail=f"撤銷 collection access grant#{row.id}",
        metadata={
            "collection_access_grant_id": row.id,
            "clearance_grant_id": row.clearance_grant_id,
            "collection_id": row.collection_id,
        },
    )
    db.commit()
    db.refresh(row)
    return row


def _active_compartment(db: Session, compartment_id: int) -> SecurityCompartment:
    row = db.get(SecurityCompartment, compartment_id)
    if row is None or not row.is_active:
        raise LookupError("compartment 不存在或未啟用")
    return row


def assign_collection_required_compartment(
    db: Session,
    *,
    actor: User,
    collection_id: int,
    compartment_id: int,
    basis_ticket: str,
) -> CollectionRequiredCompartment:
    _assert_manager(actor)
    if db.get(IngestionCollection, collection_id) is None:
        raise LookupError("collection 不存在")
    _active_compartment(db, compartment_id)
    existing = db.get(
        CollectionRequiredCompartment,
        {"collection_id": collection_id, "compartment_id": compartment_id},
    )
    if existing is not None:
        return existing
    row = CollectionRequiredCompartment(
        collection_id=collection_id,
        compartment_id=compartment_id,
        basis_ticket=_basis_ticket(basis_ticket),
        assigned_by_user_id=actor.id,
    )
    db.add(row)
    _audit_required(
        db,
        action="clearance.collection_requirement_assigned",
        resource_type="ingestion_collection",
        resource_id=collection_id,
        actor=actor,
        detail=f"collection#{collection_id} 要求 compartment#{compartment_id}",
        metadata={
            "collection_id": collection_id,
            "compartment_id": compartment_id,
        },
    )
    db.commit()
    db.refresh(row)
    return row


def assign_document_required_compartment(
    db: Session,
    *,
    actor: User,
    document_id: int,
    compartment_id: int,
    basis_ticket: str,
) -> DocumentRequiredCompartment:
    _assert_manager(actor)
    if db.get(IngestionDocument, document_id) is None:
        raise LookupError("document 不存在")
    _active_compartment(db, compartment_id)
    existing = db.get(
        DocumentRequiredCompartment,
        {"document_id": document_id, "compartment_id": compartment_id},
    )
    if existing is not None:
        return existing
    row = DocumentRequiredCompartment(
        document_id=document_id,
        compartment_id=compartment_id,
        basis_ticket=_basis_ticket(basis_ticket),
        assigned_by_user_id=actor.id,
    )
    db.add(row)
    _audit_required(
        db,
        action="clearance.document_requirement_assigned",
        resource_type="ingestion_document",
        resource_id=document_id,
        actor=actor,
        detail=f"document#{document_id} 要求 compartment#{compartment_id}",
        metadata={
            "document_id": document_id,
            "compartment_id": compartment_id,
        },
    )
    db.commit()
    db.refresh(row)
    return row


def resolve_data_access_context(
    db: Session,
    *,
    user_id: int,
    collection_id: int,
    document_id: int | None = None,
    now: datetime | None = None,
) -> DataAccessContext:
    collection = db.get(IngestionCollection, collection_id)
    if collection is None:
        raise LookupError("collection 不存在")
    levels = [
        _classification_from_storage(
            collection.classification_level,
            field_name=f"collection#{collection.id}",
        )
    ]
    document: IngestionDocument | None = None
    if document_id is not None:
        document = (
            db.query(IngestionDocument)
            .filter_by(id=document_id, collection_id=collection.id)
            .first()
        )
        if document is None:
            raise LookupError("document 不存在或不屬於指定 collection")
        levels.append(
            _classification_from_storage(
                document.classification_level,
                field_name=f"document#{document.id}",
            )
        )

    required_compartments = {
        row[0]
        for row in db.query(CollectionRequiredCompartment.compartment_id)
        .filter(CollectionRequiredCompartment.collection_id == collection.id)
        .all()
    }
    if document is not None:
        required_compartments.update(
            row[0]
            for row in db.query(DocumentRequiredCompartment.compartment_id)
            .filter(DocumentRequiredCompartment.document_id == document.id)
            .all()
        )
    evaluated_at = _required_aware_datetime(
        now or datetime.now(timezone.utc), field_name="evaluated_at"
    )
    return DataAccessContext(
        user_id=user_id,
        collection_id=collection.id,
        document_id=document.id if document is not None else None,
        required_classification=Classification.max_of(levels),
        required_compartment_ids=frozenset(required_compartments),
        is_collection_owner=collection.created_by == user_id,
        evaluated_at=evaluated_at,
    )


def evaluate_data_access(
    db: Session, *, context: DataAccessContext
) -> DataAccessDecision:
    """Evaluate one immutable context without cross-grant composition."""

    authoritative = resolve_data_access_context(
        db,
        user_id=context.user_id,
        collection_id=context.collection_id,
        document_id=context.document_id,
        now=context.evaluated_at,
    )
    if authoritative != context:
        raise ClearancePolicyDataError(
            "DataAccessContext 與 authoritative collection/document 狀態不一致"
        )
    context = authoritative

    subject = db.get(User, context.user_id)
    if subject is None or not subject.is_active:
        return DataAccessDecision(False, "subject_inactive", context)

    grants = (
        db.query(ClearanceGrant)
        .filter(ClearanceGrant.subject_user_id == context.user_id)
        .order_by(ClearanceGrant.id.asc())
        .all()
    )
    parsed_grants: list[tuple[ClearanceGrant, Classification]] = []
    for grant in grants:
        # Parse every persisted grant before filtering dates/revocation. A
        # corrupted classification row denies the whole decision, even if a
        # second row would otherwise allow access.
        parsed_grants.append(
            (
                grant,
                _classification_from_storage(
                    grant.max_classification_level,
                    field_name=f"clearance_grant#{grant.id}",
                ),
            )
        )

    active: list[tuple[ClearanceGrant, Classification]] = []
    for grant, level in parsed_grants:
        valid_from = _stored_datetime_as_utc(
            grant.valid_from, field_name=f"clearance_grant#{grant.id}.valid_from"
        )
        expires_at = _stored_datetime_as_utc(
            grant.expires_at, field_name=f"clearance_grant#{grant.id}.expires_at"
        )
        if expires_at <= valid_from:
            raise ClearancePolicyDataError(
                f"clearance_grant#{grant.id} 時間窗非法"
            )
        if (
            grant.revoked_at is None
            and valid_from <= context.evaluated_at < expires_at
        ):
            active.append((grant, level))

    if not active:
        return DataAccessDecision(False, "no_active_clearance_grant", context)

    for grant, level in active:
        if level < context.required_classification:
            continue
        covered_compartments = {
            row[0]
            for row in db.query(ClearanceGrantCompartment.compartment_id)
            .filter(ClearanceGrantCompartment.clearance_grant_id == grant.id)
            .all()
        }
        if not context.required_compartment_ids.issubset(covered_compartments):
            continue
        access = (
            db.query(CollectionAccessGrant)
            .filter_by(
                clearance_grant_id=grant.id,
                collection_id=context.collection_id,
                revoked_at=None,
            )
            .first()
        )
        if access is None or not access.need_to_know:
            continue
        if not (context.is_collection_owner or access.membership_granted):
            continue
        return DataAccessDecision(
            True,
            "allowed",
            context,
            clearance_grant_id=grant.id,
            collection_access_grant_id=access.id,
        )
    return DataAccessDecision(
        False, "no_single_grant_satisfies_requirements", context
    )


def resolve_and_evaluate_data_access(
    db: Session,
    *,
    user_id: int,
    collection_id: int,
    document_id: int | None = None,
    now: datetime | None = None,
) -> DataAccessDecision:
    context = resolve_data_access_context(
        db,
        user_id=user_id,
        collection_id=collection_id,
        document_id=document_id,
        now=now,
    )
    return evaluate_data_access(db, context=context)


__all__ = [
    "ClearancePolicyDataError",
    "DataAccessContext",
    "DataAccessDecision",
    "add_grant_compartment",
    "assign_collection_required_compartment",
    "assign_document_required_compartment",
    "create_security_compartment",
    "evaluate_data_access",
    "grant_collection_access",
    "issue_clearance_grant",
    "resolve_and_evaluate_data_access",
    "resolve_data_access_context",
    "revoke_clearance_grant",
    "revoke_collection_access",
]
