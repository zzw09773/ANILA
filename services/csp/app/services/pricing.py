"""查詢當下才計成本。每一筆用量用「呼叫當時生效」的單價。

輸入與輸出都有價，模型才算已計價。思考沒填時沿用輸出價。
缺任何一邊、或當時還沒有價格列，成本是 None（未計價），不是 0。
群組成本先把未捨去的分子加總，再一次換成 micros。
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy.orm import Session

from app.models.model_price import ModelPrice
from app.models.model_registry import ModelRegistry
from app.models.platform_setting import PlatformSetting
from app.services.auto_seed import PLATFORM_ROUTER_NAME

CURRENCY_KEY = "billing.currency"
DEFAULT_CURRENCY = "TWD"
_MICROS = Decimal(1_000_000)


def aware_utc(dt: datetime | None) -> datetime:
    if dt is None:
        return datetime.now(timezone.utc)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def format_micros(micros: int | None) -> str | None:
    """最多六位小數，去掉尾端的 0。"""
    if micros is None:
        return None
    sign = "-" if micros < 0 else ""
    whole, frac = divmod(abs(int(micros)), 1_000_000)
    if frac == 0:
        return f"{sign}{whole}"
    text = f"{frac:06d}".rstrip("0")
    return f"{sign}{whole}.{text}"


def money_to_micros(value) -> int | None:
    if value is None or value == "":
        return None
    dec = Decimal(str(value))
    if dec < 0:
        raise ValueError("單價不可為負")
    return int((dec * _MICROS).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def get_billing_currency(db: Session) -> str:
    """每次都查。平台設定不在行程裡快取。"""
    row = (
        db.query(PlatformSetting)
        .filter(PlatformSetting.key == CURRENCY_KEY)
        .first()
    )
    if row is None or not (row.value or "").strip():
        return DEFAULT_CURRENCY
    return row.value.strip().upper()


def billing_currency_locked(db: Session) -> bool:
    """已有任一單價或金額額度之後，平台貨幣不再改寫。"""
    from app.models.usage_quota import UsageQuota

    if db.query(ModelPrice.id).first() is not None:
        return True
    return (
        db.query(UsageQuota.id)
        .filter(UsageQuota.metric == "cost")
        .first()
        is not None
    )


class PriceBook:
    def __init__(self, rows_by_model: dict[int, list[ModelPrice]]):
        self._rows = rows_by_model

    @classmethod
    def load(cls, db: Session) -> "PriceBook":
        rows = (
            db.query(ModelPrice)
            .order_by(ModelPrice.effective_at.desc(), ModelPrice.id.desc())
            .all()
        )
        grouped: dict[int, list[ModelPrice]] = {}
        for row in rows:
            grouped.setdefault(row.model_id, []).append(row)
        return cls(grouped)

    def price_at(self, model_id: int, when: datetime | None) -> ModelPrice | None:
        moment = aware_utc(when)
        for row in self._rows.get(model_id, []):
            if aware_utc(row.effective_at) <= moment:
                return row
        return None


def point_is_priced(point: ModelPrice | None) -> bool:
    """輸入與輸出都有數字才算計價。0 是合法單價，None 是缺價。"""
    if point is None:
        return False
    return point.input_micros is not None and point.output_micros is not None


def _component_micros(point: ModelPrice, kind: str) -> int:
    if kind == "input":
        return int(point.input_micros)
    if kind == "output":
        return int(point.output_micros)
    if point.reasoning_micros is not None:
        return int(point.reasoning_micros)
    return int(point.output_micros)


def _field(row, name, default=None):
    if isinstance(row, dict):
        return row.get(name, default)
    return getattr(row, name, default)


def row_cost_numerator(row, book: PriceBook) -> int | None:
    """token × 單價的分子。未計價回 None，不把缺的欄位當 0。"""
    point = book.price_at(_field(row, "model_id"), _field(row, "request_timestamp"))
    if not point_is_priced(point):
        return None
    prompt = int(_field(row, "prompt_tokens", 0) or 0)
    completion = int(_field(row, "completion_tokens", 0) or 0)
    reasoning = int(_field(row, "reasoning_tokens", 0) or 0)
    output = max(0, completion - reasoning)
    return (
        prompt * _component_micros(point, "input")
        + output * _component_micros(point, "output")
        + reasoning * _component_micros(point, "reasoning")
    )


def row_cost_micros(row, book: PriceBook) -> int | None:
    """一列用量的成本。未計價回 None。思考 token 不跟輸出重複計。"""
    total = row_cost_numerator(row, book)
    if total is None:
        return None
    return total // 1_000_000


def cost_rollup(db: Session, rows, *, book: PriceBook | None = None, currency: str | None = None) -> dict:
    """priced / unpriced / mixed。分子先加總，再換一次 micros。"""
    if book is None:
        book = PriceBook.load(db)
    if currency is None:
        currency = get_billing_currency(db)
    priced = 0
    unpriced = 0
    numerator = 0
    for row in rows:
        cost = row_cost_numerator(row, book)
        if cost is None:
            unpriced += 1
        else:
            priced += 1
            numerator += cost
    micros = numerator // 1_000_000
    if priced == 0:
        state = "unpriced"
        shown = None
    elif unpriced == 0:
        state = "priced"
        shown = format_micros(micros)
    else:
        state = "mixed"
        shown = format_micros(micros)
    return {
        "cost": shown,
        "cost_currency": currency,
        "cost_state": state,
    }


def _billable_models(db: Session) -> list[ModelRegistry]:
    rows = (
        db.query(ModelRegistry)
        .filter(
            ModelRegistry.is_active.is_(True),
            ModelRegistry.name != PLATFORM_ROUTER_NAME,
        )
        .all()
    )
    return [row for row in rows if (row.model_type or "") != "agent"]


def models_subject_can_use(
    db: Session,
    *,
    scope_type: str,
    user_id: int | None = None,
    department_id: int | None = None,
    api_key_id: int | None = None,
) -> list[ModelRegistry]:
    """這個額度對象實際叫得到的計價模型。平台入口與 agent 本體不算。"""
    from app.models.api_key import ApiKey
    from app.models.user import User
    from app.services.api_key_service import check_model_permission
    from app.services.department_tree import get_descendant_ids

    models = _billable_models(db)
    if scope_type == "user":
        user = db.get(User, user_id) if user_id else None
        if user is None:
            return []
        return [
            model for model in models
            if check_model_permission(db, user=user, api_key_id=None, model_id=model.id)
        ]
    if scope_type == "api_key":
        key = db.get(ApiKey, api_key_id) if api_key_id else None
        user = db.get(User, key.user_id) if key is not None else None
        if user is None:
            return []
        return [
            model for model in models
            if check_model_permission(db, user=user, api_key_id=key.id, model_id=model.id)
        ]
    if scope_type == "unit":
        desc = get_descendant_ids(db, department_id, include_self=True) if department_id else []
        if not desc:
            return []
        users = (
            db.query(User)
            .filter(User.department_id.in_(desc), User.is_active.is_(True))
            .all()
        )
        allowed = []
        for model in models:
            if any(
                check_model_permission(db, user=user, api_key_id=None, model_id=model.id)
                for user in users
            ):
                allowed.append(model)
        return allowed
    return []


def unpriced_models_for_subject(
    db: Session,
    *,
    scope_type: str,
    user_id: int | None = None,
    department_id: int | None = None,
    api_key_id: int | None = None,
    now: datetime | None = None,
) -> list[str]:
    """對象可用、但輸入或輸出還沒有現價的模型。"""
    moment = aware_utc(now)
    book = PriceBook.load(db)
    missing = []
    for model in models_subject_can_use(
        db,
        scope_type=scope_type,
        user_id=user_id,
        department_id=department_id,
        api_key_id=api_key_id,
    ):
        if not point_is_priced(book.price_at(model.id, moment)):
            missing.append(model.display_name or model.name)
    return missing


def unpriced_active_models(db: Session, now: datetime | None = None) -> list[str]:
    """使用中、且輸入或輸出缺價的模型（平台對話入口與 agent 本體除外）。"""
    moment = aware_utc(now)
    book = PriceBook.load(db)
    missing = []
    for model in _billable_models(db):
        if not point_is_priced(book.price_at(model.id, moment)):
            missing.append(model.display_name or model.name)
    return missing
