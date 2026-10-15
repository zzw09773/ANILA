"""管理員診斷面只公開固定本機能力，不接受出向或命令輸入。"""

import asyncio
import json

import httpx
import pytest
from fastapi import FastAPI

from app.api import quick_readiness
from app.database import get_db
from app.models.department import Department
from app.models.unit_admin_assignment import UnitAdminAssignment
from app.services.auth_service import get_current_user
from tests.conftest import make_user


@pytest.fixture
def app(db, monkeypatch):
    app = FastAPI()
    app.include_router(quick_readiness.router)
    app.dependency_overrides[get_db] = lambda: db
    calls = []

    def diagnose():
        calls.append(True)
        return {
            "schema_version": 1,
            "scope": "current_process",
            "status": "attention",
            "checked_at": "2026-10-15T00:00:00+00:00",
            "message": "僅本機檢查",
            "checks": [
                {
                    "id": "tesseract",
                    "label": "離線 OCR",
                    "status": "blocked",
                    "type": "offline_ocr_languages_missing",
                    "message": "缺少 chi_tra",
                    "action": "請管理員補齊經審核的離線語料。",
                    "missing": ["chi_tra"],
                }
            ],
            "formats": [],
            "not_checked": ["ingestion_worker", "remote_docling", "production_image"],
        }

    monkeypatch.setattr(quick_readiness, "diagnose_local_ingestion", diagnose)
    return app, calls


def request(app, url="/api/quick-builder/readiness", *, headers=None):
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            return await client.get(url, headers=headers)

    return asyncio.run(scenario())


def test_unauthenticated_readiness_never_runs_local_probes(app):
    application, calls = app
    assert request(application).status_code == 401
    assert calls == []


@pytest.mark.parametrize("unit_admin", [False, True])
def test_user_and_unit_reviewer_cannot_read_or_execute_diagnostics(app, db, unit_admin):
    application, calls = app
    department = Department(name="診斷權限合成單位")
    db.add(department)
    db.flush()
    user = make_user(db, "readiness-user", department_id=department.id)
    if unit_admin:
        db.add(UnitAdminAssignment(user_id=user.id, department_id=department.id))
        db.commit()
    application.dependency_overrides[get_current_user] = lambda: user
    assert request(application).status_code == 403
    assert calls == []


@pytest.mark.parametrize("role", ["admin", "owner"])
def test_existing_platform_admin_tiers_can_read_scoped_sanitized_report(app, db, role):
    application, calls = app
    user = make_user(db, "readiness-admin", role=role)
    application.dependency_overrides[get_current_user] = lambda: user
    response = request(application)
    assert response.status_code == 200
    report = response.json()
    assert report["service"] == "csp" and report["scope"] == "current_process"
    assert report["not_checked"] == [
        "ingestion_worker",
        "remote_docling",
        "production_image",
    ]
    assert report["checks"][0]["type"] == "offline_ocr_languages_missing"
    assert report["checks"][0]["missing"] == ["chi_tra"]
    assert "未檢查" in report["message"] and "CSP" in report["message"]
    assert calls == [True]


def test_no_query_can_supply_command_tool_path_or_remote_endpoint(app, db):
    application, calls = app
    user = make_user(db, "readiness-admin", role="admin")
    application.dependency_overrides[get_current_user] = lambda: user
    response = request(
        application,
        "/api/quick-builder/readiness?command=arbitrary&path=/private/path&url=https://private.invalid",
    )
    assert response.status_code == 422
    assert response.json()["detail"]["type"] == "READINESS_INPUT_UNSUPPORTED"
    assert "private" not in json.dumps(response.json())
    assert calls == []


@pytest.mark.parametrize("mode", ["json", "chunked", "misstated_length"])
def test_no_get_body_can_supply_configuration_or_commands(app, db, mode):
    application, calls = app
    user = make_user(db, "readiness-admin-body", role="admin")
    application.dependency_overrides[get_current_user] = lambda: user

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application), base_url="http://testserver"
        ) as client:
            kwargs = {
                "content": b'{"command":"private-command","path":"/private/path"}'
            }
            if mode == "chunked":
                kwargs["headers"] = {"transfer-encoding": "chunked"}
            elif mode == "misstated_length":
                kwargs["headers"] = {"content-length": "0"}
            return await client.request("GET", "/api/quick-builder/readiness", **kwargs)

    response = asyncio.run(scenario())
    assert response.status_code == 422
    assert response.json()["detail"]["type"] == "READINESS_INPUT_UNSUPPORTED"
    assert "private" not in response.text and calls == []


@pytest.mark.parametrize(
    "role,revoked,expected",
    [
        ("admin", False, 200),
        ("owner", False, 200),
        ("user", False, 403),
        ("admin", True, 401),
    ],
)
def test_http_uses_real_existing_jwt_identity_and_admin_gate(
    app, db, role, revoked, expected
):
    from app.utils.security import create_access_token

    application, calls = app
    user = make_user(db, "readiness-real-jwt", role=role)
    token = create_access_token({"sub": str(user.id), "tv": user.token_version}, db=db)
    if revoked:
        user.token_version += 1
        db.commit()
    response = request(application, headers={"Authorization": "Bearer " + token})
    assert response.status_code == expected
    assert calls == ([True] if expected == 200 else [])
