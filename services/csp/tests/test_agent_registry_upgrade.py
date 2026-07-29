# -*- coding: utf-8 -*-
"""Slice 5a — Agent Registry 升級測試(doc 05 §3/§4/§6、doc 06 §8)。

涵蓋:
- Manifest 契約(AgentManifest)fail-closed 驗證(pass / 未知欄位 / 非四級分類 /
  錯誤 trace protocol)+ 註冊端點整合(422 / 存 manifest_json)。
- Shadow 註冊(shadow=True → draft;預設 → pending_connection_test)。
- 七值狀態機 approve blocker(無 trace-test → 409;狀態不符 → 409;
  pending_security_review + 落章 → 200;already approved 冪等)。
- trace-test 端點:mock agent 以「直接寫 trace_spans」模擬 agent 回報 —— 全過
  轉態 pending_security_review + 落章 + 存報告;缺 span type → 逐項 fail;
  逾時 → fail;無憑證 → 409;非 owner → 403。

DB 用 SQLite(StaticPool 單連線);mock agent 的 span 由 db_engine 綁定的獨立
session commit,端點以有界輪詢讀回(同 test_trace_endpoints 的跨 session 姿態)。
"""

from __future__ import annotations

import os

os.environ.setdefault("ANILA_ALLOW_DEV_SECRET", "1")

from datetime import datetime, timezone

import httpx
import pytest
from sqlalchemy.orm import Session, sessionmaker

from app.api.agents import health
from app.models.agent import Agent
from app.models.trace_span import TraceSpan
from app.schemas.contracts.agents import AgentManifest, RuntimeType
from app.services import agent_credential_service
from tests.conftest import login, make_agent, make_model, make_user

pytestmark = pytest.mark.filterwarnings("ignore")

_CORE_SPAN_TYPES = (
    "agent.run.started",
    "agent.model_call.started",
    "agent.model_call.finished",
    "agent.output.started",
    "agent.output.finished",
    "agent.run.finished",
)


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
    # for register + trace-test outbound validation, as the dev stack does.
    monkeypatch.setenv("ANILA_ALLOW_HTTP_ENDPOINT", "1")
    monkeypatch.setenv("ANILA_ALLOW_PRIVATE_ENDPOINT", "1")
    monkeypatch.setenv("ANILA_TRUSTED_HOSTS", "agent,mock-llm")
    # Real (non-dev) secret so agent credential encrypt/decrypt round-trips
    # (get_active_plaintext_for_agent needs the same key it was issued with).
    monkeypatch.setenv("SECRET_KEY", "test-secret-key-" + "a" * 40)


# ── Fake agent endpoint (records trace_id, optionally emits spans) ───────────


class _FakeAgentClient:
    """Stand-in for ``httpx.AsyncClient`` used by the trace-test dispatch.

    ``emit`` (if given) is called with the outbound ``X-ANILA-Trace-Id`` so the
    test can write ``trace_spans`` rows that simulate the agent's Full Trace
    callback landing before the endpoint polls.
    """

    def __init__(self, *a, emit=None, status_code=200, **k):
        self._emit = emit
        self._status = status_code

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None, headers=None):
        if self._emit is not None:
            self._emit(headers["X-ANILA-Trace-Id"])
        return httpx.Response(self._status, json={"choices": []})


def _emitter(db_engine, span_types):
    """Return an emit(trace_id) that writes ``span_types`` as a single-rooted
    tree (span 0 = root, the rest parented to it)."""

    def _emit(trace_id: str) -> None:
        factory = sessionmaker(bind=db_engine)
        s = factory()
        try:
            for i, st in enumerate(span_types):
                s.add(
                    TraceSpan(
                        trace_id=trace_id,
                        span_id=f"sp{i}",
                        parent_span_id=None if i == 0 else "sp0",
                        span_type=st,
                        name=st,
                        status="ok",
                        producer="agent",
                    )
                )
            s.commit()
        finally:
            s.close()

    return _emit


def _patch_agent_client(monkeypatch, *, emit=None, status_code=200):
    monkeypatch.setattr(
        health.httpx,
        "AsyncClient",
        lambda *a, **k: _FakeAgentClient(*a, emit=emit, status_code=status_code, **k),
    )


def _issue_cred(db: Session, agent: Agent, admin) -> None:
    agent_credential_service.issue_static_credential(
        db, agent=agent, issuer=admin, label="trace-test"
    )
    db.commit()


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


# ── Shadow registration + defaults ──────────────────────────────────────────


class TestShadowRegistration:
    def test_shadow_register_creates_draft(self, client, db):
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
        assert resp.json()["approval_status"] == "draft"

    def test_default_register_is_pending_connection_test(self, client, db):
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
        assert data["approval_status"] == "pending_connection_test"
        assert data["runtime_type"] == "openai_compatible_agent"


# ── Approval state machine (7-value blocker) ────────────────────────────────


class TestApprovalStateMachine:
    def test_approve_blocked_without_trace_test(self, client, db):
        dev = make_user(db, username="ap_dev", role="developer")
        make_user(db, username="ap_admin", role="admin")
        agent = make_agent(
            db, dev, name="ap-no-trace", approval_status="pending_security_review"
        )
        token = login(client, "ap_admin")
        resp = client.post(
            f"/api/agents/{agent.id}/approve", headers=_bearer(token)
        )
        assert resp.status_code == 409
        assert "Full Trace" in resp.json()["detail"]

    def test_approve_blocked_wrong_state_even_with_stamp(self, client, db):
        dev = make_user(db, username="ap_dev2", role="developer")
        make_user(db, username="ap_admin2", role="admin")
        agent = make_agent(
            db, dev, name="ap-wrong-state", approval_status="pending_connection_test"
        )
        agent.trace_test_passed_at = datetime.now(timezone.utc)
        db.commit()
        token = login(client, "ap_admin2")
        resp = client.post(
            f"/api/agents/{agent.id}/approve", headers=_bearer(token)
        )
        assert resp.status_code == 409
        assert "安全審查" in resp.json()["detail"]

    def test_approve_allowed_at_review_gate_with_stamp(self, client, db):
        dev = make_user(db, username="ap_dev3", role="developer")
        admin = make_user(db, username="ap_admin3", role="admin")
        agent = make_agent(
            db, dev, name="ap-ok", approval_status="pending_security_review"
        )
        agent.trace_test_passed_at = datetime.now(timezone.utc)
        db.commit()
        token = login(client, "ap_admin3")
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


# ── Trace-test endpoint ─────────────────────────────────────────────────────


class TestTraceTest:
    def test_pass_transitions_state_and_stamps(
        self, client, db, db_engine, monkeypatch
    ):
        dev = make_user(db, username="tt_dev", role="developer")
        admin = make_user(db, username="tt_admin", role="admin")
        agent = make_agent(
            db, dev, name="tt-pass", approval_status="pending_trace_test"
        )
        _issue_cred(db, agent, admin)
        _patch_agent_client(
            monkeypatch, emit=_emitter(db_engine, _CORE_SPAN_TYPES)
        )
        token = login(client, "tt_dev")

        resp = client.post(
            f"/api/agents/{agent.id}/trace-test", headers=_bearer(token)
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["passed"] is True
        assert data["trace_id"].startswith("tracetest-")

        db.expire_all()
        refreshed = db.query(Agent).filter(Agent.id == agent.id).first()
        assert refreshed.approval_status == "pending_security_review"
        assert refreshed.trace_test_passed_at is not None
        assert refreshed.trace_test_report is not None

        # ...and approve now succeeds (blocker cleared, at the review gate).
        admin_token = login(client, "tt_admin")
        approve = client.post(
            f"/api/agents/{agent.id}/approve", headers=_bearer(admin_token)
        )
        assert approve.status_code == 200, approve.text

    def test_missing_span_types_fails_with_per_item_report(
        self, client, db, db_engine, monkeypatch
    ):
        dev = make_user(db, username="tt_dev2", role="developer")
        admin = make_user(db, username="tt_admin2", role="admin")
        agent = make_agent(
            db, dev, name="tt-partial", approval_status="pending_trace_test"
        )
        _issue_cred(db, agent, admin)
        # Only one span type — required set incomplete.
        _patch_agent_client(
            monkeypatch, emit=_emitter(db_engine, ["agent.run.started"])
        )
        token = login(client, "tt_dev2")

        resp = client.post(
            f"/api/agents/{agent.id}/trace-test", headers=_bearer(token)
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["passed"] is False
        items = {i["name"]: i for i in data["items"]}
        assert items["spans_received"]["status"] == "passed"
        assert items["required_span_types"]["status"] == "failed"
        assert items["required_span_types"]["required"] is True

        db.expire_all()
        refreshed = db.query(Agent).filter(Agent.id == agent.id).first()
        assert refreshed.trace_test_passed_at is None
        assert refreshed.approval_status == "pending_trace_test"

    def test_timeout_no_spans_fails(
        self, client, db, db_engine, monkeypatch
    ):
        dev = make_user(db, username="tt_dev3", role="developer")
        admin = make_user(db, username="tt_admin3", role="admin")
        agent = make_agent(
            db, dev, name="tt-timeout", approval_status="pending_trace_test"
        )
        _issue_cred(db, agent, admin)
        _patch_agent_client(monkeypatch, emit=None)  # agent emits nothing
        monkeypatch.setattr(health, "_TRACE_TEST_POLL_TIMEOUT_S", 0.3)
        monkeypatch.setattr(health, "_TRACE_TEST_POLL_INTERVAL_S", 0.05)
        token = login(client, "tt_dev3")

        resp = client.post(
            f"/api/agents/{agent.id}/trace-test", headers=_bearer(token)
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["passed"] is False
        items = {i["name"]: i for i in data["items"]}
        assert items["spans_received"]["status"] == "failed"

        db.expire_all()
        refreshed = db.query(Agent).filter(Agent.id == agent.id).first()
        assert refreshed.trace_test_passed_at is None

    def test_requires_credential_returns_409(self, client, db, monkeypatch):
        dev = make_user(db, username="tt_dev4", role="developer")
        agent = make_agent(
            db, dev, name="tt-nocred", approval_status="pending_trace_test"
        )
        _patch_agent_client(monkeypatch, emit=None)
        token = login(client, "tt_dev4")
        resp = client.post(
            f"/api/agents/{agent.id}/trace-test", headers=_bearer(token)
        )
        assert resp.status_code == 409
        assert "憑證" in resp.json()["detail"]

    def test_forbidden_for_non_owner(self, client, db, monkeypatch):
        dev = make_user(db, username="tt_owner", role="developer")
        make_user(db, username="tt_other", role="user")
        agent = make_agent(
            db, dev, name="tt-forbidden", approval_status="pending_trace_test"
        )
        _patch_agent_client(monkeypatch, emit=None)
        token = login(client, "tt_other")
        resp = client.post(
            f"/api/agents/{agent.id}/trace-test", headers=_bearer(token)
        )
        assert resp.status_code == 403
