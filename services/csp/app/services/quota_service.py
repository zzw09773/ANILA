"""額度檢查。只在上游呼叫之前跑，而且用呼叫端交進來的 session。

Redis 沒有這期的鍵，或鍵已標成失效，就從資料庫重算。重建與累加在同一支
script 裡做完，不拿較舊的絕對值覆蓋別人剛加上的數字。Redis 不在就放行並記警告。
多條額度同時套用時，任何一條設成擋下而且已經到上限，就擋下。

金額計數存的是未捨去的分子（token × 單價），跟上限比較時才換成 micros。
沒有任何有上限的額度時，行程內快取空集合，呼叫路徑不再讀額度表。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from sqlalchemy import event, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.alert import Alert
from app.models.api_key import ApiKey
from app.models.department import Department
from app.models.handoff import Notification
from app.models.token_usage import TokenUsage
from app.models.unit_admin_assignment import UnitAdminAssignment
from app.models.usage_quota import QuotaEvent, UsageQuota
from app.models.user import User
from app.services.alert_notifier import notify_alert_opened
from app.services.alert_service import upsert_alert
from app.services.department_tree import get_ancestor_ids, get_descendant_ids
from app.services.pricing import (
    PriceBook,
    format_micros,
    get_billing_currency,
    row_cost_numerator,
)
from app.services.quota_redis import (
    APPLY_MISSING,
    INCR_IF_PRESENT,
    INIT_IF_ABSENT,
    QuotaRedisDown,
    get_quota_redis,
)

logger = logging.getLogger(__name__)

_TPE = timezone(timedelta(hours=8))
_UNBILLED = ("router_transport", "platform")
_CACHE_TTL_SECONDS = 5.0
_cache_rows: tuple["QuotaSnap", ...] | None = None
_cache_at = 0.0
_invalid_keys: set[str] = set()


@dataclass(frozen=True)
class QuotaSnap:
    id: int
    scope_type: str
    user_id: int | None
    department_id: int | None
    api_key_id: int | None
    period: str
    metric: str
    limit_value: int | None
    warn_percent: int
    on_limit: str


def clear_active_quota_cache() -> None:
    global _cache_rows, _cache_at
    _cache_rows = None
    _cache_at = 0.0
    _invalid_keys.clear()


def _snap(row: UsageQuota) -> QuotaSnap:
    return QuotaSnap(
        id=row.id,
        scope_type=row.scope_type,
        user_id=row.user_id,
        department_id=row.department_id,
        api_key_id=row.api_key_id,
        period=row.period,
        metric=row.metric,
        limit_value=None if row.limit_value is None else int(row.limit_value),
        warn_percent=int(row.warn_percent or 80),
        on_limit=row.on_limit or "warn",
    )


def _active_quotas(db: Session) -> tuple[QuotaSnap, ...]:
    global _cache_rows, _cache_at
    now = time.monotonic()
    if _cache_rows is not None and now - _cache_at < _CACHE_TTL_SECONDS:
        return _cache_rows
    rows = (
        db.query(UsageQuota)
        .filter(UsageQuota.limit_value.isnot(None))
        .order_by(UsageQuota.id)
        .all()
    )
    _cache_rows = tuple(_snap(row) for row in rows)
    _cache_at = now
    return _cache_rows


@event.listens_for(UsageQuota, "after_insert")
@event.listens_for(UsageQuota, "after_update")
@event.listens_for(UsageQuota, "after_delete")
def _quota_changed(_mapper, _connection, _target) -> None:
    clear_active_quota_cache()


def _aware(dt: datetime | None) -> datetime:
    if dt is None:
        return datetime.now(timezone.utc)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def period_window(period: str, moment: datetime | None) -> tuple[datetime, datetime, str]:
    """回傳 [start, end) 的 UTC 邊界，以及 Redis 用的期間鍵。重置時刻是 end。"""
    local = _aware(moment).astimezone(_TPE)
    if period == "daily":
        start_local = local.replace(hour=0, minute=0, second=0, microsecond=0)
        end_local = start_local + timedelta(days=1)
        key = start_local.strftime("%Y-%m-%d")
    else:
        start_local = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        if start_local.month == 12:
            end_local = start_local.replace(year=start_local.year + 1, month=1)
        else:
            end_local = start_local.replace(month=start_local.month + 1)
        key = start_local.strftime("%Y-%m")
    return (
        start_local.astimezone(timezone.utc),
        end_local.astimezone(timezone.utc),
        key,
    )


def _metric_label(metric: str) -> str:
    # 金額鍵存分子，不能跟以前以 micros 解讀的鍵混用。
    return "costnum" if metric == "cost" else metric


def counter_key(quota, period_key: str) -> str:
    scope_id = quota.user_id or quota.department_id or quota.api_key_id
    return (
        f"anila:quota:v1:{quota.period}:{period_key}:"
        f"{quota.scope_type}:{scope_id}:{_metric_label(quota.metric)}"
    )


def measured_units(metric: str, raw: int) -> int:
    if metric == "cost":
        return int(raw) // 1_000_000
    return int(raw)


def _exclude_unbilled(query):
    return query.filter(
        (TokenUsage.usage_kind.is_(None))
        | (TokenUsage.usage_kind.notin_(_UNBILLED))
    )


def _scoped(query, db: Session, quota):
    if quota.scope_type == "user":
        return query.filter(TokenUsage.user_id == quota.user_id)
    if quota.scope_type == "api_key":
        return query.filter(TokenUsage.api_key_id == quota.api_key_id)
    desc = get_descendant_ids(db, quota.department_id, include_self=True)
    if not desc:
        return query.filter(TokenUsage.id < 0)
    return query.filter(TokenUsage.department_id.in_(desc))


def rebuild_counter(db: Session, quota, moment: datetime) -> int:
    start, end, _period_key = period_window(quota.period, moment)
    query = db.query(TokenUsage).filter(
        TokenUsage.request_timestamp >= start,
        TokenUsage.request_timestamp < end,
    )
    query = _exclude_unbilled(_scoped(query, db, quota))
    if quota.metric == "tokens":
        total = query.with_entities(
            func.coalesce(func.sum(TokenUsage.total_tokens), 0)
        ).scalar()
        return int(total or 0)
    book = PriceBook.load(db)
    numerator = 0
    for row in query.all():
        amount = row_cost_numerator(row, book)
        if amount is None:
            continue
        numerator += amount
    return numerator


def _ttl_seconds(end: datetime, now: datetime) -> int:
    remaining = int((end - _aware(now)).total_seconds())
    return max(60, remaining + 2 * 86400)


def _mark_invalid(client, key: str) -> None:
    _invalid_keys.add(key)
    try:
        client.set(key, "invalid")
    except Exception:
        logger.warning("額度計數已標為失效，待下次讀取對帳", exc_info=True)
        return
    logger.warning("額度計數已標為失效，待下次讀取對帳")


def _init_absolute(client, key: str, total: int, ttl: int) -> int | None:
    current = client.eval(INIT_IF_ABSENT, 1, key, int(total), int(ttl))
    if current is None:
        return None
    _invalid_keys.discard(key)
    return int(current)


def read_counter(db: Session, client, quota, now: datetime) -> int:
    if client is None:
        raise QuotaRedisDown()
    _start, end, period_key = period_window(quota.period, now)
    key = counter_key(quota, period_key)
    try:
        raw = client.get(key)
    except Exception as exc:
        raise QuotaRedisDown() from exc
    if raw is None or raw == "invalid" or key in _invalid_keys:
        total = rebuild_counter(db, quota, now)
        try:
            current = _init_absolute(client, key, int(total), _ttl_seconds(end, now))
        except Exception:
            logger.warning("額度計數已標為失效，待下次讀取對帳", exc_info=True)
            return int(total)
        if current is None:
            return int(total)
        return current
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        raise QuotaRedisDown() from exc


def _scope_subject(db: Session, quota) -> str:
    if quota.scope_type == "unit":
        dept = db.get(Department, quota.department_id) if quota.department_id else None
        name = dept.name if dept is not None else "單位"
        return f"單位「{name}」"
    if quota.scope_type == "api_key":
        key = db.get(ApiKey, quota.api_key_id) if quota.api_key_id else None
        name = key.name if key is not None else "API 金鑰"
        return f"API 金鑰「{name}」"
    return "使用者"


def quota_block_message(db: Session, quota, reset_at: datetime) -> str:
    subject = _scope_subject(db, quota)
    period = "每日" if quota.period == "daily" else "每月"
    if quota.metric == "tokens":
        metric = "token"
        limit_text = f"{int(quota.limit_value):,}"
    else:
        metric = "金額"
        currency = get_billing_currency(db)
        limit_text = f"{format_micros(int(quota.limit_value))} {currency}"
    reset = reset_at.astimezone(_TPE).strftime("%Y-%m-%d %H:%M")
    return (
        f"已達{subject}{period} {metric} 上限（{limit_text}），"
        f"將於 {reset}（台北時間）重置。"
    )


def _warn_message(db: Session, quota) -> str:
    subject = _scope_subject(db, quota)
    period = "每日" if quota.period == "daily" else "每月"
    metric = "token" if quota.metric == "tokens" else "金額"
    return f"{subject}{period} {metric} 已達提醒門檻（{int(quota.warn_percent)}%）。"


def _recipients(db: Session, quota, trigger_user_id: int | None) -> set[int]:
    ids: set[int] = set()
    if quota.scope_type == "user" and quota.user_id:
        ids.add(quota.user_id)
    elif quota.scope_type == "api_key" and quota.api_key_id:
        key = db.get(ApiKey, quota.api_key_id)
        if key is not None:
            ids.add(key.user_id)
    elif quota.scope_type == "unit":
        if trigger_user_id:
            ids.add(trigger_user_id)
        if quota.department_id:
            rows = (
                db.query(UnitAdminAssignment.user_id)
                .filter(
                    UnitAdminAssignment.department_id == quota.department_id,
                    UnitAdminAssignment.revoked_at.is_(None),
                )
                .all()
            )
            ids.update(row[0] for row in rows)
    return {uid for uid in ids if uid}


def _notify(
    db: Session,
    quota,
    *,
    kind: str,
    title: str,
    body: str,
    trigger_user_id: int | None,
    period_key: str,
    commit: bool = False,
) -> None:
    fingerprint = f"quota:{quota.id}:{period_key}:{kind}"[:200]
    severity = "medium" if kind == "warn" else "high"
    inserted = False
    try:
        with db.begin_nested():
            existing = db.query(Alert.id).filter(Alert.fingerprint == fingerprint).first()
            if existing is None:
                upsert_alert(
                    db,
                    fingerprint=fingerprint,
                    category="quota",
                    severity=severity,
                    title=title,
                    message=body,
                    source_type="usage_quota",
                    source_id=quota.id,
                )
                db.flush()
                inserted = True
    except IntegrityError:
        return
    if not inserted:
        return
    notify_alert_opened(
        fingerprint=fingerprint,
        category="quota",
        severity=severity,
        title=title,
        message=body,
        source_type="usage_quota",
        source_id=quota.id,
    )
    for uid in _recipients(db, quota, trigger_user_id):
        db.add(
            Notification(
                user_id=uid,
                type=f"quota_{kind}",
                title=title,
                body=body,
                payload={"fingerprint": fingerprint, "quota_id": quota.id},
            )
        )
    if not commit:
        return
    try:
        db.commit()
    except Exception:
        logger.warning("額度通知寫入失敗", exc_info=True)
        try:
            db.rollback()
        except Exception:
            logger.warning("額度通知回復失敗", exc_info=True)


def applicable_quotas(
    db: Session,
    *,
    user_id: int | None,
    department_id: int | None,
    api_key_id: int | None,
) -> list[QuotaSnap]:
    quotas = _active_quotas(db)
    if not quotas:
        return []
    ancestors: set[int] = set()
    if department_id is not None:
        ancestors = get_ancestor_ids(db, department_id, include_self=True)
    out = []
    for quota in quotas:
        if quota.limit_value is None:
            continue
        if quota.scope_type == "user" and quota.user_id == user_id:
            out.append(quota)
        elif (
            quota.scope_type == "api_key"
            and api_key_id is not None
            and quota.api_key_id == api_key_id
        ):
            out.append(quota)
        elif quota.scope_type == "unit" and quota.department_id in ancestors:
            out.append(quota)
    return out


def enforce_call_quota(
    db: Session,
    *,
    user_id: int | None,
    department_id: int | None,
    api_key_id: int | None,
    usage_kind: str | None = "inference",
    now: datetime | None = None,
) -> None:
    """到上限且設成擋下時丟 429。通知與擋下紀錄留在同一個 session，由呼叫端 commit。"""
    if not user_id or usage_kind in _UNBILLED:
        return
    quotas = applicable_quotas(
        db,
        user_id=user_id,
        department_id=department_id,
        api_key_id=api_key_id,
    )
    if not quotas:
        return
    moment = _aware(now)
    client = get_quota_redis()
    blockers: list[QuotaSnap] = []
    try:
        for quota in quotas:
            usage = measured_units(quota.metric, read_counter(db, client, quota, moment))
            limit = int(quota.limit_value)
            _start, end, period_key = period_window(quota.period, moment)
            at_limit = usage >= limit
            at_warn = usage * 100 >= limit * int(quota.warn_percent or 80)
            if at_limit and quota.on_limit == "block":
                blockers.append(quota)
                continue
            if at_limit and quota.on_limit == "warn":
                _notify(
                    db,
                    quota,
                    kind="limit",
                    title="用量已達上限",
                    body=quota_block_message(db, quota, end),
                    trigger_user_id=user_id,
                    period_key=period_key,
                )
            elif at_warn:
                _notify(
                    db,
                    quota,
                    kind="warn",
                    title="用量接近上限",
                    body=_warn_message(db, quota),
                    trigger_user_id=user_id,
                    period_key=period_key,
                )
    except QuotaRedisDown:
        logger.warning("額度計數暫不可用，本次放行")
        return
    if not blockers:
        return
    first_message = None
    for quota in blockers:
        _start, end, period_key = period_window(quota.period, moment)
        message = quota_block_message(db, quota, end)
        if first_message is None:
            first_message = message
        _notify(
            db,
            quota,
            kind="block",
            title="用量已達上限，呼叫已被擋下",
            body=message,
            trigger_user_id=user_id,
            period_key=period_key,
        )
        db.add(
            QuotaEvent(
                quota_id=quota.id,
                user_id=user_id,
                api_key_id=api_key_id,
                department_id=department_id,
                action="block",
                message=message,
                occurred_at=moment,
            )
        )
    raise HTTPException(
        status_code=429,
        detail={"code": "quota_exceeded", "message": first_message},
    )


def _row_get(row, key, default=None):
    if isinstance(row, dict):
        return row.get(key, default)
    return getattr(row, key, default)


def _row_in_quota(db: Session, quota, row, ancestors_of: dict) -> bool:
    if quota.scope_type == "user":
        return _row_get(row, "user_id") == quota.user_id
    if quota.scope_type == "api_key":
        return _row_get(row, "api_key_id") == quota.api_key_id
    dept_id = _row_get(row, "department_id")
    if dept_id is None or quota.department_id is None:
        return False
    if dept_id not in ancestors_of:
        ancestors_of[dept_id] = get_ancestor_ids(db, dept_id, include_self=True)
    return quota.department_id in ancestors_of[dept_id]


def _accumulate_deltas(db: Session, quotas, rows: list, book) -> dict[str, dict]:
    """同一條 Redis 鍵只加一次。重複的額度列不會把同一筆用量加兩次。"""
    ancestors_of: dict[int, set[int]] = {}
    merged: dict[str, dict] = {}
    for row in rows:
        kind = _row_get(row, "usage_kind") or "inference"
        if kind in _UNBILLED:
            continue
        moment = _aware(_row_get(row, "request_timestamp"))
        seen_keys: set[str] = set()
        for quota in quotas:
            if not _row_in_quota(db, quota, row, ancestors_of):
                continue
            _start, _end, period_key = period_window(quota.period, moment)
            key = counter_key(quota, period_key)
            if key in seen_keys:
                continue
            if quota.metric == "tokens":
                amount = int(_row_get(row, "total_tokens") or 0)
            else:
                if book is None:
                    continue
                numer = row_cost_numerator(row, book)
                if numer is None:
                    continue
                amount = int(numer)
            if not amount:
                continue
            seen_keys.add(key)
            slot = merged.get(key)
            if slot is None:
                merged[key] = {
                    "amount": amount,
                    "moment": moment,
                    "quota": quota,
                    "period_key": period_key,
                }
            else:
                slot["amount"] += amount
    return merged


def _apply_delta(db: Session, client, quota, key: str, amount: int, moment: datetime):
    _start, end, _period_key = period_window(quota.period, moment)
    ttl = _ttl_seconds(end, moment)
    try:
        written = client.eval(INCR_IF_PRESENT, 1, key, int(amount), ttl)
    except Exception:
        _mark_invalid(client, key)
        try:
            total = rebuild_counter(db, quota, moment)
            _init_absolute(client, key, int(total), ttl)
        except Exception:
            logger.warning("額度計數已標為失效，待下次讀取對帳", exc_info=True)
        return None
    if written is not None:
        return int(written), int(amount)
    absolute = rebuild_counter(db, quota, moment)
    try:
        result = client.eval(APPLY_MISSING, 1, key, int(absolute), int(amount), ttl)
    except Exception:
        _mark_invalid(client, key)
        try:
            _init_absolute(client, key, int(absolute), ttl)
        except Exception:
            logger.warning("額度計數已標為失效，待下次讀取對帳", exc_info=True)
        return None
    if isinstance(result, (list, tuple)) and len(result) >= 2:
        _invalid_keys.discard(key)
        return int(result[0]), int(result[1])
    if result is None:
        return None
    _invalid_keys.discard(key)
    return int(result), int(amount)


def _notify_crossing(db: Session, quota, *, before_raw: int, after_raw: int, applied: int, moment: datetime, user_id: int | None) -> None:
    if applied <= 0:
        return
    before = measured_units(quota.metric, before_raw)
    after = measured_units(quota.metric, after_raw)
    limit = int(quota.limit_value)
    warn_line = limit * int(quota.warn_percent or 80)
    crossed_limit = before < limit <= after
    crossed_warn = before * 100 < warn_line <= after * 100
    if not crossed_limit and not crossed_warn:
        return
    _start, end, period_key = period_window(quota.period, moment)
    if crossed_limit:
        _notify(
            db,
            quota,
            kind="limit",
            title="用量已達上限",
            body=quota_block_message(db, quota, end),
            trigger_user_id=user_id,
            period_key=period_key,
            commit=True,
        )
        return
    _notify(
        db,
        quota,
        kind="warn",
        title="用量接近上限",
        body=_warn_message(db, quota),
        trigger_user_id=user_id,
        period_key=period_key,
        commit=True,
    )


def bump_committed_usage(db: Session, rows: list) -> None:
    """用量列 commit 成功之後才加計數。Redis 不在就直接返回，不碰 session。"""
    if not rows:
        return
    client = get_quota_redis()
    if client is None:
        return
    try:
        _bump(db, client, rows)
    except Exception:
        logger.warning("用量計數累加失敗", exc_info=True)


def _bump(db: Session, client, rows: list) -> None:
    quotas = _active_quotas(db)
    if not quotas:
        return
    book = PriceBook.load(db) if any(quota.metric == "cost" for quota in quotas) else None
    deltas = _accumulate_deltas(db, quotas, rows, book)
    for key, slot in deltas.items():
        quota = slot["quota"]
        amount = int(slot["amount"])
        moment = slot["moment"]
        applied_pair = _apply_delta(db, client, quota, key, amount, moment)
        if applied_pair is None:
            continue
        after, applied = applied_pair
        _notify_crossing(
            db,
            quota,
            before_raw=after - applied,
            after_raw=after,
            applied=applied,
            moment=moment,
            user_id=_row_get(rows[0], "user_id") if rows else None,
        )


def unpriced_usage_count(db: Session, quota, now: datetime | None = None) -> int:
    if quota.metric != "cost":
        return 0
    moment = _aware(now)
    start, end, _period_key = period_window(quota.period, moment)
    query = db.query(TokenUsage).filter(
        TokenUsage.request_timestamp >= start,
        TokenUsage.request_timestamp < end,
    )
    query = _exclude_unbilled(_scoped(query, db, quota))
    book = PriceBook.load(db)
    count = 0
    for row in query.all():
        if row_cost_numerator(row, book) is None:
            count += 1
    return count


def list_quota_blocks(
    db: Session,
    *,
    start_time: datetime,
    end_time: datetime | None = None,
    user_id: int | None = None,
    department_id: int | None = None,
    scope_ids: list[int] | None = None,
) -> list[dict]:
    query = db.query(QuotaEvent, User.username).outerjoin(
        User, QuotaEvent.user_id == User.id
    )
    query = query.filter(QuotaEvent.occurred_at >= start_time)
    if end_time is not None:
        query = query.filter(QuotaEvent.occurred_at < end_time)
    if user_id is not None:
        query = query.filter(QuotaEvent.user_id == user_id)
    else:
        scope: list[int] | None
        if department_id is not None:
            selected = set(get_descendant_ids(db, department_id, include_self=True))
            if scope_ids is not None:
                selected &= set(scope_ids)
            scope = sorted(selected)
        elif scope_ids is not None:
            scope = list(scope_ids)
        else:
            scope = None
        if scope is not None:
            if not scope:
                return []
            query = query.filter(QuotaEvent.department_id.in_(scope))
    rows = query.order_by(QuotaEvent.occurred_at.desc()).limit(200).all()
    out = []
    for event, username in rows:
        out.append(
            {
                "id": event.id,
                "user_id": event.user_id,
                "username": username or "",
                "message": event.message,
                "action": event.action,
                "occurred_at": _aware(event.occurred_at).isoformat(),
                "quota_id": event.quota_id,
                "department_id": event.department_id,
            }
        )
    return out
