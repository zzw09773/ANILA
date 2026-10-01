from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from app.database import SessionLocal, get_db
from app.models.user import User
from app.schemas.token_usage import (
    UsageSummary,
    ChartDataResponse,
    TopModelUsage,
    TopUserUsage,
    TopDepartmentUsage,
)
from app.services.auth_service import get_current_user, is_admin_tier, is_deputy, require_admin
from app.services.unit_admin_service import (
    get_unit_admin_scope_ids,
    is_unit_admin,
)
from app.services.quota_service import list_quota_blocks
from app.services.usage_service import (
    ExportRangeError,
    get_agent_usage,
    get_caller_usage,
    get_chart_data,
    get_top_agents,
    get_top_departments,
    get_top_models,
    get_top_users,
    get_usage_by_api_key,
    get_usage_by_base_model,
    get_usage_by_client,
    get_usage_by_unit,
    get_usage_summary,
    iter_usage_csv,
    resolve_export_window,
)

# 測試會替換這個名字。預設走逐批產生器；若被換成回傳字串的替身，路由仍可送出。
def export_usage_csv(*args, **kwargs):
    return iter_usage_csv(*args, **kwargs)

router = APIRouter(prefix="/api/usage", tags=["用量統計"])


def _resolve_usage_caller_filters(
    db: Session,
    current_user: User,
    department_id: int | None,
) -> tuple[int | None, int | None, list[int] | None]:
    """Three-tier usage gate → (user_id, department_id, scope_ids).

    - admin: unchanged (optional department_id expanded in service).
    - unit admin: unit-wide (user_id=None); validate/default scope.
    - plain user: forced self; department_id cleared.
    """
    if is_admin_tier(current_user):
        return None, department_id, None

    scope = get_unit_admin_scope_ids(db, current_user)
    if scope is not None:
        if department_id is None:
            return None, None, sorted(scope)
        if department_id not in scope:
            raise HTTPException(
                status_code=403,
                detail="僅能查詢自己管理單位的資料",
            )
        return None, department_id, None

    return current_user.id, None, None


_DEPUTY_AGGREGATE_ONLY = "代理管理員只能查看彙總用量"


def _deputy_platform_aggregate(
    current_user: User,
    *,
    user_id: int | None = None,
    group_by: str | None = None,
) -> bool:
    """代理管理員只看全平台彙總。單位管理員身份不能把這條路改成單位明細。

    回傳 True 表示呼叫端應把 user_id、department、scope 都清空。
    指定某人、或 group_by=user，直接 403。
    """
    if not (is_deputy(current_user) and not is_admin_tier(current_user)):
        return False
    if user_id is not None or group_by == "user":
        raise HTTPException(status_code=403, detail=_DEPUTY_AGGREGATE_ONLY)
    return True


def _require_admin_or_unit_admin(
    db: Session, current_user: User
) -> list[int] | None:
    """Admin → None scope (no force). Unit admin → full union scope.
    Plain user → 403 with the same message as ``require_admin``.
    """
    if is_admin_tier(current_user):
        return None
    scope = get_unit_admin_scope_ids(db, current_user)
    if scope is not None:
        return sorted(scope)
    raise HTTPException(status_code=403, detail="需要管理員權限")


@router.get("/me")
def usage_me(
    range: str = Query("24h", pattern="^(24h|7d|30d)$"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Caller's own usage. Cookie or Bearer; GET so CSRF does not apply."""
    return get_caller_usage(db, user_id=current_user.id, range_key=range)


@router.get("/summary", response_model=UsageSummary)
def usage_summary(
    range: str = Query("24h", regex="^(4h|12h|24h|7d|30d)$"),
    model_id: int | None = None,
    model_type: str | None = Query(None, description="篩選模型類型: llm/embedding/agent"),
    department_id: int | None = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    # admin / owner 看到整 tenant aggregate;單位管理員看管理單位;
    # 一般 user 只看自己。代理管理員看全平台彙總，不打開個人明細。
    if _deputy_platform_aggregate(current_user):
        user_id, department_id, scope_ids = None, None, None
    else:
        user_id, department_id, scope_ids = _resolve_usage_caller_filters(
            db, current_user, department_id
        )
    return get_usage_summary(
        db,
        range_key=range,
        model_id=model_id,
        user_id=user_id,
        model_type=model_type,
        department_id=department_id,
        scope_ids=scope_ids,
    )


@router.get("/chart", response_model=ChartDataResponse)
def usage_chart(
    range: str = Query("24h", regex="^(4h|12h|24h|7d|30d)$"),
    model_id: int | None = None,
    user_id: int | None = None,
    department_id: int | None = None,
    model_type: str | None = Query(None, description="篩選模型類型: llm/embedding/agent"),
    group_by: str = Query("total", regex="^(department|model|user|total)$"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    # 一般 user 只看得到自己的資料,group_by 也回退成 total。
    # admin / owner 都享 tenant-wide 視野。單位管理員可 group_by
    # department/model/user。代理管理員只看全平台彙總。
    if _deputy_platform_aggregate(current_user, user_id=user_id, group_by=group_by):
        user_id = None
        department_id = None
    elif is_admin_tier(current_user):
        pass
    elif is_unit_admin(db, current_user):
        user_id, department_id, scope_ids = _resolve_usage_caller_filters(
            db, current_user, department_id
        )
        return get_chart_data(
            db,
            range,
            model_id=model_id,
            user_id=user_id,
            model_type=model_type,
            department_id=department_id,
            scope_ids=scope_ids,
            group_by=group_by,
        )
    else:
        user_id = current_user.id
        department_id = None
        if group_by in {"department", "user"}:
            group_by = "total"

    return get_chart_data(
        db,
        range,
        model_id=model_id,
        user_id=user_id,
        model_type=model_type,
        department_id=department_id,
        group_by=group_by,
    )


@router.get("/top-models", response_model=list[TopModelUsage])
def top_models(
    limit: int = Query(10, ge=1, le=50),
    range: str = Query("30d", regex="^(4h|12h|24h|7d|30d)$"),
    model_type: str | None = Query(None, description="篩選模型類型: llm/embedding/agent"),
    department_id: int | None = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if _deputy_platform_aggregate(current_user):
        user_id, department_id, scope_ids = None, None, None
    else:
        user_id, department_id, scope_ids = _resolve_usage_caller_filters(
            db, current_user, department_id
        )
    return get_top_models(
        db,
        limit=limit,
        model_type=model_type,
        user_id=user_id,
        department_id=department_id,
        scope_ids=scope_ids,
        range_key=range,
    )


@router.get("/top-users", response_model=list[TopUserUsage])
def top_users(
    limit: int = Query(10, ge=1, le=50),
    range: str = Query("30d", regex="^(4h|12h|24h|7d|30d)$"),
    model_type: str | None = Query(None, description="篩選模型類型: llm/embedding/agent"),
    department_id: int | None = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if is_deputy(current_user) and not is_admin_tier(current_user):
        raise HTTPException(status_code=403, detail=_DEPUTY_AGGREGATE_ONLY)
    default_scope = _require_admin_or_unit_admin(db, current_user)
    if default_scope is not None:
        # unit admin: force/validate scope
        if department_id is None:
            scope_ids = default_scope
            department_id = None
        elif department_id not in set(default_scope):
            raise HTTPException(
                status_code=403,
                detail="僅能查詢自己管理單位的資料",
            )
        else:
            scope_ids = None
        return get_top_users(
            db,
            limit=limit,
            model_type=model_type,
            department_id=department_id,
            scope_ids=scope_ids,
            range_key=range,
        )
    return get_top_users(
        db,
        limit=limit,
        model_type=model_type,
        department_id=department_id,
        range_key=range,
    )


@router.get("/top-departments", response_model=list[TopDepartmentUsage])
def top_departments(
    limit: int = Query(10, ge=1, le=50),
    range: str = Query("30d", regex="^(4h|12h|24h|7d|30d)$"),
    model_type: str | None = Query(None, description="篩選模型類型: llm/embedding/agent"),
    department_id: int | None = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if _deputy_platform_aggregate(current_user):
        return get_top_departments(
            db,
            limit=limit,
            model_type=model_type,
            department_id=None,
            scope_ids=None,
            range_key=range,
        )
    default_scope = _require_admin_or_unit_admin(db, current_user)
    if default_scope is not None:
        if department_id is None:
            scope_ids = default_scope
            department_id = None
        elif department_id not in set(default_scope):
            raise HTTPException(
                status_code=403,
                detail="僅能查詢自己管理單位的資料",
            )
        else:
            scope_ids = None
        return get_top_departments(
            db,
            limit=limit,
            model_type=model_type,
            department_id=department_id,
            scope_ids=scope_ids,
            range_key=range,
        )
    return get_top_departments(
        db,
        limit=limit,
        model_type=model_type,
        department_id=department_id,
        range_key=range,
    )


# ── Sprint 8 X / Phase G — caller attribution rollups ───────────────────────


@router.get("/top-agents")
def top_agents(
    days: int = Query(30, ge=1, le=180),
    limit: int = Query(10, ge=1, le=50),
    department_id: int | None = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Top-N agents by token consumption over the last ``days`` days."""
    if _deputy_platform_aggregate(current_user):
        return get_top_agents(
            db, days=days, limit=limit, department_id=None, scope_ids=None,
        )
    default_scope = _require_admin_or_unit_admin(db, current_user)
    if default_scope is not None:
        if department_id is None:
            return get_top_agents(
                db, days=days, limit=limit, scope_ids=default_scope
            )
        if department_id not in set(default_scope):
            raise HTTPException(
                status_code=403,
                detail="僅能查詢自己管理單位的資料",
            )
        return get_top_agents(
            db, days=days, limit=limit, department_id=department_id
        )
    return get_top_agents(
        db, days=days, limit=limit, department_id=department_id
    )


@router.get("/by-base-model")
def by_base_model(
    days: int = Query(30, ge=1, le=180),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Group attributed token spend by ``agents.base_model_id``."""
    return get_usage_by_base_model(db, days=days)


@router.get("/by-client")
def by_client(
    days: int = Query(30, ge=1, le=180),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Group attributed token spend by ``service_clients.id`` (Router / worker)."""
    return get_usage_by_client(db, days=days)


@router.get("/agents/{agent_id}")
def usage_for_agent(
    agent_id: int,
    days: int = Query(30, ge=1, le=180),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Time-series + summary for one specific agent."""
    return get_agent_usage(db, agent_id=agent_id, days=days)


def _usage_window_filters(db, current_user, department_id):
    if _deputy_platform_aggregate(current_user):
        return None, None, None
    return _resolve_usage_caller_filters(db, current_user, department_id)


@router.get("/by-api-key")
def usage_by_api_key(
    range: str = Query("24h", regex="^(4h|12h|24h|7d|30d)$"),
    model_id: int | None = None,
    department_id: int | None = None,
    model_type: str | None = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    user_id, department_id, scope_ids = _usage_window_filters(
        db, current_user, department_id
    )
    return get_usage_by_api_key(
        db,
        range_key=range,
        model_id=model_id,
        user_id=user_id,
        model_type=model_type,
        department_id=department_id,
        scope_ids=scope_ids,
    )


@router.get("/by-unit")
def usage_by_unit(
    range: str = Query("24h", regex="^(4h|12h|24h|7d|30d)$"),
    model_id: int | None = None,
    department_id: int | None = None,
    model_type: str | None = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    user_id, department_id, scope_ids = _usage_window_filters(
        db, current_user, department_id
    )
    return get_usage_by_unit(
        db,
        range_key=range,
        model_id=model_id,
        user_id=user_id,
        model_type=model_type,
        department_id=department_id,
        scope_ids=scope_ids,
    )


@router.get("/quota-blocks")
def usage_quota_blocks(
    range: str = Query("24h", regex="^(4h|12h|24h|7d|30d)$"),
    department_id: int | None = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if is_deputy(current_user) and not is_admin_tier(current_user):
        raise HTTPException(status_code=403, detail=_DEPUTY_AGGREGATE_ONLY)
    from app.utils.time_helpers import get_time_range

    start_time, _bucket = get_time_range(range)
    user_id, department_id, scope_ids = _usage_window_filters(
        db, current_user, department_id
    )
    return list_quota_blocks(
        db,
        start_time=start_time,
        user_id=user_id,
        department_id=department_id,
        scope_ids=scope_ids,
    )


@router.get("/export")
def export_csv(
    range: str = Query("24h", regex="^(4h|12h|24h|7d|30d)$"),
    model_id: int | None = None,
    user_id: int | None = None,
    department_id: int | None = None,
    model_type: str | None = None,
    preset: str | None = Query(None, regex="^(this_month|last_month|this_quarter)$"),
    start: str | None = None,
    end: str | None = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    # CSV 每一列都是某人、某部門、某模型的用量，不是彙總。代理管理員不能匯出。
    if is_deputy(current_user) and not is_admin_tier(current_user):
        raise HTTPException(status_code=403, detail="代理管理員不能匯出用量明細")
    if _deputy_platform_aggregate(current_user, user_id=user_id):
        user_id = None
        department_id = None
        scope_ids = None
    elif is_admin_tier(current_user):
        scope_ids = None
    elif is_unit_admin(db, current_user):
        user_id, department_id, scope_ids = _resolve_usage_caller_filters(
            db, current_user, department_id
        )
    else:
        user_id = current_user.id
        department_id = None
        scope_ids = None

    try:
        resolve_export_window(range, preset=preset, start=start, end=end)
    except ExportRangeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    filters = dict(
        model_id=model_id,
        user_id=user_id,
        model_type=model_type,
        department_id=department_id,
        scope_ids=scope_ids,
        preset=preset,
        start=start,
        end=end,
    )

    def _stream():
        owned = SessionLocal()
        try:
            result = export_usage_csv(owned, range, **filters)
            if isinstance(result, str):
                yield result
                return
            yield from result
        finally:
            owned.close()

    return StreamingResponse(
        _stream(),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=usage_{range}.csv"},
    )
