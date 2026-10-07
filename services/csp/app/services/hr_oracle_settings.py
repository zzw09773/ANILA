"""人資資料庫設定。密碼只進加密欄，讀取只回答有沒有設。"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from anila_core.security.url_guard import UnsafeEndpointError

from app.models.department import Department
from app.models.hr_oracle import DEFAULT_HR_TITLES, DEFAULT_ROOT_UNIT_NAME, HrOracleSettings
from app.models.user import User
from app.services.audit_service import log_audit_event
from app.services.external_service_crypto import (
    encrypt_external_credential,
    open_external_credential,
)
from app.services.hr_lookup import (
    TABLE_NAME_RE,
    HrConnection,
    HrUnavailable,
    enforce_oracle_host,
    lookup_staff,
)

logger = logging.getLogger(__name__)

_TITLE_LIMIT = 40
_TITLE_LEN = 40


class HrSettingsError(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def _clean_titles(value) -> list[str]:
    if not isinstance(value, list):
        raise HrSettingsError("職稱清單必須是陣列")
    found: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise HrSettingsError("職稱必須是文字")
        if any(ord(ch) < 32 or ord(ch) == 127 for ch in item):
            raise HrSettingsError("職稱含有不允許的字元")
        title = item.strip()
        if not title:
            continue
        if len(title) > _TITLE_LEN:
            raise HrSettingsError("職稱過長")
        if title not in found:
            found.append(title)
    if len(found) > _TITLE_LIMIT:
        raise HrSettingsError("職稱清單過長")
    return found


def _public_titles(value) -> list[str]:
    if not isinstance(value, list):
        return list(DEFAULT_HR_TITLES)
    return [item for item in value if isinstance(item, str)]


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _clean_root_name(value: str) -> str:
    text = value or ""
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in text):
        raise HrSettingsError("根單位名稱含有不允許的字元")
    text = " ".join(text.split())
    if not text:
        raise HrSettingsError("請填根單位名稱")
    if len(text) > 100:
        raise HrSettingsError("根單位名稱過長")
    return text


def root_placement_note(db: Session, root_name: str) -> str | None:
    """好幾個最上層單位、又沒有一個等於根單位名稱時，設定頁要講原因與怎麼處理。"""
    roots = (
        db.query(Department)
        .filter(Department.parent_id.is_(None), Department.is_active.is_(True))
        .all()
    )
    if len(roots) < 2:
        return None
    if any(row.name == root_name for row in roots):
        return None
    names = "、".join(sorted(row.name for row in roots))
    return (
        f"有多個最上層單位（{names}），沒有一個名稱等於根單位名稱「{root_name}」。"
        "請把根單位名稱改成其中一個，或先停用多餘的最上層單位。"
        "在這之前，人資登入無法把一級單位掛到同一個根。"
    )


def public_view(row: HrOracleSettings | None, db: Session | None = None) -> dict:
    if row is None:
        view = {
            "enabled": False,
            "host": "",
            "port": 1521,
            "service_name": "",
            "user": "",
            "has_password": False,
            "table_name": "",
            "unit_admin_titles": list(DEFAULT_HR_TITLES),
            "declass_titles": list(DEFAULT_HR_TITLES),
            "root_unit_name": DEFAULT_ROOT_UNIT_NAME,
            "health_status": "unknown",
            "health_checked_at": None,
            "health_detail": None,
        }
    else:
        checked = row.health_checked_at
        view = {
            "enabled": bool(row.enabled),
            "host": row.host or "",
            "port": int(row.port or 1521),
            "service_name": row.service_name or "",
            "user": row.db_user or "",
            "has_password": bool(row.password_envelope),
            "table_name": row.table_name or "",
            "unit_admin_titles": _public_titles(row.unit_admin_titles),
            "declass_titles": _public_titles(row.declass_titles),
            "root_unit_name": row.root_unit_name or DEFAULT_ROOT_UNIT_NAME,
            "health_status": row.health_status or "unknown",
            "health_checked_at": _iso(checked),
            "health_detail": row.health_detail,
        }
    view["placement_note"] = (
        root_placement_note(db, view["root_unit_name"]) if db is not None else None
    )
    return view


def _load(db: Session) -> HrOracleSettings:
    row = db.get(HrOracleSettings, 1)
    if row is None:
        row = HrOracleSettings(id=1)
        db.add(row)
        db.flush()
    return row


def update_settings(
    db: Session,
    *,
    enabled: bool,
    host: str,
    port: int,
    service_name: str,
    db_user: str,
    table_name: str,
    unit_admin_titles,
    declass_titles,
    root_unit_name: str,
    password_set: bool,
    password: str | None,
    actor: User,
    ip_address: str | None,
) -> HrOracleSettings:
    host = (host or "").strip()
    service_name = (service_name or "").strip()
    db_user = (db_user or "").strip()
    table_name = (table_name or "").strip()
    if port < 1 or port > 65535:
        raise HrSettingsError("埠必須介於 1 到 65535")
    if table_name and not TABLE_NAME_RE.fullmatch(table_name):
        raise HrSettingsError("資料表名稱不符合規定")
    if host:
        enforce_oracle_host(host, port)
    titles_admin = _clean_titles(unit_admin_titles)
    titles_declass = _clean_titles(declass_titles)
    root_name = _clean_root_name(root_unit_name)
    row = _load(db)
    password_state = "unchanged"
    if password_set:
        if password:
            row.password_envelope = encrypt_external_credential(password)
            password_state = "set"
        else:
            row.password_envelope = None
            password_state = "cleared"
    if enabled:
        if not host or not service_name or not db_user or not table_name:
            raise HrSettingsError("啟用時要填主機、服務名稱、帳號與資料表")
        if not row.password_envelope:
            raise HrSettingsError("啟用時要設定密碼")
    row.enabled = bool(enabled)
    row.host = host
    row.port = port
    row.service_name = service_name
    row.db_user = db_user
    row.table_name = table_name
    row.unit_admin_titles = titles_admin
    row.declass_titles = titles_declass
    row.root_unit_name = root_name
    row.updated_at = datetime.now(timezone.utc)
    row.updated_by_user_id = actor.id
    db.commit()
    db.refresh(row)
    log_audit_event(
        db,
        actor=actor,
        action="hr_oracle_settings_update",
        resource_type="hr_oracle",
        resource_id=row.id,
        detail=(
            f"人資資料庫設定 enabled={row.enabled} host={row.host} "
            f"port={row.port} table={row.table_name} root={row.root_unit_name} "
            f"password={password_state}"
        ),
        ip_address=ip_address,
        commit=True,
    )
    return row


def _remember(db: Session, status: str, detail: str) -> None:
    row = db.get(HrOracleSettings, 1)
    if row is None:
        return
    row.health_status = status
    row.health_detail = detail[:500]
    row.health_checked_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(row)


def test_connection(db: Session, employee_no: str, *, actor: User, ip_address: str | None) -> dict:
    employee_no = (employee_no or "").strip()
    if (
        not employee_no
        or len(employee_no) > 64
        or any(ord(ch) < 32 or ord(ch) == 127 for ch in employee_no)
    ):
        raise HrSettingsError("請填員工編號")
    row = db.get(HrOracleSettings, 1)
    if row is None or not row.enabled:
        raise HrSettingsError("人資資料庫尚未啟用")
    password = open_external_credential(row.password_envelope) if row.password_envelope else None
    if not password or not (row.host or "").strip() or not (row.table_name or "").strip():
        raise HrSettingsError("人資資料庫連線參數不完整")
    connection = HrConnection(
        host=row.host.strip(),
        port=int(row.port or 1521),
        service_name=(row.service_name or "").strip(),
        user=(row.db_user or "").strip(),
        password=password,
        table_name=row.table_name.strip(),
    )
    if db.in_transaction():
        db.commit()
    db.close()
    try:
        record = lookup_staff(employee_no, connection)
    except UnsafeEndpointError as exc:
        raise exc
    except HrUnavailable as exc:
        message = exc.message
        if password and password in message:
            message = message.replace(password, "（已隱藏）")
        logger.warning("hr test failed type=%s code=%s", exc.error_type, exc.oracle_code or "-")
        _remember(db, "unhealthy", message)
        log_audit_event(
            db,
            actor=actor,
            action="hr_oracle_test",
            resource_type="hr_oracle",
            status="failure",
            detail=f"人資測試連線失敗：{exc.error_type}"
            + (f" {exc.oracle_code}" if exc.oracle_code else ""),
            ip_address=ip_address,
            commit=True,
        )
        return {
            "ok": False,
            "error_type": exc.error_type,
            "oracle_code": exc.oracle_code,
            "message": message,
        }
    if record is None:
        detail = "查無此人"
        _remember(db, "healthy", detail)
        return {
            "ok": True,
            "found": False,
            "name": None,
            "dept1": None,
            "dept2": None,
            "titles": [],
            "message": detail,
        }
    detail = "查詢成功"
    _remember(db, "healthy", detail)
    return {
        "ok": True,
        "found": True,
        "name": record.name,
        "dept1": record.dept1,
        "dept2": record.dept2,
        "titles": list(record.titles),
        "message": detail,
    }
