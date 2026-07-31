# -*- coding: utf-8 -*-
"""OE-1 — Agent Registry three-state approval.

涵蓋:
- Manifest 契約(AgentManifest)fail-closed 驗證 + 註冊端點整合。
- 註冊預設落地 ``registered``(shadow 旗標忽略)。
- 三態核准:registered → approved(無 trace 閘);disabled 可重啟;冪等。

D1 removed the on-demand ``POST …/trace-test`` diagnostic with the span tables.
"""

from __future__ import annotations

import os

os.environ.setdefault("ANILA_ALLOW_DEV_SECRET", "1")

import pytest

from app.schemas.contracts.agents import AgentManifest, RuntimeType
from tests.conftest import login, make_agent, make_model, make_user

pytestmark = pytest.mark.filterwarnings("ignore")


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _valid_manifest() -> dict:
    return {
        "agent_id": "risk-agent",
        "name": "風險分析 Agent",
        "version": "1.0.0",
        "runtime_type": "langchain",
        "api_version": "v1",
        "supported_task_types": ["analyze", "compare"],
        "description_for_router": "分析風險並產出風險表",
        "capabilities": {
            "retrieval": True,
            "tools": ["document_search"],
            "streaming": True,
        },
        "trace": {
            "required": True,
            "protocol": "anila-full-trace-v1",
            "callback_mode": "sse_and_post",
        },
        "classification": {"ceiling": "機密", "default": "營業秘密"},
    }


@pytest.fixture(autouse=True)
def _env_allowances(monkeypatch):
    # Allow http:// single-label private hosts (agent) through the SSRF guard
    # for register outbound validation, as the dev stack does.
    monkeypatch.setenv("ANILA_ALLOW_HTTP_ENDPOINT", "1")
    monkeypatch.setenv("ANILA_ALLOW_PRIVATE_ENDPOINT", "1")
    monkeypatch.setenv("ANILA_TRUSTED_HOSTS", "agent,mock-llm")
    monkeypatch.setenv("SECRET_KEY", "test-secret-key-" + "a" * 40)


# ── Manifest contract ───────────────────────────────────────────────────────


class TestManifestValidation:
    def test_valid_manifest_parses(self):
        m = AgentManifest.model_validate(_valid_manifest())
        assert m.runtime_type == RuntimeType.LANGCHAIN
        assert m.classification.ceiling.value == "機密"
        assert m.trace.protocol == "anila-full-trace-v1"

    def test_unknown_field_rejected(self):
        bad = _valid_manifest()
        bad["surprise"] = 1
        with pytest.raises(Exception):
            AgentManifest.model_validate(bad)

    def test_non_five_level_classification_rejected(self):
        bad = _valid_manifest()
        bad["classification"]["ceiling"] = "絕密"  # not a 四級 value
        with pytest.raises(Exception):
            AgentManifest.model_validate(bad)

    def test_wrong_trace_protocol_rejected(self):
        bad = _valid_manifest()
        bad["trace"]["protocol"] = "not-anila"
        with pytest.raises(Exception):
            AgentManifest.model_validate(bad)

    def test_register_with_invalid_manifest_returns_422(self, client, db):
        make_user(db, username="mf_dev", role="developer")
        model = make_model(db, name="mf-model")
        token = login(client, "mf_dev")
        bad = _valid_manifest()
        bad["classification"]["ceiling"] = "絕密"
        resp = client.post(
            "/api/agents/register",
            headers=_bearer(token),
            json={
                "name": "mf-agent",
                "endpoint_url": "http://agent:9100",
                "description_for_router": "x",
                "base_model_id": model.id,
                "manifest": bad,
            },
        )
        assert resp.status_code == 422, resp.text
        assert "manifest" in resp.json()["detail"]

    def test_register_with_valid_manifest_stores_json(self, client, db):
        make_user(db, username="mf_dev2", role="developer")
        model = make_model(db, name="mf-model2")
        token = login(client, "mf_dev2")
        resp = client.post(
            "/api/agents/register",
            headers=_bearer(token),
            json={
                "name": "mf-agent2",
                "endpoint_url": "http://agent:9100",
                "description_for_router": "x",
                "base_model_id": model.id,
                "runtime_type": "langchain",
                "manifest": _valid_manifest(),
            },
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["runtime_type"] == "langchain"
        assert data["manifest_json"]["agent_id"] == "risk-agent"


# ── Registration defaults (OE-1 three-state) ────────────────────────────────


class TestRegistrationDefaults:
    def test_shadow_flag_ignored_lands_registered(self, client, db):
        """shadow/draft construct retired: flag is accepted but ignored."""
        make_user(db, username="sh_dev", role="developer")
        model = make_model(db, name="sh-model")
        token = login(client, "sh_dev")
        resp = client.post(
            "/api/agents/register",
            headers=_bearer(token),
            json={
                "name": "shadow-agent",
                "endpoint_url": "http://agent:9100",
                "description_for_router": "legacy inventory row",
                "base_model_id": model.id,
                "shadow": True,
            },
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["approval_status"] == "registered"

    def test_default_register_is_registered(self, client, db):
        make_user(db, username="df_dev", role="developer")
        model = make_model(db, name="df-model")
        token = login(client, "df_dev")
        resp = client.post(
            "/api/agents/register",
            headers=_bearer(token),
            json={
                "name": "default-agent",
                "endpoint_url": "http://agent:9100",
                "description_for_router": "x",
                "base_model_id": model.id,
            },
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["approval_status"] == "registered"
        assert data["runtime_type"] == "openai_compatible_agent"


# ── Approval state machine (OE-1: no gate ceremony) ─────────────────────────


class TestApprovalStateMachine:
    def test_approve_registered_without_trace_test(self, client, db):
        """OE-1: registered → approved with no ceremony."""
        dev = make_user(db, username="ap_dev", role="developer")
        admin = make_user(db, username="ap_admin", role="admin")
        agent = make_agent(
            db, dev, name="ap-no-trace", approval_status="registered"
        )
        token = login(client, "ap_admin")
        resp = client.post(
            f"/api/agents/{agent.id}/approve", headers=_bearer(token)
        )
        assert resp.status_code == 200, resp.text
        db.refresh(agent)
        assert agent.approval_status == "approved"
        assert agent.approved_by == admin.id

    def test_approve_reenables_disabled(self, client, db):
        dev = make_user(db, username="ap_dev2", role="developer")
        admin = make_user(db, username="ap_admin2", role="admin")
        agent = make_agent(
            db, dev, name="ap-disabled", approval_status="disabled"
        )
        token = login(client, "ap_admin2")
        resp = client.post(
            f"/api/agents/{agent.id}/approve", headers=_bearer(token)
        )
        assert resp.status_code == 200, resp.text
        db.refresh(agent)
        assert agent.approval_status == "approved"
        assert agent.approved_by == admin.id

    def test_already_approved_is_idempotent(self, client, db):
        dev = make_user(db, username="ap_dev4", role="developer")
        make_user(db, username="ap_admin4", role="admin")
        agent = make_agent(db, dev, name="ap-done", approval_status="approved")
        token = login(client, "ap_admin4")
        resp = client.post(
            f"/api/agents/{agent.id}/approve", headers=_bearer(token)
        )
        assert resp.status_code == 200
        assert "已是核准狀態" in resp.json()["message"]
