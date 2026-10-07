"""治理中心的人資資料庫。擁有者與管理員可看，只有擁有者可改。密碼不回傳。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from anila_core.security.url_guard import UnsafeEndpointError

from app.database import get_db
from app.models.hr_oracle import DEFAULT_ROOT_UNIT_NAME, HrOracleSettings
from app.models.user import User
from app.services.auth_service import require_admin, require_owner
from app.services.endpoint_rejection import unsafe_endpoint_http_detail
from app.services.hr_oracle_settings import (
    HrSettingsError,
    public_view,
    test_connection,
    update_settings,
)
from app.utils.client_ip import client_ip

router = APIRouter(tags=["人資資料庫"])


class HrOracleUpdate(BaseModel):
    enabled: bool
    host: str = ""
    port: int = 1521
    service_name: str = ""
    user: str = ""
    table_name: str = ""
    auto_unit_admin: bool = True
    auto_declass: bool = True
    unit_admin_titles: list[str] = Field(default_factory=list)
    declass_titles: list[str] = Field(default_factory=list)
    root_unit_name: str = DEFAULT_ROOT_UNIT_NAME
    password: str | None = None


class HrOracleTest(BaseModel):
    employee_no: str


def _host_detail(exc: UnsafeEndpointError):
    detail = unsafe_endpoint_http_detail(exc)
    if isinstance(detail, str):
        if "信任主機" not in detail:
            return f"{detail}。請把這台主機加到信任主機"
        return detail
    if isinstance(detail, dict) and "信任主機" not in str(detail.get("message") or ""):
        copied = dict(detail)
        copied["message"] = f"{copied.get('message') or ''}。請把這台主機加到信任主機"
        return copied
    return detail


@router.get("/api/admin/hr-database")
def get_hr_database(
    _admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    return public_view(db.get(HrOracleSettings, 1), db)


@router.put("/api/admin/hr-database")
def put_hr_database(
    body: HrOracleUpdate,
    request: Request,
    owner: User = Depends(require_owner),
    db: Session = Depends(get_db),
):
    try:
        row = update_settings(
            db,
            enabled=body.enabled,
            host=body.host,
            port=body.port,
            service_name=body.service_name,
            db_user=body.user,
            table_name=body.table_name,
            auto_unit_admin=body.auto_unit_admin,
            auto_declass=body.auto_declass,
            unit_admin_titles=body.unit_admin_titles,
            declass_titles=body.declass_titles,
            root_unit_name=body.root_unit_name,
            password_set="password" in body.model_fields_set,
            password=body.password,
            actor=owner,
            ip_address=client_ip(request),
        )
    except HrSettingsError as exc:
        raise HTTPException(status_code=400, detail=exc.message) from exc
    except UnsafeEndpointError as exc:
        raise HTTPException(status_code=400, detail=_host_detail(exc)) from exc
    return public_view(row, db)


@router.post("/api/admin/hr-database/test")
def post_hr_database_test(
    body: HrOracleTest,
    request: Request,
    owner: User = Depends(require_owner),
    db: Session = Depends(get_db),
):
    try:
        return test_connection(
            db,
            body.employee_no,
            actor=owner,
            ip_address=client_ip(request),
        )
    except HrSettingsError as exc:
        raise HTTPException(status_code=400, detail=exc.message) from exc
    except UnsafeEndpointError as exc:
        raise HTTPException(status_code=400, detail=_host_detail(exc)) from exc
