"""模型單價與平台貨幣。改價只新增一列。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.model_price import ModelPrice
from app.models.model_registry import ModelRegistry
from app.models.platform_setting import PlatformSetting
from app.models.user import User
from app.services.audit_service import log_audit_event_or_raise
from app.services.auth_service import get_current_user, require_admin
from app.services.pricing import (
    CURRENCY_KEY,
    billing_currency_locked,
    format_micros,
    get_billing_currency,
    money_to_micros,
)

_BACKDATE_LIMIT = timedelta(minutes=5)

router = APIRouter(tags=["計價"])


class PriceWrite(BaseModel):
    input_per_million: float | int | str | None = None
    output_per_million: float | int | str | None = None
    reasoning_per_million: float | int | str | None = None
    effective_at: datetime | None = None


class CurrencyWrite(BaseModel):
    currency: str

    @field_validator("currency")
    @classmethod
    def _code(cls, value: str) -> str:
        code = value.strip().upper()
        if len(code) != 3 or not code.isalpha():
            raise ValueError("貨幣代碼須為三個英文字母")
        return code


def _price_body(row: ModelPrice) -> dict:
    return {
        "id": row.id,
        "model_id": row.model_id,
        "input_per_million": format_micros(row.input_micros),
        "output_per_million": format_micros(row.output_micros),
        "reasoning_per_million": format_micros(row.reasoning_micros),
        "effective_at": row.effective_at,
        "created_at": row.created_at,
        "created_by_user_id": row.created_by_user_id,
    }


def _parse_price(value, label: str) -> int | None:
    try:
        return money_to_micros(value)
    except (ValueError, ArithmeticError) as exc:
        raise HTTPException(status_code=400, detail=f"{label}單價不可為負") from exc


@router.get("/api/models/{model_id}/prices")
def list_prices(
    model_id: int,
    _admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    model = db.get(ModelRegistry, model_id)
    if model is None:
        raise HTTPException(status_code=404, detail="找不到模型")
    rows = (
        db.query(ModelPrice)
        .filter(ModelPrice.model_id == model_id)
        .order_by(ModelPrice.effective_at.desc(), ModelPrice.id.desc())
        .all()
    )
    return [_price_body(row) for row in rows]


@router.post("/api/models/{model_id}/prices")
def add_price(
    model_id: int,
    body: PriceWrite,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    model = db.get(ModelRegistry, model_id)
    if model is None:
        raise HTTPException(status_code=404, detail="找不到模型")
    now = datetime.now(timezone.utc)
    effective = body.effective_at or now
    if effective.tzinfo is None:
        effective = effective.replace(tzinfo=timezone.utc)
    if effective < now - _BACKDATE_LIMIT:
        raise HTTPException(status_code=400, detail="生效時間不能早於現在五分鐘以上")
    row = ModelPrice(
        model_id=model_id,
        input_micros=_parse_price(body.input_per_million, "輸入"),
        output_micros=_parse_price(body.output_per_million, "輸出"),
        reasoning_micros=_parse_price(body.reasoning_per_million, "思考"),
        effective_at=effective,
        created_by_user_id=admin.id,
    )
    db.add(row)
    db.flush()
    log_audit_event_or_raise(
        db,
        action="model.price.set",
        resource_type="model",
        actor=admin,
        resource_id=model_id,
        metadata={
            "price_id": row.id,
            "input_micros": row.input_micros,
            "output_micros": row.output_micros,
            "reasoning_micros": row.reasoning_micros,
            "effective_at": effective.isoformat(),
        },
        commit=False,
    )
    db.commit()
    db.refresh(row)
    return _price_body(row)


@router.get("/api/billing/currency")
def read_currency(
    _user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return {"currency": get_billing_currency(db), "locked": billing_currency_locked(db)}


@router.put("/api/billing/currency")
def write_currency(
    body: CurrencyWrite,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    current = get_billing_currency(db)
    if body.currency == current:
        return {"currency": current}
    if billing_currency_locked(db):
        raise HTTPException(
            status_code=409,
            detail="已有單價或金額額度，不能再改平台貨幣",
        )
    row = (
        db.query(PlatformSetting)
        .filter(PlatformSetting.key == CURRENCY_KEY)
        .first()
    )
    if row is None:
        row = PlatformSetting(
            key=CURRENCY_KEY,
            value=body.currency,
            updated_by_user_id=admin.id,
        )
        db.add(row)
    else:
        row.value = body.currency
        row.updated_by_user_id = admin.id
    log_audit_event_or_raise(
        db,
        action="billing.currency.set",
        resource_type="platform_setting",
        actor=admin,
        resource_id=CURRENCY_KEY,
        metadata={"currency": body.currency},
        commit=False,
    )
    db.commit()
    return {"currency": body.currency}
