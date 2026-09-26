# -*- coding: utf-8 -*-
"""二十顆立即生效設定的治理 API。

登錄表只包含 C 類設定，因此這個 API 不再描述秘密、部署事實、重啟
需求或開機覆蓋快照。每次 overview 與每次更新後的回應都重新走同一條
``platform_settings -> env -> default`` 解析鏈。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.audit_log import AuditLog
from app.models.platform_setting import (
    _NO_VALUE,
    _usable_or_nothing,
    PlatformSetting,
    resolve_setting,
    set_setting,
)
from app.models.user import User
from app.schemas.base import ApiResponseModel
from app.services.audit_service import log_audit_event
from app.services.auth_service import require_admin
from app.services.settings_registry import (
    SETTINGS,
    SettingSpec,
    UnknownSettingError,
    require_spec,
)
from app.utils.client_ip import client_ip as _client_ip

OVERVIEW_PATH = "/api/platform-settings/overview"
SOURCE_DB = "db"
SOURCE_ENV = "env"
SOURCE_DEFAULT = "default"

router = APIRouter(
    prefix="/api/platform-settings",
    tags=["平台設定"],
    dependencies=[Depends(require_admin)],
)


class SettingItem(ApiResponseModel):
    key: str
    setting_class: str = Field(serialization_alias="class")
    description: str
    env_name: str | None
    value_type: str
    editable: bool
    default: Any | None
    effective: Any | None
    stored: str | None
    stored_usable: bool
    source: str
    updated_at: datetime | None
    updated_by: str | None


class PlatformSettingsOverview(ApiResponseModel):
    total: int
    items: list[SettingItem]


class SettingUpdate(BaseModel):
    value: Any


def _describe(
    db: Session,
    spec: SettingSpec,
    row: PlatformSetting | None,
    updated_by: str | None,
) -> SettingItem:
    effective, source = resolve_setting(db, spec.key)
    stored_usable = False
    if row is not None:
        stored_usable = _usable_or_nothing(spec, row.value, SOURCE_DB) is not _NO_VALUE
    return SettingItem(
        key=spec.key,
        setting_class=spec.setting_class.value,
        description=spec.description,
        env_name=spec.env_name,
        value_type=spec.value_type.name,
        editable=True,
        default=spec.default,
        effective=effective,
        stored=row.value if row is not None else None,
        stored_usable=stored_usable,
        source=source,
        updated_at=row.updated_at if row is not None else None,
        updated_by=updated_by,
    )


def _rows_and_actors(db: Session) -> tuple[dict[str, PlatformSetting], dict[str, str]]:
    rows = {row.key: row for row in db.query(PlatformSetting).all()}
    actor_ids = {row.updated_by_user_id for row in rows.values() if row.updated_by_user_id}
    names: dict[int, str] = {}
    if actor_ids:
        for user in db.query(User).filter(User.id.in_(actor_ids)).all():
            names[user.id] = user.username
    return rows, {
        key: names[row.updated_by_user_id]
        for key, row in rows.items()
        if row.updated_by_user_id in names
    }


def _count_audit_events(db: Session, key: str) -> int:
    return (
        db.query(func.count(AuditLog.id))
        .filter(
            AuditLog.action == "platform_setting_set",
            AuditLog.resource_type == "platform_setting",
            AuditLog.resource_id == str(key),
        )
        .scalar()
        or 0
    )


@router.get("/overview", response_model=PlatformSettingsOverview)
def read_overview(db: Session = Depends(get_db)) -> PlatformSettingsOverview:
    rows, actors = _rows_and_actors(db)
    return PlatformSettingsOverview(
        total=len(SETTINGS),
        items=[
            _describe(db, spec, rows.get(spec.key), actors.get(spec.key))
            for spec in SETTINGS
        ],
    )


@router.put("/{key}", response_model=SettingItem)
def update_setting(
    key: str,
    payload: SettingUpdate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
) -> SettingItem:
    try:
        spec = require_spec(key)
    except UnknownSettingError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"沒有 {key} 這個設定。完整名單見 GET {OVERVIEW_PATH}。"
            ),
        )

    previous = resolve_setting(db, key)[0]
    audit_before = _count_audit_events(db, key)
    stored_before_row = db.get(PlatformSetting, key)
    stored_before = stored_before_row.value if stored_before_row is not None else None
    try:
        set_setting(db, key, payload.value, actor=current_user)
    except (ValueError, TypeError) as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))

    row = db.get(PlatformSetting, key)
    rendered = row.value if row is not None else None
    stored_now = _usable_or_nothing(spec, rendered, SOURCE_DB) if rendered is not None else _NO_VALUE
    log_audit_event(
        db,
        commit=True,
        actor=current_user,
        action="platform_setting_set",
        resource_type="platform_setting",
        resource_id=key,
        ip_address=_client_ip(request),
        metadata={
            "from": previous,
            "to": None if stored_now is _NO_VALUE else stored_now,
            "stored_before": stored_before,
            "class": spec.setting_class.value,
        },
    )

    db.expire_all()
    persisted = db.get(PlatformSetting, key)
    audit_now = _count_audit_events(db, key)
    if persisted is None or persisted.value != rendered or audit_now != audit_before + 1:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=(
                f"{key} 沒有存起來：寫入後查證發現設定或稽核事件不在 DB 裡，"
                "這一次修改已經回復，請稍後重試。"
            ),
        )
    return _describe(db, spec, persisted, current_user.username)
