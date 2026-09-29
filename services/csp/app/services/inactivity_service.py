"""超過 N 天沒有成功登入的帳號，由每日排程停用。

資料留著，沿用一般保存規則。擁有者與系統帳號不停用。
尚未核准的帳號不會被自動停用：他們本來就登不進去。
第一次排程只試算，人數給治理頁藍色通知；下一個 UTC 日才停用。
做完的那一輪同一天不重跑。行程中斷則同一天接著做完，人數只算這一輪實際停用的。
試算中或停用中若失敗，約五分鐘後重試同一天，不等到下一個午夜。
一列失敗不會中止整輪。同一個行程裡重疊的那一輪直接回已經存好的人數，
不把 notice_count 寫成 0。
停用用條件式 UPDATE：掃描之後又登入的人不會被關掉。
停用時註明 disabled_reason=inactivity。卡片、允許的帳密、OIDC 都能走回待審。
"""
from __future__ import annotations

import logging
import threading
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import and_, func, or_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.models.account_inactivity import AccountInactivityState
from app.models.audit_log import AuditLog
from app.models.platform_setting import get_setting
from app.models.user import User
from app.services.audit_service import log_audit_event, log_audit_event_or_raise
from app.services.token_revocation import commit_token_revocation

logger = logging.getLogger(__name__)
_sweep_guard = threading.Lock()

INACTIVITY_DAYS_KEY = "auth.inactivity_disable_days"
DISABLED_REASON_INACTIVITY = "inactivity"
RETRY_AFTER_INCOMPLETE_SECONDS = 300
_EXEMPT_ROLES = ("owner", "system")
_STATE_ID = 1


def inactivity_paused_message(days: int) -> str:
    return (
        f"帳號因超過 {int(days)} 天未使用已暫停，已送出重新啟用申請，請等候管理員審核"
    )


def inactivity_notice(db: Session) -> dict | None:
    row = db.query(AccountInactivityState).filter(
        AccountInactivityState.id == _STATE_ID
    ).one_or_none()
    # 試算中、停用中仍顯示上一輪留下的人數，不要讓通知在重跑時消失。
    if row is None or row.phase not in ("preview", "applied", "previewing", "applying"):
        return None
    count = int(row.notice_count or 0)
    if count <= 0:
        return None
    return {
        "phase": row.phase,
        "count": count,
        "days": int(row.notice_days or 0),
    }


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _anchor(user: User) -> datetime | None:
    stamp = user.last_login_at or user.created_at
    if stamp is None:
        return None
    return _aware(stamp)


def _candidates(db: Session, cutoff: datetime) -> list[User]:
    """已核准且仍啟用的人才會被停用。尚未核准的帳號登不進去，不在這份清單。"""
    rows = (
        db.query(User)
        .filter(User.is_active.is_(True), User.is_approved.is_(True))
        .filter(User.role.notin_(_EXEMPT_ROLES))
        .all()
    )
    found: list[User] = []
    for user in rows:
        anchor = _anchor(user)
        if anchor is None:
            continue
        # 恰好滿 N 天（anchor <= cutoff）才算超過未使用。N 天以內的人留下。
        if anchor > cutoff:
            continue
        found.append(user)
    return found


def _ensure_singleton(db: Session) -> None:
    """把 id=1 那一列補上。已存在就不動。兩次第一次排程同時進來也不會撞主鍵。

    postgresql 與 sqlite 用 ON CONFLICT DO NOTHING。
    其他資料庫先 one_or_none 再插入。
    """
    table = AccountInactivityState.__table__
    values = dict(
        id=_STATE_ID,
        preview_completed=False,
        phase="pending",
        notice_count=0,
        notice_days=180,
    )
    dialect = db.get_bind().dialect.name
    if dialect == "postgresql":
        db.execute(
            pg_insert(table).values(**values).on_conflict_do_nothing(index_elements=["id"])
        )
        return
    if dialect == "sqlite":
        db.execute(
            sqlite_insert(table).values(**values).on_conflict_do_nothing(index_elements=["id"])
        )
        return
    existing = (
        db.query(AccountInactivityState)
        .filter(AccountInactivityState.id == _STATE_ID)
        .one_or_none()
    )
    if existing is None:
        db.add(AccountInactivityState(**values))


def _state(db: Session) -> AccountInactivityState:
    row = (
        db.query(AccountInactivityState)
        .filter(AccountInactivityState.id == _STATE_ID)
        .one_or_none()
    )
    if row is not None:
        return row
    # 還沒有那一列才寫。ON CONFLICT 讓兩次第一次排程同時進來也不會撞主鍵。
    # 列已經在時不要再 INSERT，否則會跟進行中的那一輪搶寫入鎖。
    _ensure_singleton(db)
    db.flush()
    return (
        db.query(AccountInactivityState)
        .filter(AccountInactivityState.id == _STATE_ID)
        .one()
    )


def _stored_result(state: AccountInactivityState, days: int) -> dict:
    return {
        "phase": state.phase,
        "count": int(state.notice_count or 0),
        "days": int(state.notice_days or days),
    }


def _disable_if_still_idle(db: Session, user: User, cutoff: datetime) -> bool:
    """只在登入時間仍舊早於門檻時停用。掃描途中登入的人這次留下。"""
    stale_login = and_(
        User.last_login_at.is_not(None),
        User.last_login_at <= cutoff,
    )
    never_logged = and_(
        User.last_login_at.is_(None),
        User.created_at.is_not(None),
        User.created_at <= cutoff,
    )
    stmt = (
        update(User)
        .where(User.id == user.id)
        .where(User.is_active.is_(True))
        .where(User.is_approved.is_(True))
        .where(User.role.notin_(_EXEMPT_ROLES))
        .where(or_(stale_login, never_logged))
        .values(
            is_active=False,
            disabled_reason=DISABLED_REASON_INACTIVITY,
            token_version=func.coalesce(User.token_version, 0) + 1,
        )
        .execution_options(synchronize_session=False)
    )
    result = db.execute(stmt)
    if result.rowcount != 1:
        db.expire(user)
        return False
    db.expire(user)
    db.refresh(user)
    return True


def resume_after_inactivity(
    db: Session,
    user: User,
    *,
    ip_address: str | None = None,
) -> str | None:
    """身分已核對、且是閒置停用時，改回待審並把錨點設成現在。不發權杖。

    用條件式 UPDATE 讀資料庫現況：必須仍是停用且 disabled_reason 為閒置。
    管理員若已同時重新啟用，這次更新對不到列，不會把核准蓋掉。
    不是閒置停用就什麼都不改，回 None。卡片、允許的帳密、OIDC 都走這裡。
    """
    now = datetime.now(timezone.utc)
    days = int(get_setting(db, INACTIVITY_DAYS_KEY))
    result = db.execute(
        update(User)
        .where(User.id == user.id)
        .where(User.is_active.is_(False))
        .where(User.disabled_reason == DISABLED_REASON_INACTIVITY)
        .values(
            is_active=True,
            is_approved=False,
            disabled_reason=None,
            last_login_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        db.rollback()
        return None
    stored = db.get(User, user.id, populate_existing=True)
    log_audit_event(
        db,
        actor=stored,
        action="inactivity_reapproval",
        resource_type="user",
        resource_id=stored.id,
        detail="閒置停用的帳號重新登入，改回待審",
        ip_address=ip_address,
        commit=True,
    )
    return inactivity_paused_message(days)


def _claim_run_day(
    db: Session,
    today: date,
    moment: datetime,
    *,
    phase: str,
    days: int,
) -> bool:
    """搶下今天，並同時寫上這一段的 phase 與 notice_days。

    條件式 UPDATE 先提交，重疊的另一輪看到 0 列就停，回已經存好的人數。
    搶到今天之後如果行程中斷，同一天再進來會把剩下的試算或停用做完。
    做完的那一輪要等到下一個 UTC 日才再跑。
    """
    _state(db)
    db.flush()
    result = db.execute(
        update(AccountInactivityState)
        .where(AccountInactivityState.id == _STATE_ID)
        .where(
            or_(
                AccountInactivityState.last_run_on.is_(None),
                AccountInactivityState.last_run_on != today,
            )
        )
        .values(
            last_run_on=today,
            updated_at=moment,
            phase=phase,
            notice_days=days,
        )
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        db.rollback()
        return False
    if phase == "applying":
        # 這一天的起點。人數只數這個標記之後的停用，不把前幾天加進去。
        log_audit_event(
            db,
            action="inactivity_sweep_apply_started",
            resource_type="user",
            detail=f"開始停用超過 {days} 天未登入的帳號",
            commit=False,
        )
    db.commit()
    return True


def _applied_disable_count(db: Session) -> int:
    """這一輪實際停用的人數。中斷後續跑把已經提交的也算進去，前幾天的不算。"""
    marker = (
        db.query(AuditLog)
        .filter(AuditLog.action == "inactivity_sweep_apply_started")
        .order_by(AuditLog.id.desc())
        .first()
    )
    query = db.query(AuditLog).filter(AuditLog.action == "inactivity_disable")
    if marker is not None:
        query = query.filter(AuditLog.id > marker.id)
    return query.count()


def run_inactivity_sweep(db: Session, *, now: datetime | None = None) -> dict:
    """跑一輪。回傳 phase / count / days。做完的那一天不重跑，第一次只試算。"""
    moment = _aware(now or datetime.now(timezone.utc))
    today = moment.date()
    days = int(get_setting(db, INACTIVITY_DAYS_KEY))
    if not _sweep_guard.acquire(blocking=False):
        stored = _stored_result(_state(db), days)
        db.rollback()
        return stored
    try:
        return _run_inactivity_sweep(db, moment=moment, today=today, days=days)
    finally:
        _sweep_guard.release()


def _run_inactivity_sweep(
    db: Session,
    *,
    moment: datetime,
    today: date,
    days: int,
) -> dict:
    cutoff = moment - timedelta(days=days)
    state = _state(db)
    if state.last_run_on == today and state.phase in ("preview", "applied"):
        stored = _stored_result(state, days)
        db.rollback()
        return stored
    if state.phase not in ("previewing", "applying"):
        next_phase = "previewing" if not state.preview_completed else "applying"
        if not _claim_run_day(db, today, moment, phase=next_phase, days=days):
            stored = _stored_result(_state(db), days)
            db.rollback()
            return stored
        state = _state(db)
    if not state.preview_completed or state.phase == "previewing":
        return _finish_preview(db, moment=moment, today=today, days=days, cutoff=cutoff)
    return _finish_apply(db, moment=moment, today=today, days=days, cutoff=cutoff)


def _finish_preview(
    db: Session,
    *,
    moment: datetime,
    today: date,
    days: int,
    cutoff: datetime,
) -> dict:
    candidates = _candidates(db, cutoff)
    # 讀完就結束這次交易，避免讀鎖擋到重疊的那一輪。
    db.commit()
    state = _state(db)
    state.preview_completed = True
    state.phase = "preview"
    state.notice_count = len(candidates)
    state.notice_days = days
    state.last_run_on = today
    state.updated_at = moment
    log_audit_event(
        db,
        action="inactivity_sweep_preview",
        resource_type="user",
        detail=f"試算：{len(candidates)} 個帳號超過 {days} 天未登入，這次不停用",
        commit=False,
    )
    db.commit()
    return {"phase": "preview", "count": len(candidates), "days": days}


def _finish_apply(
    db: Session,
    *,
    moment: datetime,
    today: date,
    days: int,
    cutoff: datetime,
) -> dict:
    candidates = _candidates(db, cutoff)
    db.commit()
    failed = False
    for user in list(candidates):
        try:
            if not _disable_if_still_idle(db, user, cutoff):
                continue
            log_audit_event_or_raise(
                db,
                action="inactivity_disable",
                resource_type="user",
                resource_id=user.id,
                detail=f"超過 {days} 天未登入，停用「{user.username}」",
                commit=False,
            )
            # 停用、稽核、權杖作廢同一筆提交。失敗的那一列留到下一輪。
            commit_token_revocation(db, user)
        except Exception:
            logger.exception(
                "inactivity sweep skipped user_id=%s", getattr(user, "id", None)
            )
            db.rollback()
            failed = True
    if failed:
        # 留下 applying，排程幾分鐘後重試同一天，不要把這一天標成做完。
        return {
            "phase": "applying",
            "count": _applied_disable_count(db),
            "days": days,
        }
    disabled = _applied_disable_count(db)
    state = _state(db)
    state.phase = "applied"
    state.notice_count = disabled
    state.notice_days = days
    state.last_run_on = today
    state.updated_at = moment
    log_audit_event(
        db,
        action="inactivity_sweep_applied",
        resource_type="user",
        detail=f"停用 {disabled} 個超過 {days} 天未登入的帳號",
        commit=False,
    )
    db.commit()
    logger.info("inactivity sweep disabled count=%s days=%s", disabled, days)
    return {"phase": "applied", "count": disabled, "days": days}


def delay_after_sweep(
    now: datetime, *, phase: str | None, failed: bool
) -> float:
    """做完就等到下一個 UTC 午夜。試算中、停用中，或這輪丟出例外，五分鐘後重試。"""
    if failed or phase in ("previewing", "applying"):
        return float(RETRY_AFTER_INCOMPLETE_SECONDS)
    return _seconds_until_next_utc_midnight(now)


def _seconds_until_next_utc_midnight(now: datetime) -> float:
    aware = _aware(now)
    nxt = (aware + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0,
    )
    return max(1.0, (nxt - aware).total_seconds())


def _sweep_once() -> dict:
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        return run_inactivity_sweep(db)
    finally:
        db.close()


def start_inactivity_sweep():
    """啟動先跑一輪。做完就等到下一個 UTC 午夜；做到一半就五分鐘後重試同一天。

    background leader 掉租約再當選會重新呼叫這裡。是否真的停用由
    ``last_run_on`` 與 phase 決定，不靠這次行程還記不記得。
    """
    import asyncio

    async def _loop() -> None:
        await asyncio.sleep(0)
        while True:
            phase = None
            failed = False
            try:
                result = await asyncio.to_thread(_sweep_once)
                if isinstance(result, dict):
                    phase = result.get("phase")
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("帳號閒置停用排程失敗")
                failed = True
            try:
                delay = delay_after_sweep(
                    datetime.now(timezone.utc), phase=phase, failed=failed,
                )
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                raise

    logger.info("帳號閒置停用排程已啟動")
    return asyncio.create_task(_loop())
