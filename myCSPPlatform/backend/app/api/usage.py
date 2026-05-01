from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from app.database import get_db
from app.models.user import User
from app.schemas.token_usage import (
    UsageSummary,
    ChartDataResponse,
    TopModelUsage,
    TopUserUsage,
    TopDepartmentUsage,
)
from app.services.auth_service import get_current_user, require_admin
from app.services.usage_service import (
    export_usage_csv,
    get_agent_usage,
    get_chart_data,
    get_top_agents,
    get_top_departments,
    get_top_models,
    get_top_users,
    get_usage_by_base_model,
    get_usage_by_client,
    get_usage_summary,
)

router = APIRouter(prefix="/api/usage", tags=["用量統計"])


@router.get("/summary", response_model=UsageSummary)
def usage_summary(
    range: str = Query("24h", regex="^(4h|12h|24h|7d|30d)$"),
    model_id: int | None = None,
    model_type: str | None = Query(None, description="篩選模型類型: llm/vlm/embedding/agent"),
    department_id: int | None = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    user_id = None if current_user.role == "admin" else current_user.id
    if current_user.role != "admin":
        department_id = None
    return get_usage_summary(
        db,
        range_key=range,
        model_id=model_id,
        user_id=user_id,
        model_type=model_type,
        department_id=department_id,
    )


@router.get("/chart", response_model=ChartDataResponse)
def usage_chart(
    range: str = Query("24h", regex="^(4h|12h|24h|7d|30d)$"),
    model_id: int | None = None,
    user_id: int | None = None,
    department_id: int | None = None,
    model_type: str | None = Query(None, description="篩選模型類型: llm/vlm/embedding/agent"),
    group_by: str = Query("total", regex="^(department|model|user|total)$"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    # Non-admin can only see their own data
    if current_user.role != "admin":
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
    model_type: str | None = Query(None, description="篩選模型類型: llm/vlm/embedding/agent"),
    department_id: int | None = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    user_id = None if current_user.role == "admin" else current_user.id
    if current_user.role != "admin":
        department_id = None
    return get_top_models(
        db,
        limit=limit,
        model_type=model_type,
        user_id=user_id,
        department_id=department_id,
    )


@router.get("/top-users", response_model=list[TopUserUsage])
def top_users(
    limit: int = Query(10, ge=1, le=50),
    model_type: str | None = Query(None, description="篩選模型類型: llm/vlm/embedding/agent"),
    department_id: int | None = None,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    return get_top_users(
        db,
        limit=limit,
        model_type=model_type,
        department_id=department_id,
    )


@router.get("/top-departments", response_model=list[TopDepartmentUsage])
def top_departments(
    limit: int = Query(10, ge=1, le=50),
    model_type: str | None = Query(None, description="篩選模型類型: llm/vlm/embedding/agent"),
    department_id: int | None = None,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    return get_top_departments(
        db,
        limit=limit,
        model_type=model_type,
        department_id=department_id,
    )


# ── Sprint 8 X / Phase G — caller attribution rollups ───────────────────────


@router.get("/top-agents")
def top_agents(
    days: int = Query(30, ge=1, le=180),
    limit: int = Query(10, ge=1, le=50),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Top-N agents by token consumption over the last ``days`` days."""
    return get_top_agents(db, days=days, limit=limit)


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


@router.get("/export")
def export_csv(
    range: str = Query("24h", regex="^(4h|12h|24h|7d|30d)$"),
    model_id: int | None = None,
    user_id: int | None = None,
    department_id: int | None = None,
    model_type: str | None = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if current_user.role != "admin":
        user_id = current_user.id
        department_id = None

    csv_content = export_usage_csv(
        db,
        range,
        model_id=model_id,
        user_id=user_id,
        model_type=model_type,
        department_id=department_id,
    )

    return StreamingResponse(
        iter([csv_content]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=usage_{range}.csv"},
    )
