# -*- coding: utf-8 -*-
"""治理中心的外部服務。

管理員改位址與憑證。頁面只問語音是否啟用且健康。
明文憑證只走服務權杖，不進瀏覽器。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from anila_core.security.url_guard import UnsafeEndpointError

from app.database import get_db
from app.models.external_service import SERVICE_KEYS
from app.models.user import User
from app.services import external_services as svc
from app.services.auth_service import get_current_user, require_admin
from app.utils.client_ip import client_ip as _client_ip

router = APIRouter(tags=["外部服務"])


class ExternalServiceUpdate(BaseModel):
    enabled: bool
    base_url: str = ""
    credential: str | None = None
    protocol: str | None = None
    openai_model: str | None = None


def _known(service_key: str) -> str:
    if service_key not in SERVICE_KEYS:
        raise HTTPException(status_code=404, detail="沒有這項外部服務")
    return service_key


@router.get("/api/admin/external-services")
def list_external_services(
    _admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    svc.ensure_rows(db)
    db.commit()
    return {
        "services": [
            svc.public_view(svc._row(db, key)) for key in SERVICE_KEYS
        ]
    }


@router.put("/api/admin/external-services/{service_key}")
def update_external_service(
    service_key: str,
    body: ExternalServiceUpdate,
    request: Request,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    _known(service_key)
    try:
        row = svc.update_service(
            db,
            service_key,
            enabled=body.enabled,
            base_url=body.base_url,
            credential_set="credential" in body.model_fields_set,
            credential=body.credential,
            protocol=body.protocol,
            openai_model=body.openai_model,
            actor=admin,
            ip_address=_client_ip(request),
        )
    except svc.ExternalServiceUrlError as exc:
        raise HTTPException(status_code=400, detail=exc.public_message) from exc
    except UnsafeEndpointError as exc:
        if exc.fixable_by_trust_host:
            detail = {
                "code": "untrusted_host",
                "host": exc.host,
                "reason": exc.reason,
                "message": str(exc),
            }
        else:
            detail = str(exc)
        raise HTTPException(status_code=400, detail=detail) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="語音協定或模型名稱不正確") from exc
    return svc.public_view(row)


@router.post("/api/admin/external-services/{service_key}/probe")
def probe_external_service(
    service_key: str,
    _admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    _known(service_key)
    # 這一支不打上游。探測在背景工作，請求裡不送憑證。
    svc.ensure_rows(db)
    db.commit()
    return svc.public_view(svc._row(db, service_key))


@router.get("/api/external-services/speech/status")
def speech_status(
    _user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """頁面載入與回到分頁時打這一支。只回快取，不探測。"""
    return svc.speech_status(db)


@router.get("/api/internal/external-services/{service_key}")
def internal_external_service(
    service_key: str,
    request: Request,
    db: Session = Depends(get_db),
    x_csp_service_token: str | None = Header(default=None, alias="X-CSP-Service-Token"),
):
    """asr 只讀語音，ingestion-worker 的 sk- 只讀文件解析。回應可能含 credential，不准記 log。"""
    _known(service_key)
    authorization = request.headers.get("authorization")
    try:
        svc.authorize_internal_read(
            db,
            service_key,
            service_token=x_csp_service_token,
            authorization=authorization,
        )
    except svc.ReaderDenied as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    return svc.internal_payload(db, service_key)
