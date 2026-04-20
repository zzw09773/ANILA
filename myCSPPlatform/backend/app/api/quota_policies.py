"""Admin CRUD for quota policies + assignment to users / API keys."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.auth import get_current_user
from app.database import get_db
from app.models.api_key import ApiKey
from app.models.quota_policy import QuotaPolicy
from app.models.user import User

router = APIRouter(prefix="/api/quota-policies", tags=["quota-policies"])


# ── Schemas ───────────────────────────────────────────────────────────────────

class QuotaPolicyCreate(BaseModel):
    name: str = Field(..., max_length=100)
    description: Optional[str] = Field(None, max_length=500)
    token_limit_per_day: Optional[int] = Field(None, ge=1)
    token_limit_per_month: Optional[int] = Field(None, ge=1)
    request_limit_per_minute: Optional[int] = Field(None, ge=1)
    request_limit_per_hour: Optional[int] = Field(None, ge=1)
    is_default: bool = False


class QuotaPolicyUpdate(BaseModel):
    description: Optional[str] = Field(None, max_length=500)
    token_limit_per_day: Optional[int] = Field(None, ge=1)
    token_limit_per_month: Optional[int] = Field(None, ge=1)
    request_limit_per_minute: Optional[int] = Field(None, ge=1)
    request_limit_per_hour: Optional[int] = Field(None, ge=1)
    is_default: Optional[bool] = None


class QuotaPolicyOut(BaseModel):
    id: int
    name: str
    description: Optional[str]
    token_limit_per_day: Optional[int]
    token_limit_per_month: Optional[int]
    request_limit_per_minute: Optional[int]
    request_limit_per_hour: Optional[int]
    is_default: bool
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class AssignPolicyRequest(BaseModel):
    quota_policy_id: Optional[int] = None


# ── Helpers ───────────────────────────────────────────────────────────────────

def _require_admin(current_user: User) -> None:
    if current_user.role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="需要管理員權限")


def _get_policy_or_404(db: Session, policy_id: int) -> QuotaPolicy:
    policy = db.query(QuotaPolicy).filter(QuotaPolicy.id == policy_id).first()
    if not policy:
        raise HTTPException(status_code=404, detail="找不到此配額政策")
    return policy


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("", response_model=list[QuotaPolicyOut])
def list_policies(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_admin(current_user)
    return db.query(QuotaPolicy).order_by(QuotaPolicy.id).all()


@router.post("", response_model=QuotaPolicyOut, status_code=201)
def create_policy(
    body: QuotaPolicyCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_admin(current_user)
    if db.query(QuotaPolicy).filter(QuotaPolicy.name == body.name).first():
        raise HTTPException(status_code=409, detail="此名稱的配額政策已存在")
    if body.is_default:
        db.query(QuotaPolicy).filter(QuotaPolicy.is_default == True).update(
            {"is_default": False}
        )
    policy = QuotaPolicy(**body.model_dump())
    db.add(policy)
    db.commit()
    db.refresh(policy)
    return policy


@router.get("/{policy_id}", response_model=QuotaPolicyOut)
def get_policy(
    policy_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_admin(current_user)
    return _get_policy_or_404(db, policy_id)


@router.put("/{policy_id}", response_model=QuotaPolicyOut)
def update_policy(
    policy_id: int,
    body: QuotaPolicyUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_admin(current_user)
    policy = _get_policy_or_404(db, policy_id)
    if body.is_default:
        db.query(QuotaPolicy).filter(
            QuotaPolicy.is_default == True, QuotaPolicy.id != policy_id
        ).update({"is_default": False})
    for field_name, value in body.model_dump(exclude_unset=True).items():
        setattr(policy, field_name, value)
    policy.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(policy)
    return policy


@router.delete("/{policy_id}", status_code=204)
def delete_policy(
    policy_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_admin(current_user)
    policy = _get_policy_or_404(db, policy_id)
    db.delete(policy)
    db.commit()


# ── Assignment endpoints ───────────────────────────────────────────────────────

@router.put("/assign/user/{user_id}", status_code=200)
def assign_to_user(
    user_id: int,
    body: AssignPolicyRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_admin(current_user)
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="找不到此使用者")
    if body.quota_policy_id is not None:
        _get_policy_or_404(db, body.quota_policy_id)
    user.quota_policy_id = body.quota_policy_id
    db.commit()
    return {"user_id": user_id, "quota_policy_id": body.quota_policy_id}


@router.put("/assign/api-key/{key_id}", status_code=200)
def assign_to_api_key(
    key_id: int,
    body: AssignPolicyRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_admin(current_user)
    key = db.query(ApiKey).filter(ApiKey.id == key_id).first()
    if not key:
        raise HTTPException(status_code=404, detail="找不到此 API Key")
    if body.quota_policy_id is not None:
        _get_policy_or_404(db, body.quota_policy_id)
    key.quota_policy_id = body.quota_policy_id
    db.commit()
    return {"api_key_id": key_id, "quota_policy_id": body.quota_policy_id}
