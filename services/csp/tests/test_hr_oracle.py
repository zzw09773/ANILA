# -*- coding: utf-8 -*-
"""人資 Oracle：刷卡當下查一個人，補單位與職稱權限。

沒有真的 Oracle。登入流程在 lookup_staff 邊界換上假資料；
連線本身用本機的 HTTP 替身，或直接呼叫正規化函式。
"""
from __future__ import annotations

import json
import logging
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.api.auth import card as card_api
from app.config import settings
from app.models.audit_log import AuditLog
from app.models.classification import (
    ClassificationAuthorityAssignment,
    DeclassificationRequest,
)
from app.models.department import Department
from app.models.hr_oracle import HrOracleSettings
from app.models.unit_admin_assignment import UnitAdminAssignment
from app.models.user import User
from app.modules.policy.service import has_declassification_authority
from app.services.card_auth import CardClaims
from app.services.card_auth_service import issue_registration_token
from app.services.external_service_crypto import (
    encrypt_external_credential,
    open_external_credential,
)
from app.services.hr_lookup import (
    HrConnection,
    HrUnavailable,
    StaffRecord,
    lookup_staff,
    normalize_text,
    split_titles,
    staff_from_rows,
)
from app.services.unit_admin_service import get_unit_admin_scope_ids

from tests.conftest import login, make_user


SECRET = "hr-pw-7f3a91c0-not-oracle"
VERIFY_URL = "/api/auth/card/verify"
SETTINGS_URL = "/api/admin/hr-database"


@pytest.fixture
def card_on(monkeypatch):
    monkeypatch.setattr(settings, "ANILA_AUTH_MODE", "mixed")
    monkeypatch.setattr(settings, "CARD_INITIAL_OWNERS", "")


def _enable(db: Session, **kwargs) -> HrOracleSettings:
    row = db.get(HrOracleSettings, 1)
    if row is None:
        row = HrOracleSettings(id=1)
        db.add(row)
    row.enabled = kwargs.get("enabled", True)
    row.host = kwargs.get("host", "10.8.8.8")
    row.port = kwargs.get("port", 1521)
    row.service_name = kwargs.get("service_name", "HRSVC")
    row.db_user = kwargs.get("db_user", "hr_reader")
    row.table_name = kwargs.get("table_name", "CSIIH.VIHBUY")
    password = kwargs.get("password", SECRET)
    if password is None:
        row.password_envelope = None
    else:
        row.password_envelope = encrypt_external_credential(password)
    row.auto_unit_admin = kwargs.get("auto_unit_admin", True)
    row.auto_declass = kwargs.get("auto_declass", True)
    row.unit_admin_titles = list(kwargs.get("unit_admin_titles", []))
    row.declass_titles = list(kwargs.get("declass_titles", []))
    db.commit()
    db.refresh(row)
    return row


def _record(employee_no: str, **kwargs) -> StaffRecord:
    return StaffRecord(
        employee_no=employee_no,
        name=kwargs.get("name", "人資姓名"),
        email=kwargs.get("email", "hr-person@example.invalid"),
        dept1=kwargs.get("dept1", "資訊通信研究所"),
        dept2=kwargs.get("dept2", "人工智慧組"),
        titles=tuple(kwargs.get("titles", ("組長",))),
    )


def _install_lookup(monkeypatch, producer):
    """換成假的 lookup_staff，並確認呼叫發生在資料庫連線已放開之後。"""
    from app.services import hr_login

    original_release = hr_login._release
    seen = {"released": False, "calls": 0, "employee_no": None}

    def _spy(session):
        original_release(session)
        assert session.in_transaction() is False
        seen["released"] = True

    def _lookup(employee_no, connection):
        assert seen["released"] is True
        seen["calls"] += 1
        seen["employee_no"] = employee_no
        seen["table"] = connection.table_name
        assert SECRET not in (connection.table_name or "")
        return producer(employee_no, connection)

    monkeypatch.setattr(hr_login, "_release", _spy)
    monkeypatch.setattr(hr_login, "lookup_staff", _lookup)
    return seen


def _swipe(client: TestClient, monkeypatch, user: User, **claims) -> object:
    def _fake(db, *, signature_b64, challenge_token, card_serial):
        return user, CardClaims(
            employee_id=user.username,
            display_name=claims.get("name", "卡片姓名"),
            email=claims.get("email", "card@example.invalid"),
            card_serial="SN-HR",
        )

    monkeypatch.setattr(card_api, "verify_card_and_resolve_user", _fake)
    headers = {}
    csrf = client.cookies.get("anila_csrf")
    if csrf:
        headers["X-CSRF-Token"] = csrf
    return client.post(
        VERIFY_URL,
        json={"challenge_token": "stub", "signature": "stub"},
        headers=headers,
    )


def _reload(db: Session, user: User) -> User:
    db.expire_all()
    return db.get(User, user.id)


def _audits(db: Session, user: User, action: str | None = None) -> list[AuditLog]:
    db.expire_all()
    query = db.query(AuditLog).filter(AuditLog.resource_id == str(user.id))
    if action is not None:
        query = query.filter(AuditLog.action == action)
    return query.all()


def _dept(db: Session, name: str, parent_id: int | None = None, *, active: bool = True) -> Department:
    row = Department(name=name, parent_id=parent_id, is_active=active)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _grant_unit(db, user, department, source="manual") -> UnitAdminAssignment:
    row = UnitAdminAssignment(
        user_id=user.id,
        department_id=department.id,
        granted_by=None,
        source=source,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _grant_declass(db, user, *, source="manual", department_id=None, reference="手授與簽呈"):
    row = ClassificationAuthorityAssignment(
        user_id=user.id,
        department_id=department_id,
        authority_reference=reference,
        granted_by_user_id=None,
        confirmed_by_user_id=None,
        is_active=True,
        source=source,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _active_unit(db, user) -> list[UnitAdminAssignment]:
    db.expire_all()
    return (
        db.query(UnitAdminAssignment)
        .filter(
            UnitAdminAssignment.user_id == user.id,
            UnitAdminAssignment.revoked_at.is_(None),
        )
        .all()
    )


def _active_declass(db, user) -> list[ClassificationAuthorityAssignment]:
    db.expire_all()
    return (
        db.query(ClassificationAuthorityAssignment)
        .filter(
            ClassificationAuthorityAssignment.user_id == user.id,
            ClassificationAuthorityAssignment.revoked_at.is_(None),
            ClassificationAuthorityAssignment.is_active.is_(True),
        )
        .all()
    )


def _bearer(client, db, username="hr-admin", role="admin") -> dict:
    make_user(db, username=username, role=role)
    token = login(client, username)
    return {"Authorization": f"Bearer {token}"}


def _assert_secret_absent(db: Session, *blobs: str) -> None:
    for blob in blobs:
        assert SECRET not in blob
    db.expire_all()
    for row in db.query(AuditLog).all():
        assert SECRET not in (row.detail or "")
        assert SECRET not in (row.metadata_json or "")
    for row in db.query(HrOracleSettings).all():
        assert SECRET not in (row.health_detail or "")
        assert SECRET not in (row.password_envelope or "")
        assert row.password_envelope is None or row.password_envelope.startswith("enc::ext1::")


# ── 純函式 ──────────────────────────────────────────────────────────────────


def test_staff_rows_merge_titles_and_reject_disagreement():
    base = {
        "ovc_PNO": "9100301",
        "ovc_NAME": "  王小明  ",
        "ovc_DEPT1_NAME": "資訊通信研究所",
        "ovc_DEPT2_NAME": "人工智慧組",
        "ovc_EMAIL": "first@example.invalid",
        "ovc_DUTY_DS": "組長、副組長",
    }
    second = dict(base)
    second["ovc_EMAIL"] = "second@example.invalid"
    second["ovc_DUTY_DS"] = "所長"
    second["ovc_PNO"] = 9100301
    merged = staff_from_rows("9100301", [base, second])
    assert merged is not None
    assert merged.name == "王小明"
    assert merged.email == "first@example.invalid"
    assert merged.titles == ("組長", "副組長", "所長")

    assert staff_from_rows("9100301", []) is None

    clash = dict(base)
    clash["ovc_DEPT2_NAME"] = "系統組"
    with pytest.raises(HrUnavailable) as disagree:
        staff_from_rows("9100301", [base, clash])
    assert disagree.value.error_type == "row_disagreement"

    wrong = dict(base)
    wrong["ovc_PNO"] = "9100999"
    with pytest.raises(HrUnavailable) as mismatch:
        staff_from_rows("9100301", [wrong])
    assert mismatch.value.error_type == "pno_mismatch"

    with pytest.raises(HrUnavailable) as many:
        staff_from_rows("9100301", [dict(base) for _ in range(33)])
    assert many.value.error_type == "too_many_rows"

    dirty = dict(base)
    dirty["ovc_DEPT1_NAME"] = "資訊\x00通信研究所"
    dirty["ovc_NAME"] = None
    dirty["ovc_DUTY_DS"] = "組長\x00"
    dirty["ovc_EMAIL"] = "not-an-email"
    cleaned = staff_from_rows("9100301", [dirty])
    assert cleaned is not None
    assert cleaned.dept1 is None
    assert cleaned.dept2 is None
    assert cleaned.name is None
    assert cleaned.email is None
    assert cleaned.titles == ()

    assert split_titles(" 組長、副組長\n所長 ") == ["組長", "副組長", "所長"]
    assert normalize_text("人工  智慧組", 100) == "人工 智慧組"
    assert normalize_text("人工\t智慧組", 100) is None
    assert normalize_text("組\x1f名", 100) is None


def test_table_name_is_rejected_before_any_call(monkeypatch):
    monkeypatch.setattr(
        "app.services.hr_lookup.enforce_oracle_host",
        lambda *args, **kwargs: None,
    )
    connection = HrConnection(
        host="10.8.8.8",
        port=1521,
        service_name="HRSVC",
        user="hr_reader",
        password=SECRET,
        table_name="nope;drop",
    )
    with pytest.raises(HrUnavailable) as exc:
        lookup_staff("9100301", connection)
    assert exc.value.error_type == "invalid_table"
    assert SECRET not in exc.value.message
    assert "nope;drop" not in exc.value.message


def test_csp_requirements_do_not_install_oracledb():
    from pathlib import Path

    text_file = Path(__file__).resolve().parents[1] / "requirements.txt"
    assert "oracledb" not in text_file.read_text(encoding="utf-8")


def test_lookup_http_scrubs_password_and_sends_employee_as_its_own_field(monkeypatch):
    captured: dict = {}

    class _Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            captured.update(json.loads(self.rfile.read(length).decode()))
            if captured.get("employee_no") == "9100402":
                payload = {
                    "ok": False,
                    "error_type": "connect_failed",
                    "oracle_code": SECRET,
                    "message": f"失敗 {SECRET}",
                }
                status = 502
            elif captured.get("employee_no") == "9100403":
                payload = {
                    "ok": False,
                    "error_type": f"connect_failed-{SECRET}",
                    "oracle_code": "ORA-12541",
                    "message": f"listener {SECRET} ORA-12541",
                }
                status = 502
            else:
                payload = {
                    "ok": True,
                    "rows": [
                        {
                            "ovc_PNO": captured["employee_no"],
                            "ovc_NAME": "人資姓名",
                            "ovc_DEPT1_NAME": "資訊通信研究所",
                            "ovc_DEPT2_NAME": "人工智慧組",
                            "ovc_EMAIL": "hr-person@example.invalid",
                            "ovc_DUTY_DS": "組長",
                        }
                    ],
                }
                status = 200
            raw = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, fmt, *args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(
        "app.services.hr_lookup.HR_LOOKUP_URL",
        f"http://127.0.0.1:{server.server_address[1]}/lookup",
    )
    monkeypatch.setattr(
        "app.services.hr_lookup.enforce_oracle_host",
        lambda *args, **kwargs: None,
    )
    connection = HrConnection(
        host="10.8.8.8",
        port=1521,
        service_name="HRSVC",
        user="hr_reader",
        password=SECRET,
        table_name="CSIIH.VIHBUY",
    )
    try:
        found = lookup_staff("9100401", connection)
        assert found is not None and found.name == "人資姓名"
        assert captured["employee_no"] == "9100401"
        assert captured["table_name"] == "CSIIH.VIHBUY"
        assert captured["password"] == SECRET
        assert "sql" not in captured
        posted = json.dumps({key: value for key, value in captured.items() if key != "password"})
        assert "SELECT" not in posted
        assert "9100401" not in captured["table_name"]

        with pytest.raises(HrUnavailable) as hidden:
            lookup_staff("9100402", connection)
        assert hidden.value.oracle_code is None
        assert hidden.value.error_type == "connect_failed"
        assert SECRET not in hidden.value.message

        with pytest.raises(HrUnavailable) as coded:
            lookup_staff("9100403", connection)
        assert coded.value.error_type == "unavailable"
        assert coded.value.oracle_code == "ORA-12541"
        assert "ORA-12541" in coded.value.message
        assert SECRET not in coded.value.message
        assert "connect_failed" in coded.value.message or coded.value.error_type
    finally:
        server.shutdown()
        server.server_close()


def test_lookup_service_refuses_connection_is_unavailable(monkeypatch):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    monkeypatch.setattr(
        "app.services.hr_lookup.HR_LOOKUP_URL",
        f"http://127.0.0.1:{port}/lookup",
    )
    monkeypatch.setattr(
        "app.services.hr_lookup.enforce_oracle_host",
        lambda *args, **kwargs: None,
    )
    with pytest.raises(HrUnavailable) as exc:
        lookup_staff(
            "9100404",
            HrConnection("10.8.8.8", 1521, "HRSVC", "hr_reader", SECRET, "VIHBUY"),
        )
    assert exc.value.error_type == "unreachable"
    assert SECRET not in exc.value.message


# ── 刷卡 ────────────────────────────────────────────────────────────────────


def test_first_login_found_fills_profile_reuses_departments_and_approves_nobody(
    client, db, monkeypatch, card_on
):
    _enable(db)
    seen = _install_lookup(monkeypatch, lambda employee_no, connection: _record(employee_no))
    fresh = make_user(db, username="9101001", is_approved=False)
    response = _swipe(client, monkeypatch, fresh)
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["status"] == "pending_approval"
    assert body["display_name"] == "人資姓名"
    assert body["email"] == "hr-person@example.invalid"

    stored = _reload(db, fresh)
    assert stored.display_name == "人資姓名"
    assert stored.email == "hr-person@example.invalid"
    assert stored.department_source == "hr"
    assert stored.hr_titles == ["組長"]
    assert stored.is_approved is False
    assert stored.role == "user"
    assert stored.is_active is True
    assert seen["released"] is True
    assert seen["employee_no"] == "9101001"
    root = db.query(Department).filter(Department.name == "國家中山科學研究院").one()
    institute = db.query(Department).filter(Department.name == "資訊通信研究所").one()
    group = db.query(Department).filter(Department.name == "人工智慧組").one()
    assert root.parent_id is None
    assert institute.parent_id == root.id
    assert group.parent_id == institute.id
    assert stored.department_id == group.id
    assert _audits(db, fresh, "hr_unit_moved") == []
    assert len(_audits(db, fresh, "hr_department_created")) == 3
    profile = _audits(db, fresh, "hr_profile_updated")
    assert profile and "單位=人工智慧組" in profile[0].detail
    assert "人資" in profile[0].detail
    admins = _active_unit(db, fresh)
    assert len(admins) == 1
    assert admins[0].source == "hr"
    assert admins[0].granted_by is None
    assert admins[0].department_id == group.id
    declass = _active_declass(db, fresh)
    assert len(declass) == 1
    assert declass[0].source == "hr"
    assert declass[0].granted_by_user_id is None
    assert declass[0].confirmed_by_user_id is None
    assert declass[0].authority_reference == "人資職稱"
    assert declass[0].department_id == group.id
    assert has_declassification_authority(db, fresh.id) is True

    department_count = db.query(Department).count()
    approved = make_user(db, username="9101002", is_approved=True)

    def _padded(employee_no, connection):
        return staff_from_rows(
            employee_no,
            [{
                "ovc_PNO": employee_no,
                "ovc_NAME": "人資姓名",
                "ovc_DEPT1_NAME": "  資訊通信研究所  ",
                "ovc_DEPT2_NAME": "  人工智慧組  ",
                "ovc_EMAIL": "second@example.invalid",
                "ovc_DUTY_DS": "組長",
            }],
        )

    _install_lookup(monkeypatch, _padded)
    entered = _swipe(client, monkeypatch, approved)
    assert entered.status_code == 200, entered.text
    approved_row = _reload(db, approved)
    assert approved_row.is_approved is True
    assert approved_row.department_id == group.id
    assert approved_row.department_source == "hr"
    assert db.query(Department).count() == department_count
    assert _audits(db, approved, "hr_department_created") == []

    headers = _bearer(client, db, username="hr-reader-admin")
    listed = client.get(f"/api/users/{fresh.id}", headers=headers)
    assert listed.status_code == 200, listed.text
    payload = listed.json()
    assert payload["department_source"] == "hr"
    assert payload["hr_titles"] == ["組長"]
    assert payload["display_name"] == "人資姓名"
    assert payload["department_name"] == "人工智慧組"


def test_group_leader_is_scoped_to_the_group_and_institute_head_covers_the_subtree(
    client, db, monkeypatch, card_on
):
    _enable(db)
    root = _dept(db, "院")
    _install_lookup(
        monkeypatch,
        lambda employee_no, connection: _record(
            employee_no,
            name="所長甲",
            dept1="資訊通信研究所",
            dept2="資訊通信研究所",
            titles=("所長",),
            email="head@example.invalid",
        ),
    )
    head = make_user(db, username="9101101", is_approved=False)
    head_response = _swipe(client, monkeypatch, head)
    assert head_response.status_code == 202, head_response.text
    institute_rows = db.query(Department).filter(Department.name == "資訊通信研究所").all()
    assert len(institute_rows) == 1
    institute = institute_rows[0]
    assert institute.parent_id == root.id
    assert (
        db.query(Department)
        .filter(Department.parent_id == institute.id, Department.name == institute.name)
        .count()
        == 0
    )
    group = _dept(db, "人工智慧組", institute.id)
    sibling = _dept(db, "系統組", institute.id)
    head_row = _reload(db, head)
    assert head_row.department_id == institute.id
    assert get_unit_admin_scope_ids(db, head_row) == {institute.id, group.id, sibling.id}

    _install_lookup(
        monkeypatch,
        lambda employee_no, connection: _record(
            employee_no,
            name="組長乙",
            dept1="資訊通信研究所",
            dept2="人工智慧組",
            titles=("組長",),
            email="lead@example.invalid",
        ),
    )
    lead = make_user(db, username="9101102", is_approved=False)
    lead_response = _swipe(client, monkeypatch, lead)
    assert lead_response.status_code == 202, lead_response.text
    lead_row = _reload(db, lead)
    assert lead_row.department_id == group.id
    assert get_unit_admin_scope_ids(db, lead_row) == {group.id}
    assert sibling.id not in get_unit_admin_scope_ids(db, lead_row)
    assert db.query(Department).filter(Department.name == "資訊通信研究所").count() == 1
    assert db.query(Department).filter(Department.name == "人工智慧組").count() == 1


def test_unit_change_moves_the_user_and_audits_old_and_new_names(
    client, db, monkeypatch, card_on
):
    _enable(db)
    root = _dept(db, "院")
    previous = _dept(db, "舊單位", root.id)
    person = make_user(db, username="9101201", is_approved=False, department_id=previous.id)
    person.department_source = "manual"
    db.commit()

    step = {"n": 0}

    def _produce(employee_no, connection):
        dept2 = "人工智慧組" if step["n"] == 0 else "系統組"
        step["n"] += 1
        return staff_from_rows(
            employee_no,
            [
                {
                    "ovc_PNO": employee_no,
                    "ovc_NAME": "人資姓名",
                    "ovc_DEPT1_NAME": "資訊通信研究所",
                    "ovc_DEPT2_NAME": dept2,
                    "ovc_EMAIL": "move@example.invalid",
                    "ovc_DUTY_DS": "組長",
                },
                {
                    "ovc_PNO": employee_no,
                    "ovc_NAME": "人資姓名",
                    "ovc_DEPT1_NAME": "資訊通信研究所",
                    "ovc_DEPT2_NAME": dept2,
                    "ovc_EMAIL": "other@example.invalid",
                    "ovc_DUTY_DS": "副組長",
                },
            ],
        )

    _install_lookup(monkeypatch, _produce)
    first = _swipe(client, monkeypatch, person)
    assert first.status_code == 202, first.text
    moved = _reload(db, person)
    group = db.query(Department).filter(Department.name == "人工智慧組").one()
    assert moved.department_id == group.id
    assert moved.department_source == "hr"
    assert moved.hr_titles == ["組長", "副組長"]
    move_audits = _audits(db, person, "hr_unit_moved")
    assert len(move_audits) == 1
    assert "舊單位" in move_audits[0].detail
    assert "人工智慧組" in move_audits[0].detail

    second = _swipe(client, monkeypatch, person)
    assert second.status_code == 202, second.text
    again = _reload(db, person)
    other = db.query(Department).filter(Department.name == "系統組").one()
    assert again.department_id == other.id
    later = _audits(db, person, "hr_unit_moved")
    assert any("人工智慧組" in row.detail and "系統組" in row.detail for row in later)
    assert len(_active_unit(db, person)) == 1
    assert _active_unit(db, person)[0].department_id == other.id
    assert len(_active_declass(db, person)) == 1
    assert _active_declass(db, person)[0].department_id == other.id


def test_titles_grant_skip_revoke_and_leave_hand_grants(
    client, db, monkeypatch, card_on
):
    _enable(db)
    root = _dept(db, "院")
    institute = _dept(db, "資訊通信研究所", root.id)
    group = _dept(db, "人工智慧組", institute.id)
    other = _dept(db, "系統組", institute.id)
    for index in range(3):
        holder = make_user(db, username=f"910130{index}", is_approved=True)
        _grant_unit(db, holder, group, source="manual")

    def _rows(employee_no, dept2, duty):
        return staff_from_rows(
            employee_no,
            [{
                "ovc_PNO": employee_no,
                "ovc_NAME": "職稱員",
                "ovc_DEPT1_NAME": "資訊通信研究所",
                "ovc_DEPT2_NAME": dept2,
                "ovc_EMAIL": f"{employee_no}@example.invalid",
                "ovc_DUTY_DS": duty,
            }],
        )

    _install_lookup(
        monkeypatch,
        lambda employee_no, connection: _rows(employee_no, "人工智慧組", "組長、副組長"),
    )
    capped = make_user(db, username="9101310", is_approved=False)
    capped_response = _swipe(client, monkeypatch, capped)
    assert capped_response.status_code == 202, capped_response.text
    # 指派的名額已滿三人。主管由人資帶入，不占名額，照樣成為單位管理員。
    capped_rows = _active_unit(db, capped)
    assert [(row.department_id, row.source) for row in capped_rows] == [(group.id, "hr")]
    assert _audits(db, capped, "hr_unit_admin_skipped") == []
    assert _active_declass(db, capped)

    manual_user = make_user(db, username="9101311", is_approved=False)
    hand = _grant_unit(db, manual_user, other, source="manual")
    _install_lookup(
        monkeypatch,
        lambda employee_no, connection: _rows(employee_no, "系統組", "組長"),
    )
    granted = _swipe(client, monkeypatch, manual_user)
    assert granted.status_code == 202, granted.text
    assert len(_active_unit(db, manual_user)) == 1
    assert _active_unit(db, manual_user)[0].id == hand.id
    assert hand.source == "manual"

    _install_lookup(
        monkeypatch,
        lambda employee_no, connection: _rows(employee_no, "系統組", ""),
    )
    lost = _swipe(client, monkeypatch, manual_user)
    assert lost.status_code == 202, lost.text
    still = _active_unit(db, manual_user)
    assert len(still) == 1 and still[0].source == "manual" and still[0].revoked_at is None
    assert _active_declass(db, manual_user) == []
    assert _audits(db, manual_user, "hr_declass_revoked")

    promoted = make_user(db, username="9101312", is_approved=False)
    _install_lookup(
        monkeypatch,
        lambda employee_no, connection: _rows(employee_no, "系統組", "組長"),
    )
    assert _swipe(client, monkeypatch, promoted).status_code == 202
    hr_row = _active_unit(db, promoted)
    assert len(hr_row) == 1 and hr_row[0].source == "hr"
    side = _dept(db, "另一組", institute.id)
    hand_side = _grant_unit(db, promoted, side, source="manual")
    settings = db.get(HrOracleSettings, 1)
    settings.auto_unit_admin = False
    settings.auto_declass = False
    db.commit()
    _install_lookup(
        monkeypatch,
        lambda employee_no, connection: _rows(employee_no, "系統組", "組長"),
    )
    assert _swipe(client, monkeypatch, promoted).status_code == 202
    remaining = _active_unit(db, promoted)
    assert [row.id for row in remaining] == [hand_side.id]
    assert remaining[0].source == "manual"
    assert _active_declass(db, promoted) == []
    revoked = _audits(db, promoted, "hr_unit_admin_revoked")
    assert revoked and "人資" in revoked[-1].detail

    boss = make_user(db, username="9101313", role="admin", is_approved=False)
    stale = _dept(db, "舊組", institute.id)
    _grant_unit(db, boss, stale, source="hr")
    kept = _grant_unit(db, boss, other, source="hr")
    _install_lookup(
        monkeypatch,
        lambda employee_no, connection: _rows(employee_no, "系統組", "所長"),
    )
    # 兩個開關都關掉會把管理員的人資列也收回。這裡打開開關、清單留空，只測「不新發」。
    settings = db.get(HrOracleSettings, 1)
    settings.auto_unit_admin = True
    settings.auto_declass = True
    settings.unit_admin_titles = []
    settings.declass_titles = []
    db.commit()
    assert _swipe(client, monkeypatch, boss).status_code == 202
    boss_rows = _active_unit(db, boss)
    assert [row.id for row in boss_rows] == [kept.id]
    assert kept.department_id == other.id
    assert _active_declass(db, boss)
    assert _reload(db, boss).role == "admin"
    assert _reload(db, boss).is_approved is False


def _duty_record(employee_no, duty):
    return staff_from_rows(
        employee_no,
        [{
            "ovc_PNO": employee_no,
            "ovc_NAME": "專員甲",
            "ovc_DEPT1_NAME": "資訊通信研究所",
            "ovc_DEPT2_NAME": "人工智慧組",
            "ovc_EMAIL": f"{employee_no}@example.invalid",
            "ovc_DUTY_DS": duty,
        }],
    )


def _by_source(rows, source):
    return [row for row in rows if row.source == source]


def test_any_title_grants_and_each_switch_or_list_limits_one_kind(
    client, db, monkeypatch, card_on
):
    """有職稱就授與。空職稱不授與。關掉一個開關只收回那一種。清單有填才縮小。"""
    _enable(db)
    person = make_user(db, username="9102201", is_approved=True)
    _install_lookup(monkeypatch, lambda employee_no, connection: _duty_record(employee_no, "專員"))
    assert _swipe(client, monkeypatch, person).status_code == 200
    group = db.query(Department).filter(Department.name == "人工智慧組").one()
    institute = db.query(Department).filter(Department.name == "資訊通信研究所").one()
    assert [row.department_id for row in _by_source(_active_unit(db, person), "hr")] == [group.id]
    assert len(_by_source(_active_declass(db, person), "hr")) == 1
    side = _dept(db, "系統組", institute.id)
    hand = _grant_unit(db, person, side, source="manual")

    _install_lookup(monkeypatch, lambda employee_no, connection: _duty_record(employee_no, "   "))
    assert _swipe(client, monkeypatch, person).status_code == 200
    assert [row.id for row in _active_unit(db, person)] == [hand.id]
    assert _active_declass(db, person) == []

    _install_lookup(monkeypatch, lambda employee_no, connection: _duty_record(employee_no, "專員"))
    assert _swipe(client, monkeypatch, person).status_code == 200
    assert len(_by_source(_active_unit(db, person), "hr")) == 1
    assert len(_by_source(_active_declass(db, person), "hr")) == 1

    settings = db.get(HrOracleSettings, 1)
    settings.auto_unit_admin = False
    db.commit()
    assert _swipe(client, monkeypatch, person).status_code == 200
    assert _by_source(_active_unit(db, person), "hr") == []
    assert [row.id for row in _by_source(_active_unit(db, person), "manual")] == [hand.id]
    assert len(_by_source(_active_declass(db, person), "hr")) == 1

    settings.auto_unit_admin = True
    settings.auto_declass = False
    db.commit()
    assert _swipe(client, monkeypatch, person).status_code == 200
    assert len(_by_source(_active_unit(db, person), "hr")) == 1
    assert _active_declass(db, person) == []
    assert hand.id in {row.id for row in _active_unit(db, person)}

    settings.auto_declass = True
    settings.unit_admin_titles = ["  組長  "]
    settings.declass_titles = ["所長"]
    db.commit()
    _install_lookup(monkeypatch, lambda employee_no, connection: _duty_record(employee_no, "專員"))
    assert _swipe(client, monkeypatch, person).status_code == 200
    assert _by_source(_active_unit(db, person), "hr") == []
    assert _active_declass(db, person) == []
    assert hand.id in {row.id for row in _active_unit(db, person)}

    _install_lookup(monkeypatch, lambda employee_no, connection: _duty_record(employee_no, "組長"))
    assert _swipe(client, monkeypatch, person).status_code == 200
    hr_units = _by_source(_active_unit(db, person), "hr")
    assert len(hr_units) == 1 and hr_units[0].department_id == group.id
    assert _active_declass(db, person) == []

    _install_lookup(monkeypatch, lambda employee_no, connection: _duty_record(employee_no, "所長"))
    assert _swipe(client, monkeypatch, person).status_code == 200
    assert _by_source(_active_unit(db, person), "hr") == []
    assert len(_by_source(_active_declass(db, person), "hr")) == 1

    _install_lookup(
        monkeypatch, lambda employee_no, connection: _duty_record(employee_no, "組長、所長"),
    )
    assert _swipe(client, monkeypatch, person).status_code == 200
    assert len(_by_source(_active_unit(db, person), "hr")) == 1
    assert len(_by_source(_active_declass(db, person), "hr")) == 1
    db.expire_all()
    assert db.get(UnitAdminAssignment, hand.id).revoked_at is None


def test_missing_unavailable_disabled_and_driver_missing_do_not_block_login(
    client, db, monkeypatch, card_on, caplog
):
    _enable(db)
    caplog.set_level(logging.WARNING)

    def _produce(employee_no, connection):
        if employee_no == "9101401":
            return None
        if employee_no == "9101402":
            raise HrUnavailable("connect_failed", f"連不上 {SECRET}", "ORA-12541")
        if employee_no == "9101404":
            raise HrUnavailable("driver_missing", f"缺少驅動 {SECRET}")
        raise AssertionError(employee_no)

    seen = _install_lookup(monkeypatch, _produce)
    missing = make_user(db, username="9101401", is_approved=False)
    missing_response = _swipe(client, monkeypatch, missing, name="卡片姓名")
    assert missing_response.status_code == 202, missing_response.text
    assert missing_response.json()["status"] == "pending_registration"
    assert missing_response.json()["display_name"] == "卡片姓名"
    assert _reload(db, missing).display_name is None
    assert _reload(db, missing).department_id is None
    assert _audits(db, missing, "hr_lookup_unavailable") == []

    down = make_user(db, username="9101402", is_approved=False)
    down_response = _swipe(client, monkeypatch, down)
    assert down_response.status_code == 202, down_response.text
    assert down_response.json()["status"] == "pending_registration"
    unavailable = _audits(db, down, "hr_lookup_unavailable")
    assert len(unavailable) == 1
    assert "connect_failed" in unavailable[0].detail
    assert "ORA-12541" in unavailable[0].detail
    assert _reload(db, down).display_name is None

    row = db.get(HrOracleSettings, 1)
    row.enabled = False
    db.commit()
    calls_before = seen["calls"]
    disabled = make_user(db, username="9101403", is_approved=False)
    disabled_response = _swipe(client, monkeypatch, disabled)
    assert disabled_response.status_code == 202, disabled_response.text
    assert disabled_response.json()["status"] == "pending_registration"
    assert seen["calls"] == calls_before
    assert _reload(db, disabled).display_name is None
    assert _audits(db, disabled, "hr_lookup_unavailable") == []

    row.enabled = True
    db.commit()
    driver = make_user(db, username="9101404", is_approved=False)
    driver_response = _swipe(client, monkeypatch, driver)
    assert driver_response.status_code == 202, driver_response.text
    driver_audit = _audits(db, driver, "hr_lookup_unavailable")
    assert len(driver_audit) == 1
    assert "driver_missing" in driver_audit[0].detail
    _assert_secret_absent(db, down_response.text, driver_response.text, caplog.text)


def test_disabled_account_is_filled_in_but_not_reactivated(
    client, db, monkeypatch, card_on
):
    _enable(db)
    _install_lookup(monkeypatch, lambda employee_no, connection: _record(employee_no))
    person = make_user(db, username="9101501", role="user", is_approved=True)
    person.is_active = False
    db.commit()
    response = _swipe(client, monkeypatch, person)
    assert response.status_code == 403, response.text
    assert "停用" in response.json()["detail"]
    stored = _reload(db, person)
    assert stored.display_name == "人資姓名"
    assert stored.is_active is False
    assert stored.is_approved is True
    assert stored.role == "user"
    assert stored.department_source == "hr"


def test_unusable_units_do_not_move_the_user_or_change_grants(
    client, db, monkeypatch, card_on
):
    from app.models.platform_setting import set_setting

    _enable(db)
    home = _dept(db, "原單位")
    side = _dept(db, "旁邊組", home.id)
    person = make_user(db, username="9101601", is_approved=False, department_id=home.id)
    person.department_source = "manual"
    db.commit()
    hand = _grant_unit(db, person, home, source="manual")
    hr_grant = _grant_unit(db, person, side, source="hr")

    def _rows(employee_no, dept1, dept2="人工智慧組", email="clean@example.invalid", name="新人資"):
        return staff_from_rows(
            employee_no,
            [{
                "ovc_PNO": employee_no,
                "ovc_NAME": name,
                "ovc_DEPT1_NAME": dept1,
                "ovc_DEPT2_NAME": dept2,
                "ovc_EMAIL": email,
                "ovc_DUTY_DS": "組長",
            }],
        )

    _install_lookup(
        monkeypatch,
        lambda employee_no, connection: _rows(employee_no, "資訊\x00通信研究所"),
    )
    assert _swipe(client, monkeypatch, person).status_code == 202
    stored = _reload(db, person)
    assert stored.department_id == home.id
    assert stored.department_source == "manual"
    assert stored.display_name == "新人資"
    assert stored.hr_titles == ["組長"]
    assert any("人資沒有可用的單位" in row.detail for row in _audits(db, person, "hr_unit_unresolved"))
    assert {row.id for row in _active_unit(db, person)} == {hand.id}
    assert hr_grant.revoked_at is not None
    unusable_declass = _active_declass(db, person)
    assert len(unusable_declass) == 1
    assert unusable_declass[0].source == "hr"
    assert unusable_declass[0].department_id is None

    taken = make_user(db, username="9101602", is_approved=True)
    taken.email = "taken@example.invalid"
    db.commit()
    _install_lookup(
        monkeypatch,
        lambda employee_no, connection: _rows(
            employee_no, "資訊通信研究所", email="taken@example.invalid", name="信箱衝突"
        ),
    )
    mail_user = make_user(db, username="9101603", is_approved=False)
    mail_user.email = "keep@example.invalid"
    db.commit()
    mail_response = _swipe(client, monkeypatch, mail_user)
    assert mail_response.status_code == 202, mail_response.text
    assert _reload(db, mail_user).email == "keep@example.invalid"
    assert any("人資信箱" in row.detail for row in _audits(db, mail_user, "hr_email_conflict"))
    assert _reload(db, mail_user).department_id is not None

    blank = make_user(db, username="9101604", is_approved=False)
    _install_lookup(
        monkeypatch,
        lambda employee_no, connection: _rows(
            employee_no, "資訊通信研究所", email="not-an-email", name=""
        ),
    )
    blank_response = _swipe(client, monkeypatch, blank, name="卡片補名", email="card@example.invalid")
    assert blank_response.status_code == 202, blank_response.text
    assert blank_response.json()["email"] == "card@example.invalid"
    blank_row = _reload(db, blank)
    assert blank_row.email is None
    assert blank_row.display_name == "卡片補名"

    other_root = _dept(db, "另一個根")
    multi = make_user(db, username="9101605", is_approved=False, department_id=home.id)
    _install_lookup(
        monkeypatch,
        lambda employee_no, connection: _rows(employee_no, "新的所"),
    )
    assert _swipe(client, monkeypatch, multi).status_code == 202
    assert _reload(db, multi).department_id == home.id
    assert any("多個最上層單位" in row.detail for row in _audits(db, multi, "hr_unit_unresolved"))

    set_setting(db, "limits.department_max_depth", 1)
    db.commit()
    # 多個根會先擋下。這一筆改成只留一個根，層數上限才看得到。
    other_root.is_active = False
    db.commit()
    deep = make_user(db, username="9101606", is_approved=False, department_id=home.id)
    _install_lookup(
        monkeypatch,
        lambda employee_no, connection: _rows(employee_no, "深的所", "深的組"),
    )
    assert _swipe(client, monkeypatch, deep).status_code == 202
    assert _reload(db, deep).department_id == home.id
    assert any("層數上限" in row.detail for row in _audits(db, deep, "hr_unit_unresolved"))

    institute = db.query(Department).filter(Department.name == "資訊通信研究所").one()
    parked = _dept(db, "停用組", institute.id, active=False)
    inactive_user = make_user(db, username="9101607", is_approved=False)
    set_setting(db, "limits.department_max_depth", 3)
    db.commit()
    _install_lookup(
        monkeypatch,
        lambda employee_no, connection: _rows(employee_no, "資訊通信研究所", "停用組"),
    )
    assert _swipe(client, monkeypatch, inactive_user).status_code == 202
    inactive_row = _reload(db, inactive_user)
    assert inactive_row.department_id == parked.id
    assert db.get(Department, parked.id).is_active is False
    assert _active_unit(db, inactive_user) == []
    assert any("已停用" in row.detail for row in _audits(db, inactive_user, "hr_unit_admin_skipped"))
    assert _active_declass(db, inactive_user)

    clash = make_user(db, username="9101608", is_approved=False)

    def _disagree(employee_no, connection):
        return staff_from_rows(
            employee_no,
            [
                {
                    "ovc_PNO": employee_no,
                    "ovc_NAME": "甲",
                    "ovc_DEPT1_NAME": "資訊通信研究所",
                    "ovc_DEPT2_NAME": "人工智慧組",
                    "ovc_EMAIL": "a@example.invalid",
                    "ovc_DUTY_DS": "組長",
                },
                {
                    "ovc_PNO": employee_no,
                    "ovc_NAME": "乙",
                    "ovc_DEPT1_NAME": "資訊通信研究所",
                    "ovc_DEPT2_NAME": "人工智慧組",
                    "ovc_EMAIL": "b@example.invalid",
                    "ovc_DUTY_DS": "副組長",
                },
            ],
        )

    _install_lookup(monkeypatch, _disagree)
    assert _swipe(client, monkeypatch, clash).status_code == 202
    assert _reload(db, clash).department_id is None
    assert _reload(db, clash).display_name is None
    disagree_audit = _audits(db, clash, "hr_lookup_unavailable")
    assert len(disagree_audit) == 1
    assert "row_disagreement" in disagree_audit[0].detail


def test_manual_department_choice_marks_the_source(client, db, monkeypatch, card_on):
    _enable(db)
    root = _dept(db, "院")
    current = _dept(db, "人工智慧組", root.id)
    other = _dept(db, "系統組", root.id)
    person = make_user(db, username="9101701", is_approved=True, department_id=current.id)
    person.department_source = "hr"
    db.commit()
    headers = _bearer(client, db, username="hr-dept-admin")
    same = client.put(
        f"/api/users/{person.id}",
        headers=headers,
        json={"department_id": current.id},
    )
    assert same.status_code == 200, same.text
    assert _reload(db, person).department_source == "hr"
    changed = client.put(
        f"/api/users/{person.id}",
        headers=headers,
        json={"department_id": other.id},
    )
    assert changed.status_code == 200, changed.text
    assert _reload(db, person).department_source == "manual"
    assert _reload(db, person).department_id == other.id

    pending = make_user(db, username="9101702", is_approved=False)
    pending.department_source = "hr"
    db.commit()
    token, _expires = issue_registration_token(pending.id)
    done = client.post(
        "/api/auth/card/complete-registration",
        headers={"X-CSRF-Token": client.cookies.get("anila_csrf")},
        json={"registration_token": token, "department_id": current.id},
    )
    assert done.status_code == 200, done.text
    assert _reload(db, pending).department_source == "manual"
    assert _reload(db, pending).department_id == current.id
    assert _reload(db, pending).is_approved is False


# ── 治理中心設定 ────────────────────────────────────────────────────────────


def test_settings_api_password_host_csrf_and_test_button(
    client, db, monkeypatch, caplog
):
    from app.services.trusted_host_service import _invalidate_cache

    caplog.set_level(logging.WARNING)
    admin_headers = _bearer(client, db, username="hr-settings-admin", role="admin")
    headers = _bearer(client, db, username="hr-settings-owner", role="owner")
    stranger = make_user(db, username="hr-settings-user", role="user")
    user_headers = {"Authorization": f"Bearer {login(client, stranger.username)}"}

    first = client.get(SETTINGS_URL, headers=admin_headers)
    assert first.status_code == 200, first.text
    assert first.json()["enabled"] is False
    assert first.json()["port"] == 1521
    assert first.json()["has_password"] is False
    assert first.json()["auto_unit_admin"] is True
    assert first.json()["auto_declass"] is True
    assert first.json()["unit_admin_titles"] == []
    assert first.json()["declass_titles"] == []
    assert first.json()["root_unit_name"] == "國家中山科學研究院"
    assert first.json()["placement_note"] is None
    assert db.get(HrOracleSettings, 1) is None
    assert client.get(SETTINGS_URL, headers=user_headers).status_code == 403
    admin_denied = client.put(
        SETTINGS_URL,
        headers=admin_headers,
        json={
            "enabled": False,
            "host": "",
            "unit_admin_titles": [],
            "declass_titles": [],
        },
    )
    assert admin_denied.status_code == 403, admin_denied.text
    assert "需要 owner 權限" in admin_denied.text

    from app.services import hr_oracle_settings as hr_settings

    original_enforce = hr_settings.enforce_oracle_host
    monkeypatch.setattr(hr_settings, "enforce_oracle_host", lambda *args, **kwargs: None)
    incomplete = client.put(
        SETTINGS_URL,
        headers=headers,
        json={
            "enabled": True,
            "host": "10.8.8.8",
            "port": 1521,
            "service_name": "HRSVC",
            "user": "hr_reader",
            "table_name": "VIHBUY",
            "unit_admin_titles": [],
            "declass_titles": [],
        },
    )
    monkeypatch.setattr(hr_settings, "enforce_oracle_host", original_enforce)
    assert incomplete.status_code == 400, incomplete.text
    assert "密碼" in incomplete.text

    bad_table = "nope;drop"
    rejected = client.put(
        SETTINGS_URL,
        headers=headers,
        json={
            "enabled": False,
            "host": "",
            "table_name": bad_table,
            "unit_admin_titles": [],
            "declass_titles": [],
        },
    )
    assert rejected.status_code == 400, rejected.text
    assert bad_table not in rejected.text
    assert "資料表名稱" in rejected.text

    _invalidate_cache()
    monkeypatch.delenv("ANILA_ALLOW_PRIVATE_ENDPOINT", raising=False)
    monkeypatch.delenv("ANILA_TRUSTED_HOSTS", raising=False)
    switched_off = client.put(
        SETTINGS_URL,
        headers=headers,
        json={
            "enabled": False,
            "host": "10.9.8.7",
            "port": 1521,
            "unit_admin_titles": [],
            "declass_titles": [],
        },
    )
    assert switched_off.status_code == 400, switched_off.text
    assert "信任主機" in switched_off.text
    monkeypatch.setenv("ANILA_ALLOW_PRIVATE_ENDPOINT", "1")
    _invalidate_cache()
    untrusted = client.put(
        SETTINGS_URL,
        headers=headers,
        json={
            "enabled": False,
            "host": "10.9.8.7",
            "port": 1521,
            "unit_admin_titles": [],
            "declass_titles": [],
        },
    )
    assert untrusted.status_code == 400, untrusted.text
    assert untrusted.json()["detail"]["code"] == "host_not_trusted"
    assert "信任主機" in untrusted.json()["detail"]["message"]

    saved = client.put(
        SETTINGS_URL,
        headers=headers,
        json={
            "enabled": False,
            "host": "",
            "port": 1521,
            "service_name": "",
            "user": "",
            "table_name": "",
            "password": SECRET,
            "root_unit_name": "院本部",
            "auto_unit_admin": False,
            "auto_declass": True,
            "unit_admin_titles": ["  專員  "],
            "declass_titles": [],
        },
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["has_password"] is True
    assert saved.json()["root_unit_name"] == "院本部"
    assert saved.json()["auto_unit_admin"] is False
    assert saved.json()["auto_declass"] is True
    assert saved.json()["unit_admin_titles"] == ["專員"]
    assert saved.json()["declass_titles"] == []
    assert "password" not in saved.json()
    assert SECRET not in saved.text
    envelope = db.get(HrOracleSettings, 1).password_envelope
    assert open_external_credential(envelope) == SECRET

    kept = client.put(
        SETTINGS_URL,
        headers=headers,
        json={
            "enabled": False,
            "host": "",
            "unit_admin_titles": [],
            "declass_titles": [],
        },
    )
    assert kept.status_code == 200, kept.text
    assert kept.json()["has_password"] is True
    assert open_external_credential(db.get(HrOracleSettings, 1).password_envelope) == SECRET

    cleared = client.put(
        SETTINGS_URL,
        headers=headers,
        json={
            "enabled": False,
            "host": "",
            "password": "",
            "unit_admin_titles": [],
            "declass_titles": [],
        },
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["has_password"] is False
    assert db.get(HrOracleSettings, 1).password_envelope is None
    details = " ".join(row.detail or "" for row in db.query(AuditLog).all())
    assert "password=set" in details
    assert "password=unchanged" in details
    assert "password=cleared" in details
    _assert_secret_absent(db, saved.text, kept.text, cleared.text, caplog.text)

    # 測試連線只吃已存的設定。密碼先存回去，主機用私有 IP，信任檢查在送出前擋下。
    stored = db.get(HrOracleSettings, 1)
    stored.enabled = True
    stored.host = "10.9.8.7"
    stored.port = 1521
    stored.service_name = "HRSVC"
    stored.db_user = "hr_reader"
    stored.table_name = "VIHBUY"
    stored.password_envelope = encrypt_external_credential(SECRET)
    db.commit()
    blocked = client.post(
        f"{SETTINGS_URL}/test",
        headers=headers,
        json={"employee_no": "9101801"},
    )
    assert blocked.status_code == 200, blocked.text
    assert blocked.json()["ok"] is False
    assert blocked.json()["error_type"] == "untrusted_host"
    assert "信任主機" in blocked.json()["message"]
    assert SECRET not in blocked.text

    monkeypatch.setattr(
        "app.services.hr_oracle_settings.lookup_staff",
        lambda employee_no, connection: _record(employee_no, email="hidden@example.invalid"),
    )
    monkeypatch.setattr(
        "app.services.hr_lookup.enforce_oracle_host",
        lambda *args, **kwargs: None,
    )
    # 上面那個假的沒有走到 enforce。改測查到人時的回應形狀。
    stored.host = "203.0.113.50"
    db.commit()
    probed = client.post(
        f"{SETTINGS_URL}/test",
        headers=headers,
        json={"employee_no": "9101801"},
    )
    assert probed.status_code == 200, probed.text
    assert probed.json()["found"] is True
    assert probed.json()["name"] == "人資姓名"
    assert probed.json()["titles"] == ["組長"]
    assert "email" not in probed.json()
    assert "hidden@example.invalid" not in probed.text
    viewed = client.get(SETTINGS_URL, headers=admin_headers)
    assert viewed.json()["health_status"] == "healthy"
    assert str(viewed.json()["health_checked_at"]).endswith("Z")

    # cookie 沒帶 CSRF 要擋。Bearer 不用。
    cookie_user = make_user(db, username="hr-cookie-admin", role="admin")
    login(client, cookie_user.username)
    no_csrf = client.put(
        SETTINGS_URL,
        json={
            "enabled": False,
            "host": "",
            "password": "",
            "unit_admin_titles": [],
            "declass_titles": [],
        },
    )
    assert no_csrf.status_code == 403
    assert "CSRF" in no_csrf.text
    with_csrf = client.put(
        SETTINGS_URL,
        headers={"X-CSRF-Token": client.cookies.get("anila_csrf")},
        json={
            "enabled": False,
            "host": "",
            "unit_admin_titles": [],
            "declass_titles": [],
        },
    )
    assert with_csrf.status_code == 403, with_csrf.text
    assert "需要 owner 權限" in with_csrf.text
    bearer = client.put(
        SETTINGS_URL,
        headers=headers,
        json={
            "enabled": False,
            "host": "",
            "unit_admin_titles": [],
            "declass_titles": [],
        },
    )
    assert bearer.status_code == 200, bearer.text
    _assert_secret_absent(db, blocked.text, probed.text, viewed.text, caplog.text)


def test_test_button_failure_names_the_oracle_code_without_the_password(
    client, db, monkeypatch, caplog
):
    caplog.set_level(logging.WARNING)
    admin_headers = _bearer(client, db, username="hr-probe-admin", role="admin")
    headers = _bearer(client, db, username="hr-probe-owner", role="owner")
    _enable(db, host="10.8.8.8", password=SECRET)
    admin_probe = client.post(
        f"{SETTINGS_URL}/test",
        headers=admin_headers,
        json={"employee_no": "9101802"},
    )
    assert admin_probe.status_code == 403, admin_probe.text
    assert "需要 owner 權限" in admin_probe.text
    monkeypatch.setattr(
        "app.services.hr_lookup.enforce_oracle_host",
        lambda *args, **kwargs: None,
    )

    class _Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            self.rfile.read(length)
            payload = {
                "ok": False,
                "error_type": "connect_failed",
                "oracle_code": "ORA-12541",
                "message": f"沒有 listener {SECRET} ORA-12541",
            }
            raw = json.dumps(payload).encode()
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, fmt, *args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(
        "app.services.hr_lookup.HR_LOOKUP_URL",
        f"http://127.0.0.1:{server.server_address[1]}/lookup",
    )
    try:
        response = client.post(
            f"{SETTINGS_URL}/test",
            headers=headers,
            json={"employee_no": "9101802"},
        )
    finally:
        server.shutdown()
        server.server_close()
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is False
    assert body["error_type"] == "connect_failed"
    assert body["oracle_code"] == "ORA-12541"
    assert "ORA-12541" in body["message"]
    assert "connect_failed" in body["message"]
    _assert_secret_absent(db, response.text, caplog.text)
    db.expire_all()
    health = db.get(HrOracleSettings, 1)
    assert health.health_status == "unhealthy"
    assert SECRET not in (health.health_detail or "")


def test_migration_upgrade_from_r1_0069_adds_source_and_settings():
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    import migrations.versions.r1_0070_hr_oracle as revision

    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE users ("
            "id INTEGER PRIMARY KEY, username VARCHAR(100) NOT NULL, "
            "hashed_password VARCHAR(255) NOT NULL)"
        ))
        conn.execute(text(
            "INSERT INTO users (id, username, hashed_password) VALUES (1, '9101901', 'x')"
        ))
        conn.execute(text(
            "CREATE TABLE unit_admin_assignments ("
            "id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, "
            "department_id INTEGER NOT NULL)"
        ))
        conn.execute(text(
            "INSERT INTO unit_admin_assignments (id, user_id, department_id) "
            "VALUES (7, 1, 3)"
        ))
        conn.execute(text(
            "CREATE TABLE classification_authority_assignments ("
            "id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, "
            "authority_reference VARCHAR(255) NOT NULL, is_active BOOLEAN NOT NULL)"
        ))
        conn.execute(text(
            "INSERT INTO classification_authority_assignments "
            "(id, user_id, authority_reference, is_active) VALUES (8, 1, '手登', 1)"
        ))
    with engine.begin() as conn:
        context = MigrationContext.configure(conn)
        with Operations.context(context):
            revision.upgrade()
    with engine.begin() as conn:
        user_columns = {row[1] for row in conn.execute(text("PRAGMA table_info(users)"))}
        assert {"display_name", "department_source", "hr_titles"} <= user_columns
        display_name, department_source, hr_titles = conn.execute(text(
            "SELECT display_name, department_source, hr_titles FROM users WHERE id = 1"
        )).one()
        assert display_name is None
        assert department_source is None
        assert hr_titles is None
        unit_source = conn.execute(text(
            "SELECT source FROM unit_admin_assignments WHERE id = 7"
        )).scalar()
        declass_source = conn.execute(text(
            "SELECT source FROM classification_authority_assignments WHERE id = 8"
        )).scalar()
        assert unit_source == "manual"
        assert declass_source == "manual"
        conn.execute(text(
            "INSERT INTO hr_oracle_settings (id, updated_at) "
            "VALUES (1, '2026-10-07 00:00:00')"
        ))
        titles = conn.execute(text(
            "SELECT unit_admin_titles, declass_titles FROM hr_oracle_settings WHERE id = 1"
        )).one()
        assert json.loads(titles[0]) == []
        assert json.loads(titles[1]) == []


def test_r1_0072_turns_title_switches_on_and_clears_lists():
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    import migrations.versions.r1_0070_hr_oracle as rev70
    import migrations.versions.r1_0071_hr_root_and_lookup_order as rev71
    import migrations.versions.r1_0072_hr_title_switches as rev72

    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE users ("
            "id INTEGER PRIMARY KEY, username VARCHAR(100) NOT NULL, "
            "hashed_password VARCHAR(255) NOT NULL)"
        ))
        conn.execute(text(
            "INSERT INTO users (id, username, hashed_password) VALUES (1, '9101902', 'x')"
        ))
        conn.execute(text(
            "CREATE TABLE unit_admin_assignments ("
            "id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, "
            "department_id INTEGER NOT NULL)"
        ))
        conn.execute(text(
            "CREATE TABLE classification_authority_assignments ("
            "id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, "
            "authority_reference VARCHAR(255) NOT NULL, is_active BOOLEAN NOT NULL)"
        ))

    def _run(revision, direction="upgrade"):
        with engine.begin() as conn:
            context = MigrationContext.configure(conn)
            with Operations.context(context):
                getattr(revision, direction)()

    _run(rev70)
    _run(rev71)
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO hr_oracle_settings "
            "(id, updated_at, unit_admin_titles, declass_titles) "
            "VALUES (1, '2026-10-07 00:00:00', '[\"專員\"]', '[\"科長\"]')"
        ))
    _run(rev72)
    with engine.begin() as conn:
        flags = conn.execute(text(
            "SELECT auto_unit_admin, auto_declass, unit_admin_titles, declass_titles "
            "FROM hr_oracle_settings WHERE id = 1"
        )).one()
        assert flags[0] in (1, True)
        assert flags[1] in (1, True)
        assert json.loads(flags[2]) == []
        assert json.loads(flags[3]) == []
        schema = conn.execute(text(
            "SELECT sql FROM sqlite_master WHERE name = 'hr_oracle_settings'"
        )).scalar()
        assert "組長" not in (schema or "")
    _run(rev72, "downgrade")
    with engine.begin() as conn:
        columns = {item[1] for item in conn.execute(text("PRAGMA table_info(hr_oracle_settings)"))}
        assert "auto_unit_admin" not in columns
        assert "auto_declass" not in columns
        titles = conn.execute(text(
            "SELECT unit_admin_titles, declass_titles FROM hr_oracle_settings WHERE id = 1"
        )).one()
        assert json.loads(titles[0]) == []
        assert json.loads(titles[1]) == []


def _seed_hr_holder(db, username, department):
    """這個人已經有人資授與的兩種列，另外各有一筆手授與。

    同一個單位不能同時有兩筆有效的單位管理員，所以人資那筆掛在子節點。
    """
    side = _dept(db, f"人資{username}", department.id)
    person = make_user(db, username=username, is_approved=True, department_id=department.id)
    person.display_name = "原姓名"
    person.department_source = "manual"
    db.commit()
    hand_unit = _grant_unit(db, person, department, source="manual")
    hr_unit = _grant_unit(db, person, side, source="hr")
    hand_declass = _grant_declass(db, person, source="manual", reference="總字第1號")
    hr_declass = _grant_declass(
        db, person, source="hr", department_id=department.id, reference="人資職稱",
    )
    return person, hand_unit, hr_unit, hand_declass, hr_declass


def test_two_first_level_units_on_an_empty_tree_are_siblings(
    client, db, monkeypatch, card_on
):
    _enable(db)
    produced = {
        "9102001": _record(
            "9102001", name="甲員", email="a-unit@example.invalid",
            dept1="甲所", dept2="甲所", titles=("組長",),
        ),
        "9102002": _record(
            "9102002", name="乙員", email="b-unit@example.invalid",
            dept1="乙所", dept2="乙所", titles=("組長",),
        ),
    }
    _install_lookup(monkeypatch, lambda employee_no, connection: produced[employee_no])
    first = make_user(db, username="9102001", is_approved=True)
    second = make_user(db, username="9102002", is_approved=True)
    assert _swipe(client, monkeypatch, first).status_code == 200
    assert _swipe(client, monkeypatch, second).status_code == 200
    roots = (
        db.query(Department)
        .filter(Department.parent_id.is_(None), Department.is_active.is_(True))
        .all()
    )
    assert [row.name for row in roots] == ["國家中山科學研究院"]
    unit_a = db.query(Department).filter(Department.name == "甲所").one()
    unit_b = db.query(Department).filter(Department.name == "乙所").one()
    assert unit_a.parent_id == roots[0].id
    assert unit_b.parent_id == roots[0].id
    scope_a = get_unit_admin_scope_ids(db, _reload(db, first))
    scope_b = get_unit_admin_scope_ids(db, _reload(db, second))
    assert unit_a.id in scope_a and unit_b.id not in scope_a
    assert unit_b.id in scope_b and unit_a.id not in scope_b


def test_dept1_equal_to_the_root_name_sits_on_that_root(client, db, monkeypatch, card_on):
    _enable(db)
    _install_lookup(
        monkeypatch,
        lambda employee_no, connection: _record(
            employee_no,
            name="院裡的人",
            email="on-root@example.invalid",
            dept1="國家中山科學研究院",
            dept2="國家中山科學研究院",
            titles=("組長",),
        ),
    )
    person = make_user(db, username="9102011", is_approved=True)
    assert _swipe(client, monkeypatch, person).status_code == 200
    rows = db.query(Department).all()
    assert len(rows) == 1
    assert rows[0].name == "國家中山科學研究院"
    assert rows[0].parent_id is None
    assert _reload(db, person).department_id == rows[0].id

    _install_lookup(
        monkeypatch,
        lambda employee_no, connection: _record(
            employee_no,
            name="組裡的人",
            email="under-root@example.invalid",
            dept1="國家中山科學研究院",
            dept2="人工智慧組",
            titles=("組長",),
        ),
    )
    other = make_user(db, username="9102012", is_approved=True)
    assert _swipe(client, monkeypatch, other).status_code == 200
    group = db.query(Department).filter(Department.name == "人工智慧組").one()
    assert group.parent_id == rows[0].id
    assert _reload(db, other).department_id == group.id
    assert db.query(Department).filter(Department.parent_id.is_(None)).count() == 1
    assert db.query(Department).filter(Department.name == "國家中山科學研究院").count() == 1


def test_several_roots_use_the_named_one_or_fail_and_the_settings_card_says_why(
    client, db, monkeypatch, card_on
):
    _enable(db)
    named = _dept(db, "國家中山科學研究院")
    other = _dept(db, "另一院")
    _install_lookup(
        monkeypatch,
        lambda employee_no, connection: _record(
            employee_no, name="掛對的人", email="matched@example.invalid",
            dept1="甲所", dept2="甲所", titles=("工程師",),
        ),
    )
    placed = make_user(db, username="9102021", is_approved=True)
    assert _swipe(client, monkeypatch, placed).status_code == 200
    unit = db.query(Department).filter(Department.name == "甲所").one()
    assert unit.parent_id == named.id
    headers = _bearer(client, db, username="hr-root-note-admin")
    matched_view = client.get(SETTINGS_URL, headers=headers)
    assert matched_view.status_code == 200, matched_view.text
    assert matched_view.json()["placement_note"] is None

    named.is_active = False
    db.commit()
    _dept(db, "第三院")
    home = other
    stayed, hand_unit, hr_unit, hand_declass, hr_declass = _seed_hr_holder(
        db, "9102022", home,
    )
    _install_lookup(
        monkeypatch,
        lambda employee_no, connection: _record(
            employee_no, name="對不到", email="miss@example.invalid",
            dept1="新的所", dept2="新的所", titles=("組長",),
        ),
    )
    assert _swipe(client, monkeypatch, stayed).status_code == 200
    stored = _reload(db, stayed)
    assert stored.department_id == home.id
    assert stored.department_source == "manual"
    unresolved = _audits(db, stayed, "hr_unit_unresolved")
    assert unresolved
    assert "多個最上層單位" in unresolved[-1].detail
    assert "根單位名稱" in unresolved[-1].detail
    assert "停用" in unresolved[-1].detail
    active_units = _active_unit(db, stayed)
    assert {row.id for row in active_units} == {hand_unit.id}
    assert hr_unit.id not in {row.id for row in active_units}
    declass = _active_declass(db, stayed)
    assert {row.id for row in declass} == {hand_declass.id, hr_declass.id}
    assert any(row.source == "hr" and row.department_id == home.id for row in declass)
    note = client.get(SETTINGS_URL, headers=headers).json()["placement_note"]
    assert note
    assert "另一院" in note and "第三院" in note
    assert "根單位名稱" in note
    assert "停用" in note


def test_authoritative_miss_revokes_hr_grants_already_held(client, db, monkeypatch, card_on):
    from datetime import datetime, timezone

    from app.services.hr_login import _as_utc

    _enable(db)
    home = _dept(db, "原單位")
    person, hand_unit, hr_unit, hand_declass, hr_declass = _seed_hr_holder(db, "9102031", home)
    previous = datetime(2026, 1, 1, tzinfo=timezone.utc)
    person.hr_lookup_started_at = previous
    db.commit()
    _install_lookup(monkeypatch, lambda employee_no, connection: None)
    response = _swipe(client, monkeypatch, person, name="卡片姓名")
    assert response.status_code == 200, response.text
    stored = _reload(db, person)
    assert stored.display_name == "原姓名"
    assert stored.department_id == home.id
    assert stored.department_source == "manual"
    assert _as_utc(stored.hr_lookup_started_at) > previous
    assert _audits(db, person, "hr_lookup_unavailable") == []
    assert {row.id for row in _active_unit(db, person)} == {hand_unit.id}
    assert {row.id for row in _active_declass(db, person)} == {hand_declass.id}
    db.expire_all()
    assert db.get(UnitAdminAssignment, hr_unit.id).revoked_at is not None
    assert db.get(ClassificationAuthorityAssignment, hr_declass.id).is_active is False


def test_placement_failure_with_no_title_revokes_both_hr_grants(client, db, monkeypatch, card_on):
    _enable(db)
    home = _dept(db, "原單位")
    _dept(db, "另一個根")
    person, hand_unit, hr_unit, hand_declass, hr_declass = _seed_hr_holder(db, "9102041", home)
    _install_lookup(
        monkeypatch,
        lambda employee_no, connection: _record(
            employee_no, name="職稱沒了", email="gone@example.invalid",
            dept1="新的所", dept2="新的組", titles=(),
        ),
    )
    assert _swipe(client, monkeypatch, person).status_code == 200
    stored = _reload(db, person)
    assert stored.department_id == home.id
    assert stored.display_name == "職稱沒了"
    assert not stored.hr_titles
    assert {row.id for row in _active_unit(db, person)} == {hand_unit.id}
    assert {row.id for row in _active_declass(db, person)} == {hand_declass.id}
    db.expire_all()
    assert db.get(UnitAdminAssignment, hr_unit.id).revoked_at is not None
    assert db.get(ClassificationAuthorityAssignment, hr_declass.id).revoked_at is not None


def test_unavailable_lookup_leaves_existing_hr_grants(client, db, monkeypatch, card_on):
    from datetime import datetime, timezone

    _enable(db)
    home = _dept(db, "原單位")
    started = datetime(2026, 2, 2, tzinfo=timezone.utc)
    person, hand_unit, hr_unit, hand_declass, hr_declass = _seed_hr_holder(db, "9102051", home)
    person.hr_lookup_started_at = started
    db.commit()

    def _produce(employee_no, connection):
        if employee_no == "9102051":
            raise HrUnavailable("connect_failed", f"連不上 {SECRET}", "ORA-12541")
        raise AssertionError(employee_no)

    _install_lookup(monkeypatch, _produce)
    assert _swipe(client, monkeypatch, person).status_code == 200
    db.expire_all()
    stored = db.get(User, person.id)
    from app.services.hr_login import _as_utc
    assert stored.display_name == "原姓名"
    assert _as_utc(stored.hr_lookup_started_at) == started
    assert {row.id for row in _active_unit(db, person)} == {hand_unit.id, hr_unit.id}
    assert {row.id for row in _active_declass(db, person)} == {hand_declass.id, hr_declass.id}
    assert _audits(db, person, "hr_lookup_unavailable")

    clash, _, clash_hr_unit, _, clash_hr_declass = _seed_hr_holder(db, "9102052", home)

    def _disagree(employee_no, connection):
        return staff_from_rows(
            employee_no,
            [
                {
                    "ovc_PNO": employee_no,
                    "ovc_NAME": "甲",
                    "ovc_DEPT1_NAME": "資訊通信研究所",
                    "ovc_DEPT2_NAME": "人工智慧組",
                    "ovc_EMAIL": "a@example.invalid",
                    "ovc_DUTY_DS": "組長",
                },
                {
                    "ovc_PNO": employee_no,
                    "ovc_NAME": "乙",
                    "ovc_DEPT1_NAME": "資訊通信研究所",
                    "ovc_DEPT2_NAME": "人工智慧組",
                    "ovc_EMAIL": "b@example.invalid",
                    "ovc_DUTY_DS": "副組長",
                },
            ],
        )

    _install_lookup(monkeypatch, _disagree)
    assert _swipe(client, monkeypatch, clash).status_code == 200
    assert _reload(db, clash).display_name == "原姓名"
    assert _reload(db, clash).hr_lookup_started_at is None
    assert {row.id for row in _active_unit(db, clash)} >= {clash_hr_unit.id}
    assert any(row.id == clash_hr_declass.id for row in _active_declass(db, clash))
    assert any("row_disagreement" in row.detail for row in _audits(db, clash, "hr_lookup_unavailable"))


def test_hr_switched_off_leaves_existing_hr_grants(client, db, monkeypatch, card_on):
    home = _dept(db, "原單位")
    person, hand_unit, hr_unit, hand_declass, hr_declass = _seed_hr_holder(db, "9102061", home)
    _enable(db, enabled=False)
    seen = _install_lookup(
        monkeypatch,
        lambda employee_no, connection: (_ for _ in ()).throw(AssertionError("不該查")),
    )
    assert _swipe(client, monkeypatch, person).status_code == 200
    assert seen["calls"] == 0
    stored = _reload(db, person)
    assert stored.display_name == "原姓名"
    assert stored.hr_lookup_started_at is None
    assert {row.id for row in _active_unit(db, person)} == {hand_unit.id, hr_unit.id}
    assert {row.id for row in _active_declass(db, person)} == {hand_declass.id, hr_declass.id}


def test_an_earlier_lookup_does_not_overwrite_a_later_one(db):
    from datetime import datetime, timedelta, timezone

    from app.services.hr_login import _apply_fetched, _as_utc

    person = make_user(db, username="9102071", is_approved=True)
    later = datetime(2026, 10, 7, 3, 0, tzinfo=timezone.utc)
    earlier = later - timedelta(minutes=5)
    _apply_fetched(
        db,
        person.id,
        _record(
            "9102071", name="新名", email="new-order@example.invalid",
            dept1="乙所", dept2="乙所", titles=(),
        ),
        later,
        card_name="卡片不該覆蓋",
        auto_unit_admin=True,
        unit_admin_titles=("組長",),
        auto_declass=True,
        declass_titles=("組長",),
        root_name="國家中山科學研究院",
        ip_address=None,
    )
    stored = _reload(db, person)
    assert stored.display_name == "新名"
    second = db.query(Department).filter(Department.name == "乙所").one()
    assert stored.department_id == second.id
    assert _active_declass(db, person) == []
    _apply_fetched(
        db,
        person.id,
        _record(
            "9102071", name="舊名", email="old-order@example.invalid",
            dept1="甲所", dept2="甲所", titles=("組長",),
        ),
        earlier,
        card_name=None,
        auto_unit_admin=True,
        unit_admin_titles=("組長",),
        auto_declass=True,
        declass_titles=("組長",),
        root_name="國家中山科學研究院",
        ip_address=None,
    )
    stored = _reload(db, person)
    assert stored.display_name == "新名"
    assert stored.email == "new-order@example.invalid"
    assert stored.department_id == second.id
    assert _as_utc(stored.hr_lookup_started_at) == later
    assert db.query(Department).filter(Department.name == "甲所").count() == 0
    assert _active_unit(db, person) == []
    assert _active_declass(db, person) == []
    newest = later + timedelta(minutes=5)
    _apply_fetched(
        db,
        person.id,
        _record(
            "9102071", name="更新", email="newest@example.invalid",
            dept1="丙所", dept2="丙所", titles=("組長",),
        ),
        newest,
        card_name=None,
        auto_unit_admin=True,
        unit_admin_titles=("組長",),
        auto_declass=True,
        declass_titles=("組長",),
        root_name="國家中山科學研究院",
        ip_address=None,
    )
    stored = _reload(db, person)
    assert stored.display_name == "更新"
    assert stored.department_id == db.query(Department).filter(Department.name == "丙所").one().id
    assert _as_utc(stored.hr_lookup_started_at) == newest
    assert _active_declass(db, person)


def _pending_declass(db, username="hr-declass-admin"):
    from app.models.conversation import Conversation
    from app.modules.policy import apply_classification, create_declassification_request

    admin = make_user(db, username=username, role="admin")
    conv = Conversation(user_id=admin.id, title="降密測試")
    db.add(conv)
    db.commit()
    db.refresh(conv)
    apply_classification(
        db,
        resource_type="conversation",
        resource_id=str(conv.id),
        new_level="機密",
        actor_type="user",
        actor_id=str(admin.id),
        reason="manual_admin",
    )
    request = create_declassification_request(
        db,
        resource_type="conversation",
        resource_id=str(conv.id),
        to_level="無機密",
        requested_by_admin_id=admin.id,
        reason="專案結案，內容已完成降密審查",
    )
    return admin, conv, request


def test_approval_recheck_follows_the_title_switches(db, monkeypatch):
    from app.modules.policy import decide_declassification

    def _decide(approver_id, request_id):
        return decide_declassification(
            db,
            request_id=request_id,
            approver_user_id=approver_id,
            approve=True,
            via="in_system",
        )

    def _lookup(titles):
        _install_lookup(
            monkeypatch,
            lambda employee_no, connection: StaffRecord(
                employee_no, "主管", None, None, None, tuple(titles),
            ),
        )

    _enable(db)
    _admin, _conv, request = _pending_declass(db, username="hr-any-title-admin")
    approver = make_user(db, username="9102301", role="user")
    granted = _grant_declass(db, approver, source="hr", reference="人資職稱")
    _lookup(("專員",))
    assert _decide(approver.id, request.id).status == "applied"
    db.expire_all()
    assert db.get(ClassificationAuthorityAssignment, granted.id).is_active is True

    _enable(db, declass_titles=["  組長  "])
    _admin, conv, request = _pending_declass(db, username="hr-narrow-miss-admin")
    approver = make_user(db, username="9102302", role="user")
    granted = _grant_declass(db, approver, source="hr", reference="人資職稱")
    _lookup(("專員",))
    assert _decide(approver.id, request.id).status == "pending_supervisor"
    db.expire_all()
    assert db.get(type(conv), conv.id).classification_level == "機密"
    assert db.get(ClassificationAuthorityAssignment, granted.id).is_active is False

    _enable(db, auto_declass=False)
    _admin, conv, request = _pending_declass(db, username="hr-declass-off-admin")
    approver = make_user(db, username="9102303", role="user")
    granted = _grant_declass(db, approver, source="hr", reference="人資職稱")
    _lookup(("專員",))
    assert _decide(approver.id, request.id).status == "pending_supervisor"
    db.expire_all()
    assert db.get(type(conv), conv.id).classification_level == "機密"
    assert db.get(ClassificationAuthorityAssignment, granted.id).is_active is False

    _enable(db, declass_titles=["  組長  "], auto_unit_admin=False)
    _admin, _conv, request = _pending_declass(db, username="hr-narrow-hit-admin")
    approver = make_user(db, username="9102304", role="user")
    granted = _grant_declass(db, approver, source="hr", reference="人資職稱")
    _lookup(("組長",))
    assert _decide(approver.id, request.id).status == "applied"
    db.expire_all()
    assert db.get(ClassificationAuthorityAssignment, granted.id).is_active is True


def test_hr_authority_still_justified_can_approve(db, monkeypatch):
    from app.modules.policy import decide_declassification

    _enable(db)
    admin, conv, request = _pending_declass(db)
    _grant_declass(db, admin, source="hr", reference="人資職稱")
    approver = make_user(db, username="9102081", role="user")
    _grant_declass(db, approver, source="hr", reference="人資職稱")
    seen = _install_lookup(
        monkeypatch,
        lambda employee_no, connection: StaffRecord(
            employee_no, "主管", None, None, None, ("組長",),
        ),
    )
    with pytest.raises(ValueError, match="申請人"):
        decide_declassification(
            db,
            request_id=request.id,
            approver_user_id=admin.id,
            approve=True,
            via="in_system",
        )
    assert seen["calls"] == 0
    result = decide_declassification(
        db,
        request_id=request.id,
        approver_user_id=approver.id,
        approve=True,
        via="in_system",
    )
    assert seen["calls"] == 1
    assert seen["released"] is True
    assert result.status == "applied"
    db.expire_all()
    assert db.get(type(conv), conv.id).classification_level == "無機密"
    assert any(row.source == "hr" and row.is_active for row in _active_declass(db, approver))


def test_hr_authority_title_gone_is_refused_like_no_authority(db, monkeypatch):
    from app.modules.policy import decide_declassification

    _enable(db)
    _admin, conv, request = _pending_declass(db, username="hr-declass-admin-gone")
    approver = make_user(db, username="9102082", role="user")
    granted = _grant_declass(db, approver, source="hr", reference="人資職稱")
    _install_lookup(
        monkeypatch,
        lambda employee_no, connection: StaffRecord(
            employee_no, "已經不是主管", None, None, None, (),
        ),
    )
    result = decide_declassification(
        db,
        request_id=request.id,
        approver_user_id=approver.id,
        approve=True,
        via="in_system",
    )
    assert result.status == "pending_supervisor"
    db.expire_all()
    assert db.get(type(conv), conv.id).classification_level == "機密"
    assert db.get(ClassificationAuthorityAssignment, granted.id).is_active is False
    missing = (
        db.query(AuditLog)
        .filter(AuditLog.action == "supervisor_missing", AuditLog.resource_id == str(request.id))
        .all()
    )
    assert len(missing) == 1


def test_hr_down_refuses_an_hr_only_approver(db, monkeypatch):
    from app.modules.policy import decide_declassification

    _enable(db)
    _admin, conv, request = _pending_declass(db, username="hr-declass-admin-down")
    approver = make_user(db, username="9102083", role="user")
    granted = _grant_declass(db, approver, source="hr", reference="人資職稱")
    _install_lookup(
        monkeypatch,
        lambda employee_no, connection: (_ for _ in ()).throw(
            HrUnavailable("connect_failed", f"連不上 {SECRET}", "ORA-12541")
        ),
    )
    with pytest.raises(ValueError, match="人資資料庫連不上，無法確認核准人的職稱"):
        decide_declassification(
            db,
            request_id=request.id,
            approver_user_id=approver.id,
            approve=True,
            via="in_system",
        )
    db.expire_all()
    assert db.get(type(conv), conv.id).classification_level == "機密"
    assert db.get(DeclassificationRequest, request.id).status == "pending_supervisor"
    assert db.get(ClassificationAuthorityAssignment, granted.id).is_active is True
    audits = (
        db.query(AuditLog)
        .filter(AuditLog.action == "hr_lookup_unavailable")
        .all()
    )
    assert len(audits) == 1
    assert audits[0].resource_type == "declassification_request"
    assert audits[0].resource_id == str(request.id)
    assert "人資資料庫連不上，無法確認核准人的職稱" in audits[0].detail
    assert "connect_failed" in audits[0].detail
    assert SECRET not in (audits[0].detail or "")
    assert "ORA-12541" not in (audits[0].detail or "")


def test_hand_granted_approver_is_not_blocked_when_hr_is_down(db, monkeypatch):
    from app.modules.policy import decide_declassification

    _enable(db)
    _admin, conv, request = _pending_declass(db, username="hr-declass-admin-hand")
    approver = make_user(db, username="9102084", role="user")
    hand = _grant_declass(db, approver, source="manual", reference="總字第9號")
    hr_row = _grant_declass(db, approver, source="hr", reference="人資職稱")
    seen = _install_lookup(
        monkeypatch,
        lambda employee_no, connection: (_ for _ in ()).throw(
            HrUnavailable("connect_failed", f"連不上 {SECRET}")
        ),
    )
    result = decide_declassification(
        db,
        request_id=request.id,
        approver_user_id=approver.id,
        approve=True,
        via="in_system",
    )
    assert seen["calls"] == 0
    assert result.status == "applied"
    db.expire_all()
    assert db.get(type(conv), conv.id).classification_level == "無機密"
    assert db.get(ClassificationAuthorityAssignment, hand.id).is_active is True
    assert db.get(ClassificationAuthorityAssignment, hr_row.id).is_active is True


def _migration_engine():
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    import migrations.versions.r1_0070_hr_oracle as revision

    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE users ("
            "id INTEGER PRIMARY KEY, username VARCHAR(100) NOT NULL, "
            "hashed_password VARCHAR(255) NOT NULL)"
        ))
        conn.execute(text(
            "INSERT INTO users (id, username, hashed_password) VALUES (1, '9101901', 'x')"
        ))
        conn.execute(text(
            "CREATE TABLE unit_admin_assignments ("
            "id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, "
            "department_id INTEGER NOT NULL)"
        ))
        conn.execute(text(
            "INSERT INTO unit_admin_assignments (id, user_id, department_id) "
            "VALUES (7, 1, 3)"
        ))
        conn.execute(text(
            "CREATE TABLE classification_authority_assignments ("
            "id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, "
            "authority_reference VARCHAR(255) NOT NULL, is_active BOOLEAN NOT NULL)"
        ))
        conn.execute(text(
            "INSERT INTO classification_authority_assignments "
            "(id, user_id, authority_reference, is_active) VALUES (8, 1, '手登', 1)"
        ))
        context = MigrationContext.configure(conn)
        with Operations.context(context):
            revision.upgrade()
    return engine, revision


def _run_downgrade(engine, revision):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    with engine.begin() as conn:
        context = MigrationContext.configure(conn)
        with Operations.context(context):
            revision.downgrade()


def test_r1_0070_downgrade_refuses_while_hr_grants_or_settings_remain():
    engine, revision = _migration_engine()
    with engine.begin() as conn:
        conn.execute(text("UPDATE unit_admin_assignments SET source = 'hr' WHERE id = 7"))
    with pytest.raises(RuntimeError, match="不能降版"):
        _run_downgrade(engine, revision)
    with engine.begin() as conn:
        columns = {row[1] for row in conn.execute(text("PRAGMA table_info(unit_admin_assignments)"))}
        assert "source" in columns
        assert conn.execute(text(
            "SELECT source FROM unit_admin_assignments WHERE id = 7"
        )).scalar() == "hr"

    engine, revision = _migration_engine()
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO hr_oracle_settings (id, updated_at) "
            "VALUES (1, '2026-10-07 00:00:00')"
        ))
    with pytest.raises(RuntimeError, match="hr_oracle_settings"):
        _run_downgrade(engine, revision)
    with engine.begin() as conn:
        tables = {row[0] for row in conn.execute(text(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ))}
        assert "hr_oracle_settings" in tables


def test_r1_0070_downgrade_drops_columns_when_nothing_hr_remains():
    engine, revision = _migration_engine()
    _run_downgrade(engine, revision)
    with engine.begin() as conn:
        tables = {row[0] for row in conn.execute(text(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ))}
        assert "hr_oracle_settings" not in tables
        unit_columns = {row[1] for row in conn.execute(text("PRAGMA table_info(unit_admin_assignments)"))}
        user_columns = {row[1] for row in conn.execute(text("PRAGMA table_info(users)"))}
        assert "source" not in unit_columns
        assert "display_name" not in user_columns


def test_only_csp_and_hr_lookup_join_the_hr_oracle_network():
    from pathlib import Path

    import yaml

    repo = Path(__file__).resolve().parents[3]
    for relative in ("infra/compose/platform.yml", "infra/compose/dev.yml"):
        text_body = (repo / relative).read_text(encoding="utf-8")
        assert "只有 csp 與 hr-lookup 可以加入" in text_body
        doc = yaml.safe_load(text_body)
        joined = []
        for name, service in doc["services"].items():
            networks = service.get("networks") or []
            names = set(networks) if not isinstance(networks, dict) else set(networks)
            if "hr-oracle" in names:
                joined.append(name)
        assert sorted(joined) == ["csp", "hr-lookup"], relative


def test_hand_assignment_limit_counts_only_hand_assigned_admins(db):
    from fastapi import HTTPException

    from app.services import unit_admin_service

    owner = make_user(db, username="limit-owner", role="owner", is_approved=True)
    root = _dept(db, "院")
    group = _dept(db, "名額組", root.id)
    # 五位主管由人資帶入，都不占名額。
    for index in range(5):
        _grant_unit(db, make_user(db, username=f"92000{index}", is_approved=True), group, source="hr")

    for index in range(3):
        target = make_user(db, username=f"92001{index}", is_approved=True)
        row = unit_admin_service.assign(db, user=target, department=group, granted_by=owner)
        assert row.source == "manual"

    fourth = make_user(db, username="920019", is_approved=True)
    with pytest.raises(HTTPException) as refused:
        unit_admin_service.assign(db, user=fourth, department=group, granted_by=owner)
    assert refused.value.status_code == 400
    assert "最多 3 名" in refused.value.detail
