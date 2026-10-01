"""用量額度。管理員可改；單位管理員只能看自己單位。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.api_key import ApiKey
from app.models.department import Department
from app.models.usage_quota import UsageQuota
from app.models.user import User
from app.services.audit_service import log_audit_event_or_raise
from app.services.auth_service import get_current_user, is_admin_tier
from app.services.pricing import format_micros, get_billing_currency, unpriced_models_for_subject
from app.services.quota_service import unpriced_usage_count
from app.services.unit_admin_service import get_unit_admin_scope_ids, is_unit_admin

router = APIRouter(prefix="/api/quotas", tags=["額度"])

_SCOPES = {"user", "unit", "api_key"}
_PERIODS = {"daily", "monthly"}
_METRICS = {"tokens", "cost"}
_ON_LIMIT = {"warn", "block"}


class QuotaWrite(BaseModel):
    scope_type: str
    user_id: int | None = None
    department_id: int | None = None
    api_key_id: int | None = None
    period: str
    metric: str
    limit_value: int | None = None
    warn_percent: int = Field(default=80, ge=1, le=100)
    on_limit: str = "warn"


class QuotaUpdate(BaseModel):
    limit_value: int | None = None
    warn_percent: int | None = Field(default=None, ge=1, le=100)
    on_limit: str | None = None
    clear_limit: bool = False


def _forbid_mutation(db: Session, user: User) -> None:
    if is_admin_tier(user):
        return
    if is_unit_admin(db, user):
        raise HTTPException(status_code=403, detail="單位管理員只能查看額度，不能修改")
    raise HTTPException(status_code=403, detail="需要管理員權限")


def _require_reader(db: Session, user: User) -> list[int] | None:
    if is_admin_tier(user):
        return None
    scope = get_unit_admin_scope_ids(db, user)
    if scope is None:
        raise HTTPException(status_code=403, detail="需要管理員權限")
    return sorted(scope)


def _in_scope(db: Session, quota: UsageQuota, scope: set[int]) -> bool:
    if quota.scope_type == "unit":
        return quota.department_id in scope
    if quota.scope_type == "user" and quota.user_id:
        owner = db.get(User, quota.user_id)
        return owner is not None and owner.department_id in scope
    if quota.scope_type == "api_key" and quota.api_key_id:
        key = db.get(ApiKey, quota.api_key_id)
        if key is None:
            return False
        owner = db.get(User, key.user_id)
        return owner is not None and owner.department_id in scope
    return False


def _validate_identity(db: Session, body: QuotaWrite) -> None:
    if body.scope_type not in _SCOPES:
        raise HTTPException(status_code=400, detail="額度對象只接受使用者、單位或 API 金鑰")
    if body.period not in _PERIODS:
        raise HTTPException(status_code=400, detail="期間只接受每日或每月")
    if body.metric not in _METRICS:
        raise HTTPException(status_code=400, detail="計量只接受 token 或金額")
    if body.on_limit not in _ON_LIMIT:
        raise HTTPException(status_code=400, detail="到上限時只接受提醒或擋下")
    if body.limit_value is not None and body.limit_value < 0:
        raise HTTPException(status_code=400, detail="上限不可為負")
    ids = {
        "user": body.user_id,
        "unit": body.department_id,
        "api_key": body.api_key_id,
    }
    if ids[body.scope_type] is None:
        raise HTTPException(status_code=400, detail="請指定額度對象")
    extras = [name for name, value in ids.items() if name != body.scope_type and value is not None]
    if extras:
        raise HTTPException(status_code=400, detail="一條額度只能有一種對象")
    if body.scope_type == "user" and db.get(User, body.user_id) is None:
        raise HTTPException(status_code=404, detail="找不到使用者")
    if body.scope_type == "unit" and db.get(Department, body.department_id) is None:
        raise HTTPException(status_code=404, detail="找不到單位")
    if body.scope_type == "api_key" and db.get(ApiKey, body.api_key_id) is None:
        raise HTTPException(status_code=404, detail="找不到 API 金鑰")
    if body.metric == "cost":
        missing = unpriced_models_for_subject(
            db,
            scope_type=body.scope_type,
            user_id=body.user_id,
            department_id=body.department_id,
            api_key_id=body.api_key_id,
        )
        if missing:
            names = "、".join(missing)
            raise HTTPException(
                status_code=400,
                detail=f"金額額度需要這個對象可用的模型都已完整計價：{names}",
            )


def _duplicate(db: Session, body: QuotaWrite, ignore_id: int | None = None) -> bool:
    query = db.query(UsageQuota).filter(
        UsageQuota.scope_type == body.scope_type,
        UsageQuota.period == body.period,
        UsageQuota.metric == body.metric,
    )
    if body.scope_type == "user":
        query = query.filter(UsageQuota.user_id == body.user_id)
    elif body.scope_type == "unit":
        query = query.filter(UsageQuota.department_id == body.department_id)
    else:
        query = query.filter(UsageQuota.api_key_id == body.api_key_id)
    if ignore_id is not None:
        query = query.filter(UsageQuota.id != ignore_id)
    return query.first() is not None


def _quota_body(db: Session, quota: UsageQuota) -> dict:
    username = None
    department_name = None
    api_key_name = None
    if quota.user_id:
        user = db.get(User, quota.user_id)
        username = user.username if user is not None else None
    if quota.department_id:
        dept = db.get(Department, quota.department_id)
        department_name = dept.name if dept is not None else None
    if quota.api_key_id:
        key = db.get(ApiKey, quota.api_key_id)
        api_key_name = key.name if key is not None else None
    currency = get_billing_currency(db)
    if quota.limit_value is None:
        limit_display = None
    elif quota.metric == "cost":
        limit_display = format_micros(int(quota.limit_value))
    else:
        limit_display = str(int(quota.limit_value))
    body = {
        "id": quota.id,
        "scope_type": quota.scope_type,
        "user_id": quota.user_id,
        "username": username,
        "department_id": quota.department_id,
        "department_name": department_name,
        "api_key_id": quota.api_key_id,
        "api_key_name": api_key_name,
        "period": quota.period,
        "metric": quota.metric,
        "limit_value": quota.limit_value,
        "limit_display": limit_display,
        "warn_percent": quota.warn_percent,
        "on_limit": quota.on_limit,
        "currency": currency,
        "unpriced_count": 0,
        "unpriced_message": None,
    }
    if quota.metric == "cost":
        count = unpriced_usage_count(db, quota)
        body["unpriced_count"] = count
        if count:
            body["unpriced_message"] = f"期間內有 {count} 筆未計價用量，未計入金額"
    return body


@router.get("")
def list_quotas(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    scope_ids = _require_reader(db, current_user)
    rows = db.query(UsageQuota).order_by(UsageQuota.id).all()
    if scope_ids is not None:
        scope = set(scope_ids)
        rows = [row for row in rows if _in_scope(db, row, scope)]
    return [_quota_body(db, row) for row in rows]


@router.post("")
def create_quota(
    body: QuotaWrite,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _forbid_mutation(db, current_user)
    _validate_identity(db, body)
    if _duplicate(db, body):
        raise HTTPException(status_code=409, detail="同一對象、期間與計量已有額度")
    quota = UsageQuota(
        scope_type=body.scope_type,
        user_id=body.user_id if body.scope_type == "user" else None,
        department_id=body.department_id if body.scope_type == "unit" else None,
        api_key_id=body.api_key_id if body.scope_type == "api_key" else None,
        period=body.period,
        metric=body.metric,
        limit_value=body.limit_value,
        warn_percent=body.warn_percent,
        on_limit=body.on_limit,
        created_by_user_id=current_user.id,
        updated_by_user_id=current_user.id,
    )
    try:
        db.add(quota)
        db.flush()
        log_audit_event_or_raise(
            db,
            action="quota.create",
            resource_type="usage_quota",
            actor=current_user,
            resource_id=quota.id,
            metadata=body.model_dump(),
            commit=False,
        )
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="同一對象、期間與計量已有額度") from exc
    db.refresh(quota)
    return _quota_body(db, quota)


@router.get("/price-coverage")
def price_coverage(
    scope_type: str,
    user_id: int | None = None,
    department_id: int | None = None,
    api_key_id: int | None = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """這個對象可用的模型是否都已完整計價。金額額度只能在 ready 時建立。"""
    _require_reader(db, current_user)
    if scope_type not in _SCOPES:
        raise HTTPException(status_code=400, detail="額度對象只接受使用者、單位或 API 金鑰")
    missing = unpriced_models_for_subject(
        db,
        scope_type=scope_type,
        user_id=user_id,
        department_id=department_id,
        api_key_id=api_key_id,
    )
    return {"ready": not missing, "missing": missing}


@router.put("/{quota_id}")
def update_quota(
    quota_id: int,
    body: QuotaUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _forbid_mutation(db, current_user)
    quota = db.get(UsageQuota, quota_id)
    if quota is None:
        raise HTTPException(status_code=404, detail="找不到額度")
    if body.on_limit is not None and body.on_limit not in _ON_LIMIT:
        raise HTTPException(status_code=400, detail="到上限時只接受提醒或擋下")
    if body.limit_value is not None and body.limit_value < 0:
        raise HTTPException(status_code=400, detail="上限不可為負")
    if body.clear_limit:
        quota.limit_value = None
    elif body.limit_value is not None:
        quota.limit_value = body.limit_value
    if body.warn_percent is not None:
        quota.warn_percent = body.warn_percent
    if body.on_limit is not None:
        quota.on_limit = body.on_limit
    quota.updated_by_user_id = current_user.id
    log_audit_event_or_raise(
        db,
        action="quota.update",
        resource_type="usage_quota",
        actor=current_user,
        resource_id=quota.id,
        metadata={
            "limit_value": quota.limit_value,
            "warn_percent": quota.warn_percent,
            "on_limit": quota.on_limit,
        },
        commit=False,
    )
    db.commit()
    db.refresh(quota)
    return _quota_body(db, quota)


@router.delete("/{quota_id}")
def delete_quota(
    quota_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _forbid_mutation(db, current_user)
    quota = db.get(UsageQuota, quota_id)
    if quota is None:
        raise HTTPException(status_code=404, detail="找不到額度")
    log_audit_event_or_raise(
        db,
        action="quota.delete",
        resource_type="usage_quota",
        actor=current_user,
        resource_id=quota.id,
        metadata={"scope_type": quota.scope_type, "period": quota.period, "metric": quota.metric},
        commit=False,
    )
    db.delete(quota)
    db.commit()
    return {"ok": True}
