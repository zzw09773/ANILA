"""卡片登入後，用這一個人的人資資料補單位與職稱權限。

不改 role、不核准、不停用也不重新啟用。人資連不上時登入照舊。
兩個開關各自決定要不要依職稱授與單位管理員與降密審批。
開關打開、而且人資至少有一個職稱時才授與。職稱清單留空表示任何職稱都算，
有填才只限完全相符的職稱。開關關掉時，這個人下次登入收回該種 source 為 hr 的列。
手授與的不動。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.classification import ClassificationAuthorityAssignment
from app.models.department import Department
from app.models.hr_oracle import DEFAULT_ROOT_UNIT_NAME, HrOracleSettings
from app.models.unit_admin_assignment import UnitAdminAssignment
from app.models.user import User
from app.services.audit_service import log_audit_event
from app.services.auth_service import is_admin_tier
from app.services.department_tree import (
    acquire_dept_tree_lock,
    depth_under_parent,
    max_depth,
)
from app.services.external_service_crypto import open_external_credential
from app.services.hr_lookup import (
    DEPT_NAME_LIMIT,
    HrConnection,
    HrUnavailable,
    StaffRecord,
    lookup_staff,
    normalize_text,
)

logger = logging.getLogger(__name__)

SOURCE_HR = "hr"
AUTHORITY_REFERENCE = "人資職稱"


class _PlacementError(Exception):
    def __init__(self, code: str, created: list[Department] | None = None):
        self.code = code
        self.created = created or []


def _release(db: Session) -> None:
    """把這次請求佔著的連線還回去，再去等人資。"""
    if db.in_transaction():
        db.commit()
    db.close()


def _audit(db: Session, user: User, action: str, detail: str, ip_address: str | None) -> None:
    log_audit_event(
        db,
        actor=user,
        action=action,
        resource_type="user",
        resource_id=user.id,
        detail=detail,
        ip_address=ip_address,
        commit=False,
    )


def _audit_unavailable(db, user, exc: HrUnavailable, ip_address: str | None) -> None:
    detail = f"人資資料庫無法查詢：{exc.error_type}"
    if exc.oracle_code:
        detail = f"{detail} {exc.oracle_code}"
    logger.warning(
        "hr lookup unavailable type=%s code=%s",
        exc.error_type,
        exc.oracle_code or "-",
    )
    _audit(db, user, "hr_lookup_unavailable", detail, ip_address)


def _persist(db: Session) -> None:
    """先提交人資變更。閒置停用對不到列時會 rollback，不能把這次寫入一起清掉。"""
    try:
        db.commit()
    except Exception:
        logger.warning("hr persist failed type=unexpected")
        try:
            db.rollback()
        except Exception:
            pass


def _titles(value) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    found: list[str] = []
    for item in value:
        if not isinstance(item, str):
            continue
        title = item.strip()
        if title and title not in found:
            found.append(title)
    return tuple(found)


def _title_justified(titles: tuple[str, ...], enabled: bool, narrowing: tuple[str, ...]) -> bool:
    """開關打開且至少有一個職稱才授與。縮小清單留空表示任何職稱都算。"""
    if not enabled:
        return False
    present = _titles(list(titles))
    if not present:
        return False
    if not narrowing:
        return True
    allowed = set(narrowing)
    return any(title in allowed for title in present)


def _snapshot(row: HrOracleSettings) -> HrConnection:
    password = open_external_credential(row.password_envelope) if row.password_envelope else None
    if not password:
        raise HrUnavailable("not_configured", "人資資料庫尚未設定密碼（not_configured）")
    if not (row.host or "").strip() or not (row.service_name or "").strip() or not (row.db_user or "").strip():
        raise HrUnavailable("not_configured", "人資資料庫連線參數不完整（not_configured）")
    return HrConnection(
        host=row.host.strip(),
        port=int(row.port or 1521),
        service_name=row.service_name.strip(),
        user=row.db_user.strip(),
        password=password,
        table_name=(row.table_name or "").strip(),
    )


def _find_named(db: Session, name: str, parent_id: int | None) -> Department | None:
    query = db.query(Department).filter(Department.name == name)
    if parent_id is None:
        query = query.filter(Department.parent_id.is_(None))
    else:
        query = query.filter(Department.parent_id == parent_id)
    return query.one_or_none()


def _reuse_or_create(db: Session, name: str, parent_id: int | None) -> tuple[Department | None, bool]:
    existing = _find_named(db, name, parent_id)
    if existing is not None:
        return existing, False
    if depth_under_parent(db, parent_id) > max_depth(db):
        return None, False
    created = Department(name=name, parent_id=parent_id, is_active=True)
    nested = db.begin_nested()
    try:
        db.add(created)
        db.flush()
        nested.commit()
    except IntegrityError:
        nested.rollback()
        existing = _find_named(db, name, parent_id)
        if existing is None:
            raise
        return existing, False
    return created, True


def _active_roots(db: Session) -> list[Department]:
    return (
        db.query(Department)
        .filter(Department.parent_id.is_(None), Department.is_active.is_(True))
        .all()
    )


def _root_name(row: HrOracleSettings) -> str:
    name = normalize_text(getattr(row, "root_unit_name", None), DEPT_NAME_LIMIT)
    return name or DEFAULT_ROOT_UNIT_NAME


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _lock_user(db: Session, user_id: int) -> User | None:
    """套用前鎖住這個人。PostgreSQL 用列鎖；sqlite 測試沒有列鎖，仍先比較時間。"""
    query = db.query(User).filter(User.id == user_id)
    bind = db.get_bind()
    if bind is not None and bind.dialect.name == "postgresql":
        query = query.with_for_update()
    return query.one_or_none()


def _place(
    db: Session,
    dept1: str | None,
    dept2: str | None,
    root_name: str,
) -> tuple[Department | None, list[Department]]:
    """一級單位都是同一個院根的子節點。一級與二級同名時人就在一級節點。"""
    if dept1 is None:
        return None, []
    acquire_dept_tree_lock(db)
    roots = _active_roots(db)
    created: list[Department] = []
    if not roots:
        root, made = _reuse_or_create(db, root_name, None)
        if root is None:
            raise _PlacementError("depth", created)
        if made:
            created.append(root)
    elif len(roots) == 1:
        root = roots[0]
    else:
        matched = [row for row in roots if row.name == root_name]
        if len(matched) != 1:
            raise _PlacementError("multiple_roots", created)
        root = matched[0]
    if dept1 == root.name:
        first = root
    else:
        first, made_first = _reuse_or_create(db, dept1, root.id)
        if made_first and first is not None:
            created.append(first)
        if first is None:
            raise _PlacementError("depth", created)
    if dept2 is None or dept2 == dept1:
        return first, created
    second, made_second = _reuse_or_create(db, dept2, first.id)
    if second is None:
        raise _PlacementError("depth", created)
    if made_second:
        created.append(second)
    return second, created


def _revoke_unit_admin(db, user, row: UnitAdminAssignment, ip_address: str | None) -> None:
    if row.revoked_at is not None or row.source != SOURCE_HR:
        return
    row.revoked_at = datetime.now(timezone.utc)
    _audit(
        db,
        user,
        "hr_unit_admin_revoked",
        f"人資撤銷單位管理員 department_id={row.department_id}",
        ip_address,
    )


def _revoke_hr_unit_admins(db: Session, user: User, ip_address: str | None) -> None:
    active = (
        db.query(UnitAdminAssignment)
        .filter(
            UnitAdminAssignment.user_id == user.id,
            UnitAdminAssignment.revoked_at.is_(None),
            UnitAdminAssignment.source == SOURCE_HR,
        )
        .all()
    )
    for row in active:
        _revoke_unit_admin(db, user, row, ip_address)


def _sync_unit_admin(
    db: Session,
    user: User,
    department: Department,
    titles: tuple[str, ...],
    enabled: bool,
    narrowing: tuple[str, ...],
    ip_address: str | None,
) -> None:
    justified = _title_justified(titles, enabled, narrowing)
    active = (
        db.query(UnitAdminAssignment)
        .filter(
            UnitAdminAssignment.user_id == user.id,
            UnitAdminAssignment.revoked_at.is_(None),
            UnitAdminAssignment.source == SOURCE_HR,
        )
        .all()
    )
    if not justified:
        for row in active:
            _revoke_unit_admin(db, user, row, ip_address)
        return
    for row in active:
        if row.department_id != department.id:
            _revoke_unit_admin(db, user, row, ip_address)
    # 管理員本來就管得到全院。不因職稱再發一筆，也不因為他是管理員就收回仍符合的那筆。
    if is_admin_tier(user):
        return
    if not department.is_active:
        _audit(
            db,
            user,
            "hr_unit_admin_skipped",
            f"人資單位已停用，未授與單位管理員 department_id={department.id}",
            ip_address,
        )
        return
    existing = (
        db.query(UnitAdminAssignment)
        .filter(
            UnitAdminAssignment.user_id == user.id,
            UnitAdminAssignment.department_id == department.id,
            UnitAdminAssignment.revoked_at.is_(None),
        )
        .first()
    )
    if existing is not None:
        return
    # 主管人數由人資決定，不受「每單位最多 3 名」限制；那個上限只算指派的。
    nested = db.begin_nested()
    try:
        db.add(
            UnitAdminAssignment(
                user_id=user.id,
                department_id=department.id,
                granted_by=None,
                source=SOURCE_HR,
            )
        )
        db.flush()
        nested.commit()
    except IntegrityError:
        nested.rollback()
        _audit(
            db,
            user,
            "hr_unit_admin_skipped",
            f"人資單位管理員與既有指派衝突，略過 department_id={department.id}",
            ip_address,
        )
        return
    _audit(
        db,
        user,
        "hr_unit_admin_granted",
        f"人資授與單位管理員 department_id={department.id}（{department.name}）",
        ip_address,
    )


def _sync_declass(
    db: Session,
    user: User,
    department: Department | None,
    titles: tuple[str, ...],
    enabled: bool,
    narrowing: tuple[str, ...],
    ip_address: str | None,
) -> None:
    justified = _title_justified(titles, enabled, narrowing)
    active = (
        db.query(ClassificationAuthorityAssignment)
        .filter(
            ClassificationAuthorityAssignment.user_id == user.id,
            ClassificationAuthorityAssignment.revoked_at.is_(None),
            ClassificationAuthorityAssignment.is_active.is_(True),
            ClassificationAuthorityAssignment.source == SOURCE_HR,
        )
        .all()
    )
    if not justified:
        now = datetime.now(timezone.utc)
        for row in active:
            row.revoked_at = now
            row.is_active = False
            _audit(db, user, "hr_declass_revoked", "人資撤銷降密審批", ip_address)
        return
    keeper = active[0] if active else None
    now = datetime.now(timezone.utc)
    for extra in active[1:]:
        extra.revoked_at = now
        extra.is_active = False
        _audit(db, user, "hr_declass_revoked", "人資撤銷重複的降密審批", ip_address)
    if keeper is None:
        department_id = department.id if department is not None else None
        db.add(
            ClassificationAuthorityAssignment(
                user_id=user.id,
                department_id=department_id,
                authority_reference=AUTHORITY_REFERENCE,
                granted_by_user_id=None,
                confirmed_by_user_id=None,
                is_active=True,
                source=SOURCE_HR,
            )
        )
        db.flush()
        _audit(
            db,
            user,
            "hr_declass_granted",
            f"人資授與降密審批 department_id={department_id}",
            ip_address,
        )
        return
    if department is not None and keeper.department_id != department.id:
        keeper.department_id = department.id
        _audit(
            db,
            user,
            "hr_declass_granted",
            f"人資更新降密審批單位 department_id={department.id}",
            ip_address,
        )


def _apply_record(
    db: Session,
    user: User,
    record: StaffRecord,
    *,
    card_name: str | None,
    auto_unit_admin: bool,
    unit_admin_titles: tuple[str, ...],
    auto_declass: bool,
    declass_titles: tuple[str, ...],
    root_name: str,
    ip_address: str | None,
) -> None:
    changed: list[str] = []
    if record.name:
        if user.display_name != record.name:
            user.display_name = record.name
            changed.append("姓名")
    elif not user.display_name and card_name:
        fallback = normalize_text(card_name, DEPT_NAME_LIMIT)
        if fallback:
            user.display_name = fallback
            changed.append("姓名")
    if record.email:
        taken = (
            db.query(User.id)
            .filter(User.email == record.email, User.id != user.id)
            .first()
        )
        if taken is not None:
            _audit(db, user, "hr_email_conflict", "人資信箱與其他帳號衝突", ip_address)
        elif user.email != record.email:
            user.email = record.email
            changed.append("信箱")
    seen = list(record.titles)
    if list(user.hr_titles or []) != seen:
        user.hr_titles = seen
        changed.append("職稱")

    placed = False
    department = None
    try:
        department, created = _place(db, record.dept1, record.dept2, root_name)
        placed = True
    except _PlacementError as exc:
        if exc.code == "multiple_roots":
            detail = (
                "人資單位無法對應：有多個最上層單位，"
                f"沒有一個名稱等於根單位名稱「{root_name}」。"
                "請把根單位名稱改成其中一個，或先停用多餘的最上層單位"
            )
        else:
            detail = "人資單位超過部門層數上限，未變更單位"
        _audit(db, user, "hr_unit_unresolved", detail, ip_address)
        created = exc.created
    for dept in created:
        _audit(
            db,
            user,
            "hr_department_created",
            f"人資建立單位「{dept.name}」 id={dept.id}",
            ip_address,
        )
    if placed and department is not None:
        if user.department_id != department.id:
            if user.department_id is not None:
                previous = db.get(Department, user.department_id)
                old_name = previous.name if previous is not None else str(user.department_id)
                _audit(
                    db,
                    user,
                    "hr_unit_moved",
                    f"人資調整單位：{old_name} → {department.name}",
                    ip_address,
                )
            else:
                changed.append(f"單位={department.name}")
            user.department_id = department.id
        if user.department_source != SOURCE_HR:
            user.department_source = SOURCE_HR
            changed.append("單位來源")
        _sync_unit_admin(
            db, user, department, record.titles, auto_unit_admin, unit_admin_titles, ip_address,
        )
        _sync_declass(
            db, user, department, record.titles, auto_declass, declass_titles, ip_address,
        )
    else:
        if placed and department is None:
            _audit(db, user, "hr_unit_unresolved", "人資沒有可用的單位", ip_address)
        # 沒有節點可以掛單位管理員。降密仍依這次查到的職稱授與或收回。
        _revoke_hr_unit_admins(db, user, ip_address)
        _sync_declass(
            db, user, None, record.titles, auto_declass, declass_titles, ip_address,
        )
    if changed:
        _audit(
            db,
            user,
            "hr_profile_updated",
            "人資更新帳號：" + "、".join(changed),
            ip_address,
        )


def _apply_fetched(
    db: Session,
    user_id: int,
    record: StaffRecord | None,
    started_at: datetime,
    *,
    card_name: str | None,
    auto_unit_admin: bool,
    unit_admin_titles: tuple[str, ...],
    auto_declass: bool,
    declass_titles: tuple[str, ...],
    root_name: str,
    ip_address: str | None,
) -> User | None:
    """套用一次已經查完的結果。較晚開始的查詢已經寫入時，這次整筆跳過。"""
    user = _lock_user(db, user_id)
    if user is None:
        return None
    previous = user.hr_lookup_started_at
    if previous is not None and _as_utc(previous) > _as_utc(started_at):
        return user
    user.hr_lookup_started_at = started_at
    if record is None:
        # 查詢成功而且沒有這個人：人資不再支持先前授與的列。手授與的不動。
        _revoke_hr_unit_admins(db, user, ip_address)
        _sync_declass(db, user, None, (), auto_declass, declass_titles, ip_address)
        _persist(db)
        return user
    try:
        _apply_record(
            db,
            user,
            record,
            card_name=card_name,
            auto_unit_admin=auto_unit_admin,
            unit_admin_titles=unit_admin_titles,
            auto_declass=auto_declass,
            declass_titles=declass_titles,
            root_name=root_name,
            ip_address=ip_address,
        )
    except Exception:
        logger.warning("hr apply failed type=unexpected")
        db.rollback()
        user = db.get(User, user_id) or user
        _audit_unavailable(
            db,
            user,
            HrUnavailable("unavailable", "人資資料無法寫入（unavailable）"),
            ip_address,
        )
    _persist(db)
    return user


def apply_hr_on_card_login(db: Session, user: User, claims, *, ip_address: str | None = None) -> User:
    """人資啟用時補這一筆。失敗只記一次，登入流程繼續。開關關掉時什麼都不改。"""
    row = db.get(HrOracleSettings, 1)
    if row is None or not row.enabled:
        return user
    user_id = user.id
    employee_no = user.username
    card_name = getattr(claims, "display_name", None)
    try:
        connection = _snapshot(row)
        auto_unit_admin = bool(row.auto_unit_admin)
        unit_admin_titles = _titles(row.unit_admin_titles)
        auto_declass = bool(row.auto_declass)
        declass_titles = _titles(row.declass_titles)
        root_name = _root_name(row)
    except HrUnavailable as exc:
        _audit_unavailable(db, user, exc, ip_address)
        _persist(db)
        return user
    except Exception:
        logger.warning("hr settings unreadable type=unexpected")
        _audit_unavailable(
            db,
            user,
            HrUnavailable("unavailable", "人資資料庫無法查詢（unavailable）"),
            ip_address,
        )
        _persist(db)
        return user
    started_at = datetime.now(timezone.utc)
    _release(db)
    try:
        record = lookup_staff(employee_no, connection)
    except HrUnavailable as exc:
        user = db.get(User, user_id) or user
        _audit_unavailable(db, user, exc, ip_address)
        _persist(db)
        return user
    except Exception:
        logger.warning("hr lookup failed type=unexpected")
        user = db.get(User, user_id) or user
        _audit_unavailable(
            db,
            user,
            HrUnavailable("unavailable", "人資資料庫無法查詢（unavailable）"),
            ip_address,
        )
        _persist(db)
        return user
    applied = _apply_fetched(
        db,
        user_id,
        record,
        started_at,
        card_name=card_name,
        auto_unit_admin=auto_unit_admin,
        unit_admin_titles=unit_admin_titles,
        auto_declass=auto_declass,
        declass_titles=declass_titles,
        root_name=root_name,
        ip_address=ip_address,
    )
    return applied or user


def confirm_hr_authority_for_approval(db: Session, user_id: int, *, ip_address: str | None = None) -> None:
    """降密核准前再查一次。人資沒開就不改。連不上時把 HrUnavailable 丟回去，呼叫端拒絕核准。"""
    user = db.get(User, user_id)
    if user is None:
        return
    row = db.get(HrOracleSettings, 1)
    if row is None or not row.enabled:
        return
    employee_no = user.username
    connection = _snapshot(row)
    auto_unit_admin = bool(row.auto_unit_admin)
    unit_admin_titles = _titles(row.unit_admin_titles)
    auto_declass = bool(row.auto_declass)
    declass_titles = _titles(row.declass_titles)
    root_name = _root_name(row)
    started_at = datetime.now(timezone.utc)
    _release(db)
    record = lookup_staff(employee_no, connection)
    _apply_fetched(
        db,
        user_id,
        record,
        started_at,
        card_name=None,
        auto_unit_admin=auto_unit_admin,
        unit_admin_titles=unit_admin_titles,
        auto_declass=auto_declass,
        declass_titles=declass_titles,
        root_name=root_name,
        ip_address=ip_address,
    )
