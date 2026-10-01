"""把治理中心的非密設定搬到另一套環境。

匯出與匯入都只給擁有者與管理員。匯入在 cookie 登入時要過 CSRF。
金鑰不在檔案裡；試算只讀，套用才寫。匯入本文上限 2 MB，
讀取時逐塊累加，Content-Length 缺漏或小於實際長度也在超過時停下。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.user import User
from app.schemas.settings_transfer import SettingsImportRequest
from app.services.audit_service import log_audit_event_or_raise
from app.services.auth_service import require_admin
from app.services.settings_transfer import (
    SettingsTransferError,
    build_export,
    run_import,
)
from app.utils.client_ip import client_ip as _client_ip

router = APIRouter(prefix="/api/admin", tags=["設定匯出"])

_MAX_IMPORT_BYTES = 2 * 1024 * 1024


async def _read_import_body(request: Request) -> bytes:
    """逐塊讀，累計超過 2 MB 立刻停。不要先把整個 body 放進記憶體。

    Content-Length 大於上限時直接拒絕。沒有這個標頭，或標頭比實際內文小，
    仍靠累計長度停，不會把後面的塊讀完。
    """
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > _MAX_IMPORT_BYTES:
        raise HTTPException(status_code=413, detail="匯入檔不可超過 2 MB")
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        if not chunk:
            continue
        total += len(chunk)
        if total > _MAX_IMPORT_BYTES:
            raise HTTPException(status_code=413, detail="匯入檔不可超過 2 MB")
        chunks.append(chunk)
    return b"".join(chunks)


@router.get("/settings-export")
def export_settings(
    request: Request,
    actor: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    document = build_export(db, actor)
    log_audit_event_or_raise(
        db,
        actor=actor,
        action="settings_export",
        resource_type="settings",
        resource_id="export",
        detail=(
            f"匯出設定：信任主機 {len(document['trusted_hosts'])}、"
            f"模型 {len(document['models'])}、"
            f"外部服務 {len(document['external_services'])}"
        ),
        ip_address=_client_ip(request),
        commit=True,
    )
    return document


@router.post("/settings-import")
async def import_settings(
    request: Request,
    actor: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    raw = await _read_import_body(request)
    try:
        body = SettingsImportRequest.model_validate_json(raw)
    except ValidationError as exc:
        raise RequestValidationError(exc.errors()) from exc
    try:
        return run_import(
            db,
            actor,
            body.document,
            dry_run=body.dry_run,
            ip_address=_client_ip(request),
        )
    except SettingsTransferError as exc:
        raise HTTPException(status_code=400, detail="；".join(exc.messages)) from exc
