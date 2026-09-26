# -*- coding: utf-8 -*-
"""Router 讀取三份 system prompt 的服務對服務端點。

治理中心改的是 ``platform_settings``（走 ``/api/platform-settings`` 的 admin API，
每次修改都有稽核）；anila-core-router 是另一個容器，靠這個端點在 TTL 到期時
拉目前生效的全文。只准 ``client_type='router'`` 的 service_client；admin 的瀏覽器
session 與其他服務的 token 都拿不到 prompt 全文（工單不變式 ⑥）。
"""

from __future__ import annotations

from anila_core.api import router_prompts as rp
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api._service_principal import require_admitted_service_principal
from app.database import get_db
from app.models.platform_setting import resolve_setting
from app.services import agent_credential_service
from app.services.auth_service import verify_service_token

router = APIRouter(prefix="/api/router-prompts", tags=["router prompts (s2s)"])


@router.get("")
def read_router_prompts(
    identity: agent_credential_service.CallerIdentity | None = Depends(verify_service_token),
    db: Session = Depends(get_db),
) -> dict:
    """目前生效的三份 prompt 全文與各自來源（``db`` / ``default``）。"""
    require_admitted_service_principal(
        identity,
        db=db,
        allowed_kinds=("service_client",),
        allowed_client_types=("router",),
        allow_legacy_env=False,
        endpoint="GET /api/router-prompts",
    )
    prompts: dict[str, str] = {}
    source: dict[str, str] = {}
    for key in rp.KEYS:
        value, where = resolve_setting(db, key)
        prompts[key] = value
        source[key] = where
    limits: dict[str, int] = {}
    for key in (rp.KEY_ROUND_CAP, rp.KEY_CALL_BUDGET):
        value, _where = resolve_setting(db, key)
        limits[key] = int(value)
    return {"prompts": prompts, "source": source, "limits": limits}
