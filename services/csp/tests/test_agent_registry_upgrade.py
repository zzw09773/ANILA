# -*- coding: utf-8 -*-
"""Slice 5a — Agent Registry 升級測試(doc 05 §3/§4/§6、doc 06 §8)。

涵蓋:
- Manifest 契約(AgentManifest)fail-closed 驗證(pass / 未知欄位 / 非五級分類 /
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

import asyncio
import importlib.util
import os
from pathlib import Path

os.environ.setdefault("ANILA_ALLOW_DEV_SECRET", "1")

from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy.orm import Session, sessionmaker

from app.api.agents import health
from app.main import app as csp_app
from app.models.agent import Agent
from app.models.clearance import ClearanceGrant
from app.models.task import Task, TaskRun
from app.models.trace_span import TraceSpan
from app.schemas.contracts.agents import AgentManifest, RuntimeType
from app.services import agent_credential_service
from app.services import startup_security
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
        bad["classification"]["ceiling"] = "絕密"  # not a 五級 value
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
    def test_trace_poll_waits_for_all_required_lifecycle_types(self):
        """A first callback span must not short-circuit the bounded poll."""

        from unittest.mock import Mock

        first = TraceSpan(span_type="agent.run.started")
        complete = [
            TraceSpan(span_type="agent.run.started"),
            TraceSpan(span_type="agent.model_call.started"),
            TraceSpan(span_type="agent.model_call.finished"),
            TraceSpan(span_type="agent.output.started"),
            TraceSpan(span_type="agent.output.finished"),
            TraceSpan(span_type="agent.run.finished"),
        ]
        db = Mock()
        db.query.return_value.filter.return_value.all.side_effect = [
            [first], complete
        ]
        rows = asyncio.run(
            health._poll_trace_spans(
                db, "trace-poll", timeout_s=1.0, interval_s=0.0
            )
        )
        assert len(rows) == 6
        assert db.rollback.call_count == 2

    def test_trace_emitter_posts_to_csp_asgi_and_poll_reads_lifecycle(
        self, client, db, db_engine, monkeypatch
    ):
        """Exercise the real Agent emitter → CSP ASGI ingest boundary.

        The fixture deliberately does not insert ``trace_spans`` rows.  The
        worktree Agent ``TraceEmitter`` POSTs its six pass-blocking lifecycle
        events to the actual CSP application through ASGITransport; the rows
        are then read back through both the DB and the owner-protected trace
        GET endpoint.
        """

        clearance_issuer = make_user(
            db, username="tt_asgi_clearance_issuer", role="admin"
        )
        owner = make_user(db, username="tt_asgi_owner", role="developer")
        now = datetime.now(timezone.utc)
        db.add(
            ClearanceGrant(
                subject_user_id=owner.id,
                max_classification_level="無機密",
                valid_from=now - timedelta(minutes=1),
                expires_at=now + timedelta(hours=1),
                basis_ticket="trace-test-asgi-fixture",
                issued_by_user_id=clearance_issuer.id,
            )
        )
        db.commit()
        agent = make_agent(
            db, owner, name="tt-asgi-agent", approval_status="pending_trace_test"
        )
        _issue_cred(db, agent, owner)
        task = Task(
            title="ASGI trace-test",
            task_type="query",
            requester_user_id=owner.id,
            status="running",
            classification_level="無機密",
            legacy_runtime_call=False,
            trace_id="tracetest-asgi-callback",
        )
        db.add(task)
        db.flush()
        db.add(
            TaskRun(
                task_id=task.id,
                run_sequence=1,
                dispatch_target="agent",
                status="running",
                started_at=datetime.now(timezone.utc),
                classification_level="無機密",
            )
        )
        db.commit()
        db.refresh(task)
        token = agent_credential_service.get_active_plaintext_for_agent(
            db, agent_id=agent.id
        )
        assert token

        # Load the Agent implementation from this worktree explicitly.  The
        # developer environment may have an editable D:\ANILA install, which
        # must not make this integration test exercise a different checkout.
        tracing_path = (
            Path(__file__).resolve().parents[3]
            / "packages"
            / "anila-agent"
            / "anila_agent"
            / "tracing.py"
        )
        spec = importlib.util.spec_from_file_location(
            "_gate5_worktree_anila_tracing", tracing_path
        )
        assert spec and spec.loader
        tracing = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(tracing)

        real_async_client = httpx.AsyncClient

        class _CspAsgiClient:
            def __init__(self, *args, **kwargs):
                del args
                kwargs.pop("verify", None)
                self._client = real_async_client(
                    transport=httpx.ASGITransport(app=csp_app),
                    base_url="http://csp.local",
                    **kwargs,
                )

            async def __aenter__(self):
                await self._client.__aenter__()
                return self

            async def __aexit__(self, *exc):
                return await self._client.__aexit__(*exc)

            async def post(self, url, *, headers=None, json=None):
                return await self._client.post(url, headers=headers, json=json)

        # TraceEmitter imports httpx lazily at flush time.  Patching the module
        # only for this test routes its real POST through CSP ASGITransport;
        # no fake DB writer or direct TraceSpan insert is involved.
        monkeypatch.setattr(httpx, "AsyncClient", _CspAsgiClient)
        emitter = tracing.TraceEmitter(
            trace_id=task.trace_id,
            endpoint="http://csp.local",
            api_key=token,
            agent_id=agent.name,
            task_id=task.id,
            user_id=owner.username,
        )

        async def _emit() -> None:
            async with emitter.run_span(agent.name):
                async with emitter.span(tracing.MODEL_CALL, "model"):
                    pass
                async with emitter.span(tracing.OUTPUT, "output"):
                    pass
            await emitter.flush()

        asyncio.run(_emit())

        # _poll_trace_spans returns only once all six required lifecycle types
        # are present (or at timeout); this proves the callback batch is what
        # made the trace-test evidence visible.
        rows = asyncio.run(
            health._poll_trace_spans(
                db,
                task.trace_id,
                timeout_s=0.1,
                interval_s=0.01,
            )
        )
        assert {
            "agent.run.started",
            "agent.model_call.started",
            "agent.model_call.finished",
            "agent.output.started",
            "agent.output.finished",
            "agent.run.finished",
        } <= {row.span_type for row in rows}
        assert all(row.task_id == task.id for row in rows)

        db.expire_all()
        stored = db.query(TraceSpan).filter(TraceSpan.trace_id == task.trace_id).all()
        assert len(stored) == 6
        owner_token = login(client, owner.username)
        response = client.get(
            f"/api/traces/{task.trace_id}", headers=_bearer(owner_token)
        )
        assert response.status_code == 200, response.text
        assert len(response.json()["spans"]) == 6

    def test_signed_pilot_denies_unapproved_trace_test_before_outbound(
        self, client, db, monkeypatch
    ):
        dev = make_user(db, username="tt_pilot_dev", role="developer")
        admin = make_user(db, username="tt_pilot_admin", role="admin")
        agent = make_agent(
            db, dev, name="tt-pilot-denied", approval_status="pending_trace_test"
        )
        _issue_cred(db, agent, admin)
        monkeypatch.setattr(health.settings, "ANILA_PILOT_MODE", True)
        now = datetime.now(timezone.utc)
        monkeypatch.setattr(
            startup_security,
            "_verified_pilot_admission",
            type(
                "Admission",
                (),
                {
                    "valid_from": now - timedelta(minutes=1),
                    "valid_until": now + timedelta(minutes=1),
                    "enabled_callsites": frozenset({"csp.chat_model"}),
                },
            )(),
        )

        class _NoOutbound:
            def __init__(self, *args, **kwargs):
                raise AssertionError("trace-test reached outbound client")

        monkeypatch.setattr(health.httpx, "AsyncClient", _NoOutbound)
        token = login(client, dev.username)

        response = client.post(
            f"/api/agents/{agent.id}/trace-test", headers=_bearer(token)
        )

        assert response.status_code == 403
        assert "pilot" in response.json()["detail"].lower()
        assert "csp.agent_trace_test" in response.json()["detail"]

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
