"""Platform anila-router forwards caller credentials, never MODEL_GATEWAY_API_KEY."""
from urllib.parse import urlparse

from app.models.model_registry import ModelRegistry
from app.services.auto_seed import PLATFORM_ROUTER_ENDPOINT, PLATFORM_ROUTER_NAME, ensure_platform_router_model
from app.services.proxy.headers import resolve_model_gateway_key
from tests.conftest import make_model


def test_platform_entry_does_not_inherit_gateway_key(db, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "MODEL_GATEWAY_API_KEY", "sk-global-must-not-leak")
    row = ensure_platform_router_model(db)
    db.commit()
    assert row.name == PLATFORM_ROUTER_NAME
    assert resolve_model_gateway_key(row) == ""


def test_real_llm_still_can_use_global_fallback(db, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "MODEL_GATEWAY_API_KEY", "sk-global-fallback")
    model = make_model(db, name="glm-upstream")
    assert resolve_model_gateway_key(model) == "sk-global-fallback"


def test_fixed_router_endpoint_helper_rejects_foreign_host(db):
    from app.api.proxy import _assert_fixed_router_endpoint
    from fastapi import HTTPException
    row = ModelRegistry(
        name=PLATFORM_ROUTER_NAME,
        display_name="ANILA 自動選助手",
        model_type="llm",
        endpoint_url="http://evil.example:9000",
        is_active=True,
        is_internal=True,
    )
    db.add(row)
    db.commit()
    try:
        _assert_fixed_router_endpoint(row)
        raised = False
    except HTTPException as exc:
        raised = True
        assert exc.status_code == 403
    assert raised is True
    expected = urlparse(PLATFORM_ROUTER_ENDPOINT).netloc
    row.endpoint_url = PLATFORM_ROUTER_ENDPOINT
    db.commit()
    _assert_fixed_router_endpoint(row)
    assert urlparse(row.endpoint_url).netloc == expected


def test_router_disabled_old_grant_denied_on_route(db, client):
    from app.models.user import UserModelPermission
    from tests.conftest import login, make_user
    from app.api.proxy import _resolve_model
    from app.middleware.caller import Caller
    from fastapi import HTTPException, Request
    user = make_user(db, username="old-grant")
    model = make_model(db, name="legacy-llm")
    model.router_enabled = False
    db.add(UserModelPermission(user_id=user.id, model_id=model.id))
    db.commit()
    class H(dict):
        def get(self, k, default=None):
            return dict.get(self, k, default)
    class Dummy:
        headers = {"X-ANILA-Route": "answer"}
    caller = Caller(user=user, api_key_id=None)
    try:
        _resolve_model(db, caller, "legacy-llm", Dummy())
        raised = False
    except HTTPException as exc:
        raised = True
        assert exc.status_code == 403
    assert raised


def test_resolve_endpoint_intersects_and_conflict(client, db):
    from tests.conftest import login, make_user, make_api_key
    from app.models.api_key import ApiKeyModelPermission
    from app.models.router_model_grant import RouterModelGrant
    user = make_user(db, username="resolve-user")
    glm = make_model(db, name="glm-resolve")
    glm.router_enabled = True
    glm.is_router_primary = True
    qwen = make_model(db, name="qwen-resolve")
    qwen.router_enabled = True
    db.add(RouterModelGrant(model_id=glm.id, scope_type="all"))
    db.add(RouterModelGrant(model_id=qwen.id, scope_type="all"))
    db.commit()
    token = login(client, "resolve-user")
    conv = client.post("/api/conversations", headers={"Authorization": f"Bearer {token}"}, json={"title":"t","origin":"anila-ui"}).json()
    ok = client.post("/api/router-models/resolve", headers={"Authorization": f"Bearer {token}"}, json={"router_model":"glm-resolve", "conversation_id": conv["id"]})
    assert ok.status_code == 200, ok.text
    conflict = client.post("/api/router-models/resolve", headers={"Authorization": f"Bearer {token}"}, json={"router_model":"qwen-resolve", "conversation_id": conv["id"]})
    assert conflict.status_code == 409
    missing = client.post("/api/router-models/resolve", headers={"Authorization": f"Bearer {token}"}, json={"router_model":"no-such-model", "conversation_id": conv["id"]})
    assert missing.status_code == 409


def _open_granted_llm(db, name):
    from app.models.router_model_grant import RouterModelGrant
    model = make_model(db, name=name)
    model.router_enabled = True
    db.add(RouterModelGrant(model_id=model.id, scope_type="all"))
    db.commit()
    db.refresh(model)
    return model


def test_route_header_does_not_bypass_restricted_api_key(client, db):
    from tests.conftest import make_user, make_api_key
    from app.models.api_key import ApiKeyModelPermission
    from app.models.router_model_grant import RouterModelGrant
    user = make_user(db, username="key-scope-user")
    glm = _open_granted_llm(db, "glm-key-scope")
    other = _open_granted_llm(db, "qwen-key-scope")
    raw = "sk-restricted-scope"
    key = make_api_key(db, user, raw_key=raw)
    db.add(ApiKeyModelPermission(api_key_id=key.id, model_id=other.id))
    db.commit()
    headers = {"Authorization": f"Bearer {raw}"}
    body = {"model": "glm-key-scope", "messages": [{"role": "user", "content": "hi"}]}
    denied = client.post("/v1/chat/completions", headers=headers, json=body)
    assert denied.status_code == 403, denied.text
    for route in ("answer", "1", "true", "router"):
        routed = client.post(
            "/v1/chat/completions",
            headers={**headers, "X-ANILA-Route": route},
            json=body,
        )
        assert routed.status_code == 403, routed.text
    # revoke campus grant: still 403 even with a matching key
    db.query(RouterModelGrant).filter(RouterModelGrant.model_id == other.id).delete()
    db.commit()
    allowed_key = make_api_key(db, user, raw_key="sk-has-other")
    db.add(ApiKeyModelPermission(api_key_id=allowed_key.id, model_id=other.id))
    db.commit()
    revoked = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer sk-has-other", "X-ANILA-Route": "answer"},
        json={"model": "qwen-key-scope", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert revoked.status_code == 403


def test_forced_route_does_not_reject_platform_entry(db, client):
    from app.api.proxy import _resolve_model
    from app.middleware.caller import Caller
    from app.services.auto_seed import PLATFORM_ROUTER_NAME, ensure_platform_router_model
    from tests.conftest import login, make_user
    user = make_user(db, username="forced-entry")
    ensure_platform_router_model(db)
    db.commit()
    class Req:
        headers = {"X-ANILA-Route": "forced"}
    caller = Caller(user=user, api_key_id=None)
    resolved = _resolve_model(db, caller, PLATFORM_ROUTER_NAME, Req())
    assert resolved.name == PLATFORM_ROUTER_NAME
    token = login(client, "forced-entry")
    resp = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {token}", "X-ANILA-Route": "forced"},
        json={"model": PLATFORM_ROUTER_NAME, "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code != 403, resp.text
