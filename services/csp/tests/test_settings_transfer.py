"""設定匯出不含金鑰；匯入可試算、套用，且同一份檔再匯入不會改資料。"""
from __future__ import annotations

import json
import threading

import pytest

from anila_core.security.url_guard import _host_is_trusted
from app.models.audit_log import AuditLog
from app.models.banner import Banner
from app.models.department import Department
from app.models.external_service import DOCUMENT_PARSER, ExternalService
from app.models.handoff import Notification
from app.models.user import User
from app.models.model_registry import ModelRegistry
from app.models.model_role import ModelRole
from app.models.router_model_grant import RouterModelGrant
from app.models.trusted_host import TrustedHost
from app.services.auto_seed import ensure_platform_router_model
from app.services.external_service_crypto import (
    encrypt_external_credential,
    open_external_credential,
)
from app.services.external_services import ensure_rows
from app.services.service_token_envelope import (
    decode_service_token_envelope,
    encode_service_token_envelope,
)
from tests.conftest import login, make_agent, make_api_key, make_user

USERINFO_SECRET = "s3cret-userinfo"
_MAX_IMPORT_BYTES = 2 * 1024 * 1024

MODEL_SECRET = "sk-model-export-leak-9f3a2c"
EXT_SECRET = "docling-export-leak-77bb"
USER_KEY = "sk-user-export-leak-abc123"
FORBIDDEN_KEYS = {
    "api_key",
    "api_key_secret_ref",
    "credential_envelope",
    "password",
    "secret",
    "token",
    "service_token",
}


def _headers(client, db, username, role="owner"):
    make_user(db, username=username, role=role)
    return {"Authorization": f"Bearer {login(client, username)}"}


def _walk_keys(value):
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from _walk_keys(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_keys(item)


def _sample_document():
    return {
        "schema_version": 1,
        "trusted_hosts": [{"host": "gemma4", "note": "演練模型"}],
        "models": [
            {
                "name": "gemma-import",
                "display_name": "Gemma",
                "model_type": "llm",
                "endpoint_url": "https://gemma4/v1",
                "protocol": "openai_compatible",
                "router_enabled": True,
                "max_concurrent": 4,
                "is_active": True,
                "is_internal": True,
                "credential": None,
                "credential_required": True,
            },
            {
                "name": "embed-import",
                "display_name": "Embed",
                "model_type": "embedding",
                "endpoint_url": "https://gemma4/v1",
                "protocol": "openai_compatible",
                "router_enabled": False,
                "max_concurrent": 2,
                "credential": None,
                "credential_required": False,
            },
        ],
        "model_roles": [
            {"role": "summary", "model_name": "gemma-import"},
            {"role": "router_primary", "model_name": "gemma-import"},
            {"role": "platform_embedding", "model_name": "embed-import"},
        ],
        "router_grants": [{"model_name": "gemma-import", "scope": "all"}],
        "external_services": [
            {
                "kind": "document_parser",
                "enabled": True,
                "base_url": "https://gemma4",
                "protocol": "native",
                "credential": None,
                "credential_required": True,
            }
        ],
        "departments": [
            {"path": ["資訊所"], "description": "演練", "is_active": True}
        ],
        "banners": [
            {
                "level": "info",
                "content": "演練公告",
                "show_on_login": False,
                "sort_order": 1,
            }
        ],
    }


def test_export_omits_secrets_and_redacts_addresses_for_plain_admin(client, db):
    owner_headers = _headers(client, db, "export-owner", role="owner")
    admin_headers = _headers(client, db, "export-admin", role="admin")
    ensure_platform_router_model(db)
    dept = Department(name="資訊所", description="演練", is_active=True)
    db.add(dept)
    db.flush()
    envelope = encode_service_token_envelope(MODEL_SECRET)
    model = ModelRegistry(
        name="gemma-export",
        display_name="Gemma",
        model_type="llm",
        endpoint_url="https://models.example.com/v1",
        protocol="openai_compatible",
        router_enabled=True,
        max_concurrent=4,
        is_active=True,
        is_internal=False,
        api_key_secret_ref=envelope,
        owner_department_id=dept.id,
    )
    db.add(model)
    db.flush()
    db.add(RouterModelGrant(model_id=model.id, scope_type="all", created_by=1))
    db.add(ModelRole(role="summary", model_id=model.id))
    ensure_rows(db)
    service = db.get(ExternalService, DOCUMENT_PARSER)
    ext_envelope = encrypt_external_credential(EXT_SECRET)
    service.enabled = True
    service.base_url = "https://docling.example.com"
    service.credential_envelope = ext_envelope
    db.add(Banner(level="info", content="維護公告", is_active=True, show_on_login=False, sort_order=0))
    db.add(TrustedHost(host="models.example.com", note="模型"))
    db.commit()
    user = db.query(User).filter(User.username == "export-owner").one()
    make_api_key(db, user, raw_key=USER_KEY)
    key_hash = user.api_keys[0].key_hash if user.api_keys else None

    exported = client.get("/api/admin/settings-export", headers=owner_headers)
    assert exported.status_code == 200, exported.text
    body = exported.json()
    blob = json.dumps(body, ensure_ascii=False)
    for secret in (MODEL_SECRET, EXT_SECRET, USER_KEY, envelope, ext_envelope):
        assert secret not in blob
    if key_hash:
        assert key_hash not in blob
    assert FORBIDDEN_KEYS.isdisjoint(set(_walk_keys(body)))
    assert "anila-router" not in {item["name"] for item in body["models"]}

    exported_model = next(item for item in body["models"] if item["name"] == "gemma-export")
    assert exported_model["endpoint_url"] == "https://models.example.com/v1"
    assert exported_model["endpoint_hidden"] is False
    assert exported_model["credential"] is None
    assert exported_model["credential_required"] is True
    assert exported_model["max_concurrent"] == 4
    assert exported_model["router_enabled"] is True
    assert exported_model["owner_department_path"] == ["資訊所"]
    assert {"role": "summary", "model_name": "gemma-export"} in body["model_roles"]
    assert {"model_name": "gemma-export", "scope": "all"} in body["router_grants"]
    parser_out = next(item for item in body["external_services"] if item["kind"] == "document_parser")
    assert parser_out["credential"] is None
    assert parser_out["credential_required"] is True
    assert parser_out["base_url"] == "https://docling.example.com"
    assert body["trusted_hosts"] == [{"host": "models.example.com", "note": "模型"}]
    assert {"path": ["資訊所"], "description": "演練", "is_active": True} in body["departments"]
    assert any(item["content"] == "維護公告" for item in body["banners"])

    hidden = client.get("/api/admin/settings-export", headers=admin_headers)
    assert hidden.status_code == 200, hidden.text
    hidden_blob = json.dumps(hidden.json(), ensure_ascii=False)
    assert "https://models.example.com/v1" not in hidden_blob
    assert MODEL_SECRET not in hidden_blob
    hidden_model = next(item for item in hidden.json()["models"] if item["name"] == "gemma-export")
    assert hidden_model["endpoint_url"] is None
    assert hidden_model["endpoint_hidden"] is True
    assert "<owner-only>" not in hidden_blob
    assert "<internal>" not in hidden_blob

    db.expire_all()
    assert db.query(AuditLog).filter(AuditLog.action == "settings_export").count() >= 1


def test_import_dry_run_apply_and_second_import_changes_nothing(client, db):
    headers = _headers(client, db, "import-owner", role="owner")
    document = _sample_document()
    preview = client.post(
        "/api/admin/settings-import",
        json={"dry_run": True, "document": document},
        headers=headers,
    )
    assert preview.status_code == 200, preview.text
    preview_body = preview.json()
    assert preview_body["dry_run"] is True
    assert preview_body["applied"] is False
    actions = {(item["entity"], item["key"]): item for item in preview_body["items"]}
    assert actions[("model", "gemma-import")]["action"] == "needs-credential"
    assert actions[("model", "gemma-import")]["message"] == "需另外填入金鑰"
    assert actions[("trusted_host", "gemma4")]["action"] == "create"
    assert actions[("department", "資訊所")]["action"] == "create"
    assert actions[("model_role", "summary")]["action"] == "create"
    assert actions[("model_role", "router_primary")]["action"] == "create"
    assert actions[("model_role", "platform_embedding")]["action"] == "skip"
    assert "連線量測" in actions[("model_role", "platform_embedding")]["message"]
    assert actions[("router_grant", "gemma-import")]["action"] == "create"
    assert actions[("external_service", "document_parser")]["message"] == "需另外填入金鑰"
    assert actions[("banner", "info:演練公告")]["action"] == "create"
    db.expire_all()
    assert db.query(ModelRegistry).filter(ModelRegistry.name == "gemma-import").count() == 0
    assert db.query(TrustedHost).filter(TrustedHost.host == "gemma4").count() == 0

    applied = client.post(
        "/api/admin/settings-import",
        json={"dry_run": False, "document": document},
        headers=headers,
    )
    assert applied.status_code == 200, applied.text
    assert applied.json()["applied"] is True
    db.expire_all()
    model = db.query(ModelRegistry).filter(ModelRegistry.name == "gemma-import").one()
    embed = db.query(ModelRegistry).filter(ModelRegistry.name == "embed-import").one()
    assert model.api_key_secret_ref is None
    assert model.endpoint_url == "https://gemma4/v1"
    assert model.router_enabled is True
    assert model.max_concurrent == 4
    assert model.is_router_primary is True
    assert embed.is_platform_embedding is False
    assert db.query(TrustedHost).filter(TrustedHost.host == "gemma4").one().note == "演練模型"
    assert db.query(Department).filter(Department.name == "資訊所").one().description == "演練"
    role = db.get(ModelRole, "summary")
    assert role is not None and role.model_id == model.id
    grant = (
        db.query(RouterModelGrant)
        .filter(RouterModelGrant.model_id == model.id, RouterModelGrant.scope_type == "all")
        .one()
    )
    assert grant.expires_at is None
    from app.models.external_service import ExternalService

    service = db.get(ExternalService, DOCUMENT_PARSER)
    assert service.enabled is True
    assert service.base_url == "https://gemma4"
    assert service.credential_envelope is None
    assert db.query(Banner).filter(Banner.content == "演練公告").one().is_active is True
    model_updated = model.updated_at
    service_updated = service.updated_at
    host_id = db.query(TrustedHost).filter(TrustedHost.host == "gemma4").one().id

    again = client.post(
        "/api/admin/settings-import",
        json={"dry_run": False, "document": document},
        headers=headers,
    )
    assert again.status_code == 200, again.text
    assert all(item["action"] in {"skip", "needs-credential"} for item in again.json()["items"])
    db.expire_all()
    model = db.query(ModelRegistry).filter(ModelRegistry.name == "gemma-import").one()
    service = db.get(ExternalService, DOCUMENT_PARSER)
    assert model.updated_at == model_updated
    assert service.updated_at == service_updated
    assert model.api_key_secret_ref is None
    assert service.credential_envelope is None
    assert db.query(ModelRegistry).filter(ModelRegistry.name == "gemma-import").count() == 1
    assert db.query(TrustedHost).filter(TrustedHost.host == "gemma4").one().id == host_id
    assert db.query(Department).filter(Department.name == "資訊所").count() == 1
    assert db.query(Banner).filter(Banner.content == "演練公告").count() == 1
    assert db.query(AuditLog).filter(AuditLog.action == "settings_import").count() >= 2


def test_import_keeps_existing_model_key(client, db):
    headers = _headers(client, db, "keep-key-owner", role="owner")
    envelope = encode_service_token_envelope(MODEL_SECRET)
    db.add(TrustedHost(host="gemma4", note="演練模型"))
    db.add(ModelRegistry(
        name="gemma-import",
        display_name="舊名",
        model_type="llm",
        endpoint_url="https://gemma4/v1",
        protocol="openai_compatible",
        api_version="v1",
        router_enabled=True,
        max_concurrent=4,
        is_active=True,
        is_internal=True,
        api_key_secret_ref=envelope,
    ))
    db.commit()
    document = _sample_document()
    document["models"] = [item for item in document["models"] if item["name"] == "gemma-import"]
    document["models"][0]["display_name"] = "新名"
    document["model_roles"] = []
    document["router_grants"] = []
    document["external_services"] = []
    document["departments"] = []
    document["banners"] = []
    applied = client.post(
        "/api/admin/settings-import",
        json={"dry_run": False, "document": document},
        headers=headers,
    )
    assert applied.status_code == 200, applied.text
    model_item = next(item for item in applied.json()["items"] if item["key"] == "gemma-import")
    assert model_item["action"] == "update"
    db.expire_all()
    model = db.query(ModelRegistry).filter(ModelRegistry.name == "gemma-import").one()
    assert model.display_name == "新名"
    assert model.api_key_secret_ref == envelope


def test_import_rejects_router_primary_without_campus_grant(client, db):
    headers = _headers(client, db, "grant-owner", role="owner")
    document = _sample_document()
    document["models"] = [item for item in document["models"] if item["name"] == "gemma-import"]
    document["model_roles"] = [{"role": "router_primary", "model_name": "gemma-import"}]
    document["router_grants"] = []
    document["external_services"] = []
    document["departments"] = []
    document["banners"] = []
    resp = client.post(
        "/api/admin/settings-import",
        json={"dry_run": True, "document": document},
        headers=headers,
    )
    assert resp.status_code == 400
    assert "全院授權" in resp.text
    db.expire_all()
    assert db.query(ModelRegistry).filter(ModelRegistry.name == "gemma-import").count() == 0


def test_import_rejects_untrusted_endpoint(client, db):
    headers = _headers(client, db, "ssrf-owner", role="owner")
    document = _sample_document()
    document["trusted_hosts"] = []
    document["models"] = [item for item in document["models"] if item["name"] == "gemma-import"]
    document["model_roles"] = []
    document["router_grants"] = []
    document["external_services"] = []
    document["departments"] = []
    document["banners"] = []
    resp = client.post(
        "/api/admin/settings-import",
        json={"dry_run": False, "document": document},
        headers=headers,
    )
    assert resp.status_code == 400, resp.text
    db.expire_all()
    assert db.query(ModelRegistry).filter(ModelRegistry.name == "gemma-import").count() == 0
    assert db.query(TrustedHost).count() == 0


def test_settings_transfer_rejects_non_admin(client, db):
    headers = _headers(client, db, "plain-transfer", role="user")
    assert client.get("/api/admin/settings-export", headers=headers).status_code == 403
    resp = client.post(
        "/api/admin/settings-import",
        json={"dry_run": True, "document": {"schema_version": 1}},
        headers=headers,
    )
    assert resp.status_code == 403


def test_import_cookie_requires_csrf(client, db):
    make_user(db, username="csrf-transfer", role="owner")
    login_resp = client.post(
        "/api/auth/login",
        json={"username": "csrf-transfer", "password": "password"},
    )
    assert login_resp.status_code == 200, login_resp.text
    blocked = client.post(
        "/api/admin/settings-import",
        json={"dry_run": True, "document": {"schema_version": 1}},
    )
    assert blocked.status_code == 403
    assert "CSRF" in blocked.text
    token = client.cookies.get("anila_csrf")
    allowed = client.post(
        "/api/admin/settings-import",
        json={"dry_run": True, "document": {"schema_version": 1}},
        headers={"X-CSRF-Token": token},
    )
    assert allowed.status_code == 200, allowed.text


def _items(body):
    return {(item["entity"], item["key"]): item for item in body["items"]}


def _identical_model_row(**extra) -> ModelRegistry:
    fields = dict(
        name="same-fields",
        display_name="Same",
        model_type="llm",
        endpoint_url="https://gateway.example.com/v1",
        api_version="v1",
        protocol="openai_compatible",
        router_enabled=False,
        max_concurrent=16,
        is_active=True,
        is_internal=True,
        description=None,
        context_window=None,
        classification_ceiling=None,
        supports_streaming=True,
        supports_json_schema=False,
        supports_tools=False,
        thinking_effort=None,
        thinking_user_selectable=True,
        temperature=None,
        top_p=None,
        presence_penalty=None,
        max_tokens=None,
    )
    fields.update(extra)
    return ModelRegistry(**fields)


def test_export_scrubs_model_endpoint_userinfo(client, db):
    headers = _headers(client, db, "userinfo-owner", role="owner")
    poisoned = f"https://alice:{USERINFO_SECRET}@gateway.example.com/v1"
    db.add(_identical_model_row(name="userinfo-model", endpoint_url=poisoned, is_internal=False))
    db.commit()

    exported = client.get("/api/admin/settings-export", headers=headers)
    assert exported.status_code == 200, exported.text
    blob = exported.text
    assert USERINFO_SECRET not in blob
    assert "alice:" not in blob
    item = next(row for row in exported.json()["models"] if row["name"] == "userinfo-model")
    assert item["endpoint_url"] == "https://gateway.example.com/v1"
    assert item["endpoint_hidden"] is False
    assert item["credential_required"] is True
    assert item["credential"] is None

    db.expire_all()
    stored = db.query(ModelRegistry).filter(ModelRegistry.name == "userinfo-model").one()
    assert stored.endpoint_url == poisoned


def test_model_api_rejects_endpoint_userinfo(client, db):
    headers = _headers(client, db, "userinfo-api", role="owner")
    poisoned = f"https://alice:{USERINFO_SECRET}@gateway.example.com/v1"
    body = {
        "name": "userinfo-create",
        "display_name": "Userinfo",
        "model_type": "llm",
        "endpoint_url": poisoned,
    }
    created = client.post("/api/models", json=body, headers=headers)
    assert created.status_code == 400, created.text
    assert "帳號" in created.text
    assert USERINFO_SECRET not in created.text
    db.expire_all()
    assert db.query(ModelRegistry).filter(ModelRegistry.name == "userinfo-create").count() == 0

    clean = client.post(
        "/api/models",
        json={**body, "endpoint_url": "https://gateway.example.com/v1"},
        headers=headers,
    )
    assert clean.status_code == 200, clean.text
    model_id = clean.json()["id"]
    updated = client.put(
        f"/api/models/{model_id}",
        json={"endpoint_url": poisoned},
        headers=headers,
    )
    assert updated.status_code == 400, updated.text
    assert "帳號" in updated.text
    assert USERINFO_SECRET not in updated.text
    db.expire_all()
    row = db.get(ModelRegistry, model_id)
    assert row.endpoint_url == "https://gateway.example.com/v1"
    assert USERINFO_SECRET not in row.endpoint_url


def test_import_rejects_endpoint_userinfo(client, db):
    headers = _headers(client, db, "userinfo-import", role="owner")
    poisoned = f"https://alice:{USERINFO_SECRET}@gateway.example.com/v1"
    document = {
        "schema_version": 1,
        "models": [{
            "name": "userinfo-import",
            "display_name": "Userinfo",
            "model_type": "llm",
            "endpoint_url": poisoned,
        }],
    }
    resp = client.post(
        "/api/admin/settings-import",
        json={"dry_run": False, "document": document},
        headers=headers,
    )
    assert resp.status_code == 400, resp.text
    assert "帳號" in resp.text
    assert USERINFO_SECRET not in resp.text
    db.expire_all()
    assert db.query(ModelRegistry).filter(ModelRegistry.name == "userinfo-import").count() == 0


def test_import_keeps_endpoint_userinfo_when_file_matches_stripped_url(client, db):
    """匯出拿掉帳密後再匯入，位址與去掉帳密的原值相同且檔案沒帶金鑰，端點不算變更。"""
    owner_headers = _headers(client, db, "userinfo-keep-owner", role="owner")
    admin_headers = _headers(client, db, "userinfo-keep-admin", role="admin")
    poisoned = f"https://alice:{USERINFO_SECRET}@gateway.example.com/v1"
    db.add(_identical_model_row(
        name="userinfo-model",
        endpoint_url=poisoned,
        is_internal=False,
    ))
    db.commit()

    exported = client.get("/api/admin/settings-export", headers=owner_headers)
    assert exported.status_code == 200, exported.text
    assert USERINFO_SECRET not in exported.text
    document = exported.json()
    item = next(row for row in document["models"] if row["name"] == "userinfo-model")
    assert item["endpoint_url"] == "https://gateway.example.com/v1"
    assert item["credential"] is None

    preview = client.post(
        "/api/admin/settings-import",
        json={"dry_run": True, "document": document},
        headers=owner_headers,
    )
    assert preview.status_code == 200, preview.text
    assert USERINFO_SECRET not in preview.text
    planned = _items(preview.json())[("model", "userinfo-model")]
    assert planned["action"] == "skip"
    assert planned["message"] == "端點帳密保留原值"

    db.expire_all()
    before = db.query(ModelRegistry).filter(ModelRegistry.name == "userinfo-model").one()
    updated_at = before.updated_at
    applied = client.post(
        "/api/admin/settings-import",
        json={"dry_run": False, "document": document},
        headers=owner_headers,
    )
    assert applied.status_code == 200, applied.text
    assert USERINFO_SECRET not in applied.text
    assert "alice:" not in applied.text
    again = _items(applied.json())[("model", "userinfo-model")]
    assert again["action"] == "skip"
    assert again["message"] == "端點帳密保留原值"
    db.expire_all()
    stored = db.query(ModelRegistry).filter(ModelRegistry.name == "userinfo-model").one()
    assert stored.endpoint_url == poisoned
    assert stored.updated_at == updated_at

    admin_preview = client.post(
        "/api/admin/settings-import",
        json={"dry_run": True, "document": document},
        headers=admin_headers,
    )
    assert admin_preview.status_code == 200, admin_preview.text
    admin_item = _items(admin_preview.json())[("model", "userinfo-model")]
    assert admin_item["action"] == "skip"
    assert admin_item["message"] == "端點帳密保留原值"

    renamed = json.loads(json.dumps(document))
    target = next(row for row in renamed["models"] if row["name"] == "userinfo-model")
    target["display_name"] = "改名但仍留帳密"
    renamed_apply = client.post(
        "/api/admin/settings-import",
        json={"dry_run": False, "document": renamed},
        headers=owner_headers,
    )
    assert renamed_apply.status_code == 200, renamed_apply.text
    assert USERINFO_SECRET not in renamed_apply.text
    renamed_item = _items(renamed_apply.json())[("model", "userinfo-model")]
    assert renamed_item["action"] == "update"
    assert renamed_item["message"] == "端點帳密保留原值"
    db.expire_all()
    stored = db.query(ModelRegistry).filter(ModelRegistry.name == "userinfo-model").one()
    assert stored.display_name == "改名但仍留帳密"
    assert stored.endpoint_url == poisoned


def test_import_flags_replaced_endpoint_that_had_userinfo(client, db):
    """檔案換成另一個 host 時照舊更新，不把原帳密帶過去，試算要標明得另外填金鑰。"""
    headers = _headers(client, db, "userinfo-drop-owner", role="owner")
    poisoned = f"https://alice:{USERINFO_SECRET}@gateway.example.com/v1"
    db.add(_identical_model_row(name="userinfo-model", endpoint_url=poisoned))
    db.commit()
    document = {
        "schema_version": 1,
        "models": [{
            "name": "userinfo-model",
            "display_name": "Same",
            "model_type": "llm",
            "endpoint_url": "https://other.example.com/v1",
            "credential": None,
            "credential_required": True,
        }],
    }
    preview = client.post(
        "/api/admin/settings-import",
        json={"dry_run": True, "document": document},
        headers=headers,
    )
    assert preview.status_code == 200, preview.text
    assert USERINFO_SECRET not in preview.text
    planned = _items(preview.json())[("model", "userinfo-model")]
    assert planned["action"] == "update"
    assert planned["message"] == "原端點帶帳密，套用後需另外填入金鑰"
    db.expire_all()
    assert db.query(ModelRegistry).filter(ModelRegistry.name == "userinfo-model").one().endpoint_url == poisoned

    applied = client.post(
        "/api/admin/settings-import",
        json={"dry_run": False, "document": document},
        headers=headers,
    )
    assert applied.status_code == 200, applied.text
    assert USERINFO_SECRET not in applied.text
    assert "alice:" not in applied.text
    applied_item = _items(applied.json())[("model", "userinfo-model")]
    assert applied_item["action"] == "update"
    assert applied_item["message"] == "原端點帶帳密，套用後需另外填入金鑰"
    db.expire_all()
    stored = db.query(ModelRegistry).filter(ModelRegistry.name == "userinfo-model").one()
    assert stored.endpoint_url == "https://other.example.com/v1"
    assert USERINFO_SECRET not in (stored.endpoint_url or "")
    details = " ".join(
        (row.detail or "") for row in db.query(AuditLog).all()
    )
    assert USERINFO_SECRET not in details


def test_import_apply_rolls_back_late_phase(client, db, monkeypatch):
    headers = _headers(client, db, "rollback-owner", role="owner")
    db.add(TrustedHost(host="gemma4", note="舊備註"))
    banner = Banner(
        level="info",
        content="演練公告",
        is_active=True,
        show_on_login=False,
        sort_order=0,
    )
    db.add(banner)
    db.commit()
    banner_id = banner.id
    document = _sample_document()
    document["banners"][0]["sort_order"] = 9

    from app.services.settings_transfer import log_audit_event_or_raise as real

    def _late(db, **kwargs):
        if kwargs.get("action") == "settings_import":
            raise RuntimeError("late phase")
        return real(db, **kwargs)

    monkeypatch.setattr("app.services.settings_transfer.log_audit_event_or_raise", _late)
    with pytest.raises(RuntimeError, match="late phase"):
        client.post(
            "/api/admin/settings-import",
            json={"dry_run": False, "document": document},
            headers=headers,
        )

    db.expire_all()
    host = db.query(TrustedHost).filter(TrustedHost.host == "gemma4").one()
    assert host.note == "舊備註"
    assert db.query(Department).filter(Department.name == "資訊所").count() == 0
    assert db.query(ModelRegistry).filter(ModelRegistry.name == "gemma-import").count() == 0
    assert db.query(ModelRegistry).filter(ModelRegistry.name == "embed-import").count() == 0
    assert db.get(Banner, banner_id).sort_order == 0
    assert db.get(ModelRole, "summary") is None
    assert db.query(AuditLog).filter(AuditLog.action == "trusted_host.update").count() == 0
    assert db.query(AuditLog).filter(AuditLog.action == "update_banner").count() == 0
    assert db.query(AuditLog).filter(AuditLog.resource_type == "department").count() == 0
    assert db.query(AuditLog).filter(AuditLog.action == "settings_import").count() == 0


def test_import_deactivating_model_marks_agents_offline(client, db, monkeypatch):
    headers = _headers(client, db, "offline-owner", role="owner")
    owner = db.query(User).filter(User.username == "offline-owner").one()
    model = _identical_model_row(name="offline-base", display_name="Offline")
    db.add(model)
    db.commit()
    db.refresh(model)
    agent = make_agent(db, owner, name="offline-agent")
    agent.base_model_id = model.id
    db.commit()
    document = {
        "schema_version": 1,
        "models": [{
            "name": "offline-base",
            "display_name": "Offline",
            "model_type": "llm",
            "endpoint_url": "https://gateway.example.com/v1",
            "is_active": False,
            "is_internal": True,
        }],
    }

    def _boom(*args, **kwargs):
        raise RuntimeError("after models")

    with monkeypatch.context() as patched:
        patched.setattr("app.services.settings_transfer._apply_banners", _boom)
        with pytest.raises(RuntimeError, match="after models"):
            client.post(
                "/api/admin/settings-import",
                json={"dry_run": False, "document": document},
                headers=headers,
            )
    db.expire_all()
    assert db.query(ModelRegistry).filter(ModelRegistry.name == "offline-base").one().is_active is True
    assert db.query(Notification).filter(Notification.type == "agent_base_model_offline").count() == 0
    from app.models.agent import Agent
    assert db.get(Agent, agent.id).unavailable_reason is None

    applied = client.post(
        "/api/admin/settings-import",
        json={"dry_run": False, "document": document},
        headers=headers,
    )
    assert applied.status_code == 200, applied.text
    db.expire_all()
    stored = db.query(ModelRegistry).filter(ModelRegistry.name == "offline-base").one()
    assert stored.is_active is False
    assert db.get(Agent, agent.id).unavailable_reason == "base_model_offline"
    note = db.query(Notification).filter(Notification.type == "agent_base_model_offline").one()
    assert note.user_id == owner.id


def test_dry_run_lists_implicit_ancestor_departments(client, db):
    headers = _headers(client, db, "ancestor-owner", role="owner")
    document = {
        "schema_version": 1,
        "departments": [{"path": ["A", "B"], "description": "子單位", "is_active": True}],
    }
    preview = client.post(
        "/api/admin/settings-import",
        json={"dry_run": True, "document": document},
        headers=headers,
    )
    assert preview.status_code == 200, preview.text
    departments = [item for item in preview.json()["items"] if item["entity"] == "department"]
    assert [(item["key"], item["action"]) for item in departments] == [
        ("A", "create"),
        ("A／B", "create"),
    ]
    db.expire_all()
    assert db.query(Department).count() == 0


def test_supplied_credential_is_an_update_even_when_fields_match(client, db):
    headers = _headers(client, db, "key-owner", role="owner")
    db.add(_identical_model_row())
    ensure_rows(db)
    db.commit()
    model_key = "sk-same-fields-key"
    service_key = "ext-same-fields-key"
    document = {
        "schema_version": 1,
        "models": [{
            "name": "same-fields",
            "display_name": "Same",
            "model_type": "llm",
            "endpoint_url": "https://gateway.example.com/v1",
            "is_internal": True,
            "credential": model_key,
        }],
        "external_services": [{
            "kind": "document_parser",
            "enabled": False,
            "base_url": "",
            "protocol": "native",
            "credential": service_key,
        }],
    }
    preview = client.post(
        "/api/admin/settings-import",
        json={"dry_run": True, "document": document},
        headers=headers,
    )
    assert preview.status_code == 200, preview.text
    actions = _items(preview.json())
    assert actions[("model", "same-fields")]["action"] == "update"
    assert actions[("model", "same-fields")]["message"] == "寫入金鑰"
    assert actions[("external_service", "document_parser")]["action"] == "update"
    assert actions[("external_service", "document_parser")]["message"] == "寫入金鑰"
    db.expire_all()
    assert db.query(ModelRegistry).filter(ModelRegistry.name == "same-fields").one().api_key_secret_ref is None

    applied = client.post(
        "/api/admin/settings-import",
        json={"dry_run": False, "document": document},
        headers=headers,
    )
    assert applied.status_code == 200, applied.text
    db.expire_all()
    model = db.query(ModelRegistry).filter(ModelRegistry.name == "same-fields").one()
    assert decode_service_token_envelope(model.api_key_secret_ref) == model_key
    service = db.get(ExternalService, DOCUMENT_PARSER)
    assert open_external_credential(service.credential_envelope) == service_key


def test_hidden_endpoint_export_imports_as_skip(client, db):
    owner_headers = _headers(client, db, "hidden-owner", role="owner")
    admin_headers = _headers(client, db, "hidden-admin", role="admin")
    public_url = "https://models.example.com/v1"
    internal_url = "https://internal-models.example.com/v1"
    db.add(_identical_model_row(
        name="public-model",
        display_name="Public",
        endpoint_url=public_url,
        is_internal=False,
    ))
    db.add(_identical_model_row(
        name="internal-model",
        display_name="Internal",
        endpoint_url=internal_url,
        is_internal=True,
    ))
    db.commit()

    owner_export = client.get("/api/admin/settings-export", headers=owner_headers)
    assert owner_export.status_code == 200, owner_export.text
    owner_public = next(item for item in owner_export.json()["models"] if item["name"] == "public-model")
    assert owner_public["endpoint_url"] == public_url
    assert owner_public["endpoint_hidden"] is False

    hidden = client.get("/api/admin/settings-export", headers=admin_headers)
    assert hidden.status_code == 200, hidden.text
    blob = hidden.text
    assert public_url not in blob
    assert internal_url not in blob
    assert "<owner-only>" not in blob
    assert "<internal>" not in blob
    document = hidden.json()
    for name in ("public-model", "internal-model"):
        item = next(row for row in document["models"] if row["name"] == name)
        assert item["endpoint_url"] is None
        assert item["endpoint_hidden"] is True
        item["display_name"] = "不該寫入"

    preview = client.post(
        "/api/admin/settings-import",
        json={"dry_run": True, "document": document},
        headers=admin_headers,
    )
    assert preview.status_code == 200, preview.text
    actions = _items(preview.json())
    for name in ("public-model", "internal-model"):
        assert actions[("model", name)]["action"] == "skip"
        assert actions[("model", name)]["message"] == "需另外填入端點"

    applied = client.post(
        "/api/admin/settings-import",
        json={"dry_run": False, "document": document},
        headers=admin_headers,
    )
    assert applied.status_code == 200, applied.text
    db.expire_all()
    public = db.query(ModelRegistry).filter(ModelRegistry.name == "public-model").one()
    internal = db.query(ModelRegistry).filter(ModelRegistry.name == "internal-model").one()
    assert public.endpoint_url == public_url
    assert internal.endpoint_url == internal_url
    assert public.display_name == "Public"
    assert internal.display_name == "Internal"

    changed = hidden.json()
    for item in changed["models"]:
        if item["name"] == "public-model":
            item["endpoint_hidden"] = False
            item["endpoint_url"] = "https://other.example.com/v1"
    blocked = client.post(
        "/api/admin/settings-import",
        json={"dry_run": True, "document": changed},
        headers=admin_headers,
    )
    assert blocked.status_code == 403, blocked.text
    assert "端點位址" in blocked.text
    db.expire_all()
    assert db.query(ModelRegistry).filter(ModelRegistry.name == "public-model").one().endpoint_url == public_url


def test_import_audits_banner_update_and_host_note(client, db):
    headers = _headers(client, db, "audit-owner", role="owner")
    host = TrustedHost(host="gemma4", note="舊備註")
    banner = Banner(
        level="info",
        content="演練公告",
        is_active=True,
        show_on_login=False,
        sort_order=0,
    )
    db.add(host)
    db.add(banner)
    db.commit()
    db.refresh(host)
    db.refresh(banner)
    document = {
        "schema_version": 1,
        "trusted_hosts": [{"host": "gemma4", "note": "新備註"}],
        "banners": [{
            "level": "info",
            "content": "演練公告",
            "show_on_login": True,
            "sort_order": 4,
        }],
    }
    applied = client.post(
        "/api/admin/settings-import",
        json={"dry_run": False, "document": document},
        headers=headers,
    )
    assert applied.status_code == 200, applied.text
    db.expire_all()
    assert db.query(TrustedHost).filter(TrustedHost.host == "gemma4").one().note == "新備註"
    stored_banner = db.get(Banner, banner.id)
    assert stored_banner.sort_order == 4
    assert stored_banner.show_on_login is True
    host_audit = db.query(AuditLog).filter(AuditLog.action == "trusted_host.update").one()
    assert host_audit.resource_type == "trusted_host"
    assert host_audit.resource_id == str(host.id)
    assert "備註" in host_audit.detail
    banner_audit = db.query(AuditLog).filter(AuditLog.action == "update_banner").one()
    assert banner_audit.resource_type == "banner"
    assert banner_audit.resource_id == str(banner.id)
    assert "更新公告" in banner_audit.detail


def test_import_rejects_body_over_2mb(client, db):
    headers = _headers(client, db, "bulk-owner", role="owner")
    raw = json.dumps({"dry_run": True, "document": {"schema_version": 1}}).encode()
    raw = raw + b" " * (_MAX_IMPORT_BYTES - len(raw) + 1)
    assert len(raw) > _MAX_IMPORT_BYTES
    blocked = client.post(
        "/api/admin/settings-import",
        content=raw,
        headers={**headers, "Content-Type": "application/json"},
    )
    assert blocked.status_code == 413, blocked.text
    assert "匯入檔不可超過 2 MB" in blocked.text

    allowed = client.post(
        "/api/admin/settings-import",
        json={"dry_run": True, "document": {"schema_version": 1}},
        headers=headers,
    )
    assert allowed.status_code == 200, allowed.text


def _post_asgi_chunks(client, headers, chunks, *, content_length: str | None):
    """把本文分成多個 http.request 塊送進 ASGI。

    TestClient 會先把整個 httpx 串流讀進記憶體，看不出伺服器有沒有提早停。
    這裡直接驅動 ASGI：每一塊都等 app 來要才交出去。回應一開始就記下已交出的位元組。
    """
    pending = list(chunks)
    state = {"sent": 0, "at_response": None}
    messages: list[dict] = []

    async def receive():
        if not pending:
            return {"type": "http.request", "body": b"", "more_body": False}
        piece = pending.pop(0)
        state["sent"] += len(piece)
        return {
            "type": "http.request",
            "body": piece,
            "more_body": bool(pending),
        }

    async def send(message):
        if message["type"] == "http.response.start" and state["at_response"] is None:
            state["at_response"] = state["sent"]
        messages.append(message)

    raw_headers = [
        (key.lower().encode("latin-1"), value.encode("latin-1"))
        for key, value in headers.items()
    ]
    raw_headers.append((b"content-type", b"application/json"))
    raw_headers.append((b"host", b"testserver"))
    if content_length is not None:
        raw_headers.append((b"content-length", content_length.encode("latin-1")))
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/api/admin/settings-import",
        "raw_path": b"/api/admin/settings-import",
        "query_string": b"",
        "headers": raw_headers,
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
        "state": {},
    }

    async def run():
        await client.app(scope, receive, send)

    client.portal.call(run)
    status = next(message["status"] for message in messages if message["type"] == "http.response.start")
    body = b"".join(
        message.get("body", b"")
        for message in messages
        if message["type"] == "http.response.body"
    )
    return status, body.decode(), state["at_response"]


def test_import_chunked_body_stops_at_2mb(client, db):
    """沒有 Content-Length、或 Content-Length 說得比實際小，讀過 2 MB 就要停。"""
    headers = _headers(client, db, "chunk-owner", role="owner")
    chunk = 64 * 1024
    cap = _MAX_IMPORT_BYTES
    offered = cap * 3
    pieces = [b"x" * chunk for _ in range(offered // chunk)]
    assert sum(len(piece) for piece in pieces) == offered

    status, text, read = _post_asgi_chunks(client, headers, pieces, content_length=None)
    assert status == 413, text
    assert "匯入檔不可超過 2 MB" in text
    assert read is not None
    assert read <= cap + chunk
    assert read < offered

    status, text, read = _post_asgi_chunks(client, headers, pieces, content_length="8")
    assert status == 413, text
    assert "匯入檔不可超過 2 MB" in text
    assert read is not None
    assert read <= cap + chunk
    assert read < offered

    payload = json.dumps(
        {"dry_run": True, "document": {"schema_version": 1}}
    ).encode()
    status, text, read = _post_asgi_chunks(
        client,
        headers,
        [payload[:8], payload[8:]],
        content_length=None,
    )
    assert status == 200, text
    assert read == len(payload)


def test_also_trust_does_not_leak_across_requests():
    from app.services.settings_transfer import _also_trust

    barrier = threading.Barrier(2)
    errors: list[str] = []

    def _worker(own: str, other: str) -> None:
        try:
            with _also_trust({own}):
                barrier.wait(timeout=5)
                if _host_is_trusted(other):
                    errors.append(f"{own} saw {other}")
                if not _host_is_trusted(own):
                    errors.append(f"{own} missing")
        except Exception as exc:
            errors.append(repr(exc))

    first = threading.Thread(target=_worker, args=("zz-import-a.example", "zz-import-b.example"))
    second = threading.Thread(target=_worker, args=("zz-import-b.example", "zz-import-a.example"))
    first.start()
    second.start()
    first.join(timeout=10)
    second.join(timeout=10)
    assert not first.is_alive()
    assert not second.is_alive()
    assert errors == []
    assert _host_is_trusted("zz-import-a.example") is False
    assert _host_is_trusted("zz-import-b.example") is False
