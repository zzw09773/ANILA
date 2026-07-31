"""FAKE-CONTROLS cleanup: retired accept-and-ignore settings must refuse.

Each test below goes red if the corresponding write path is restored without
an effect (silent success). Revert the production guard to confirm.
"""
from __future__ import annotations

import pytest

from app.api import handoffs as handoffs_api
from app.api.agents import registration as reg
from tests.conftest import login, make_agent, make_user


def test_agent_register_rejects_classification_ceiling():
    """Revert: drop _reject_dead_agent_controls ceiling check → silent accept."""
    with pytest.raises(Exception) as exc:
        reg.AgentRegisterRequest(
            name="x",
            endpoint_url="http://agent.example:9000",
            description_for_router="enough characters for router description here",
            base_model_id=1,
            classification_ceiling="密",
        )
    msg = str(exc.value).lower()
    assert "classification_ceiling" in msg or "退場" in str(exc.value)


def test_agent_register_rejects_capabilities():
    """Revert: drop capabilities rejection → silent accept again."""
    with pytest.raises(Exception) as exc:
        reg.AgentRegisterRequest(
            name="x",
            endpoint_url="http://agent.example:9000",
            description_for_router="enough characters for router description here",
            base_model_id=1,
            capabilities={"streaming": True},
        )
    msg = str(exc.value).lower()
    assert "capabilities" in msg or "退場" in str(exc.value)


def test_agent_update_rejects_capabilities():
    with pytest.raises(Exception) as exc:
        reg.AgentUpdateRequest(capabilities={"vision": False})
    msg = str(exc.value).lower()
    assert "capabilities" in msg or "退場" in str(exc.value)


def test_serialize_agent_omits_classification_ceiling_and_capabilities():
    """Revert: put fields back on _serialize_agent → assertion fails."""

    class _A:
        id = 1
        name = "a"
        owner_user_id = 1
        owner = None
        endpoint_url = "http://x"
        api_version = "v1"
        description_for_router = "d"
        base_model_id = None
        base_model = None
        capabilities = {"streaming": True}
        health_status = "healthy"
        approval_status = "registered"
        requires_encryption = False
        runtime_type = None
        agent_version = None
        audit_level = None
        classification_ceiling = "密"
        default_classification_level = "無機密"
        manifest_json = None
        runtime_config = None
        created_at = None
        bound_collection_id = None

    body = reg._serialize_agent(_A())
    assert "classification_ceiling" not in body
    assert "capabilities" not in body


def test_runtime_config_patch_is_gone(client, db):
    """Revert: restore patch_agent_runtime_config write body → expect 200 again."""
    owner = make_user(db, "rtcfg_owner", role="developer")
    agent = make_agent(db, owner, name="rtcfg-agent")
    token = login(client, "rtcfg_owner")
    resp = client.patch(
        f"/api/agents/{agent.id}/runtime-config",
        headers={"Authorization": f"Bearer {token}"},
        json={"runtime_config": {"tool_permissions": {"deny_tools": ["x"]}}},
    )
    assert resp.status_code == 410
    assert "runtime_config" in resp.json()["detail"]


def test_handoff_accept_reject_are_no_longer_stubs():
    """2026-07-31：交接不再是假控制項，兩支端點真的做事。

    這裡只釘「不是 501 佔位」這件事；真正的行為驗收(換手、原擁有者保留
    讀取、密等擋下)在 ``tests/test_handoff_transfer.py``。
    Revert: 把 accept/reject 改回 ``raise HTTPException(501)`` → 本測試轉紅。
    """
    import inspect

    for fn in (handoffs_api.accept_handoff, handoffs_api.reject_handoff):
        source = inspect.getsource(fn)
        assert "501" not in source, f"{fn.__name__} 仍是 501 佔位"
        assert "transfer." in source, f"{fn.__name__} 沒有接到 handoff_transfer"


def test_enable_api_docs_flag_absent():
    from app.config import settings

    assert not hasattr(settings, "ENABLE_API_DOCS")


def test_service_create_rejects_healthcheck_url(client, db):
    """Revert: store healthcheck_url again → this goes red."""
    make_user(db, "svc_admin", role="admin")
    token = login(client, "svc_admin")
    resp = client.post(
        "/api/services",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "name": "假探測服務",
            "entry_url": "https://example.example/app",
            "healthcheck_url": "https://example.example/healthz",
        },
    )
    assert resp.status_code == 422
    detail = resp.json().get("detail")
    assert "healthcheck_url" in str(detail)


def test_model_create_rejects_custom_adapter(client, db, monkeypatch):
    """Revert: allow custom_adapter through create_model → this goes red."""
    monkeypatch.setenv("ANILA_ALLOW_HTTP_ENDPOINT", "1")
    monkeypatch.setenv("ANILA_ALLOW_PRIVATE_ENDPOINT", "1")
    monkeypatch.setenv("ANILA_TRUSTED_HOSTS", "mock-llm")
    make_user(db, "model_owner", role="owner")
    token = login(client, "model_owner")
    resp = client.post(
        "/api/models",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "name": "custom-adapter-model",
            "display_name": "Custom Adapter Model",
            "model_type": "llm",
            "endpoint_url": "http://mock-llm:8080/v1",
            "protocol": "custom_adapter",
        },
    )
    assert resp.status_code == 422
    assert "openai_compatible" in str(resp.json().get("detail"))
