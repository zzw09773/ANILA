"""Slice 2b-C — task_id wiring through the CSP data plane (/v1/chat/completions).

Locks the doc-04/05 contract:

- Inbound ``X-ANILA-Task-Id`` (optional) on ``/v1/chat/completions``:
  user caller → ``ensure_task_access``(404 unknown / 403 foreign);
  service-token caller (Router callback) → acting user resolved from the
  inbound ``X-ANILA-User-Id`` employee id, task.requester must match.
- Valid task → PolicyDecision(action="task.run", decision="allow") row +
  TaskRun(dispatch_target model/agent) started and finished
  (completed / failed) around the proxied call.
- Usage rows carry ``task_id`` when a task is present;
  ``legacy_runtime_call=true`` marks /v1 chat calls WITHOUT a task
  (doc 10 Slice 2 Done: 無 task_id 舊流量仍相容,但標記 legacy_runtime_call).
- Outbound headers (doc 05 §4): agent dispatch gains ``X-ANILA-Task-Id`` +
  ``X-ANILA-Trace-Id`` (trace id from the task row). Model gateway headers
  stay minimal per doc 04 §3/AC5 — Bearer key + ``X-ANILA-User-Id`` ONLY,
  never task/trace headers (regression lock).
"""

from __future__ import annotations

import asyncio
import os

# 同 house pattern(test_tasks_module.py 等):endpoint 測試會啟動 app,
# startup_security 在 production 模式擋 dev 預設 secret — 測試環境放行。
os.environ.setdefault("ANILA_ALLOW_DEV_SECRET", "1")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app.models.agent import UserAgentPermission
from app.models.api_key import ApiKeyModelPermission
from app.models.policy_decision import PolicyDecision
from app.models.task import Task, TaskRun
from app.models.token_usage import TokenUsage
from app.services import proxy_service
from app.services.auth_service import create_tokens
from app.services.proxy import service as proxy_impl
from app.services.proxy import task_link
from app.services.proxy_service import (
    build_agent_headers,
    build_model_gateway_headers,
)

from tests.conftest import make_agent, make_api_key, make_model, make_user

import dataclasses as _dataclasses

from app.services.proxy.service import ProxyTuning

#: 這一支只想跑一次上游、不想等重試的退避。重試策略是固定程式常數，
#: 測試用的 tuning 直接覆蓋為一次。
_PROXY_TUNING = _dataclasses.replace(
    ProxyTuning.from_registry_defaults(), max_retries=1, retry_base_delay=0.0
)



# ── Shared fixtures / helpers ───────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _dev_ssrf_allowances(monkeypatch):
    """Mock upstreams are single-label http hosts; allow them through the
    call-time SSRF guard exactly like test_proxy_stream_usage.py does."""
    monkeypatch.setenv("ANILA_ALLOW_HTTP_ENDPOINT", "1")
    monkeypatch.setenv("ANILA_TRUSTED_HOSTS", "mock-llm,agent")


@pytest.fixture
def task_sessions(monkeypatch, db_engine):
    """Route task_link's out-of-request session factory at the test engine
    (finalize_task_run runs outside the request-scoped dependency session)."""
    factory = sessionmaker(bind=db_engine, expire_on_commit=False)
    monkeypatch.setattr(task_link, "SessionLocal", factory)
    return factory


@pytest.fixture
def captured_usage(monkeypatch):
    """Capture task-linked usage payloads instead of the real async queue."""
    recorded: list[dict] = []

    async def _fake(**kwargs):
        recorded.append(kwargs)

    monkeypatch.setattr(proxy_impl, "enqueue_usage_task_linked", _fake)
    return recorded


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _jwt(user) -> str:
    return create_tokens(user)["access_token"]


def _make_task(db: Session, user, status: str = "submitted") -> Task:
    task = Task(
        title="測試任務",
        task_type="query",
        requester_user_id=user.id,
        status=status,
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    return task


class _PostResponse:
    def __init__(self, payload, status_code: int = 200):
        import json as _json

        self.status_code = status_code
        self.headers = {"content-type": "application/json"}
        self._payload = payload
        self.text = _json.dumps(payload)

    def json(self):
        return self._payload

    def raise_for_status(self):
        return None


class _PostClient:
    """Fake httpx.AsyncClient recording headers passed to .post()."""

    last_headers: dict = {}
    status_code: int = 200

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None, headers=None):
        type(self).last_headers = dict(headers or {})
        return _PostResponse(
            {
                "choices": [{"message": {"role": "assistant", "content": "hi"}}],
                "usage": {
                    "prompt_tokens": 3,
                    "completion_tokens": 4,
                    "total_tokens": 7,
                },
            },
            status_code=type(self).status_code,
        )


class _StreamResponse:
    def __init__(self, lines, status_code: int = 200):
        self._lines = lines
        self.status_code = status_code

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def aiter_lines(self):
        for line in self._lines:
            yield line


class _StreamClient:
    """Fake httpx.AsyncClient recording headers passed to .stream()."""

    last_headers: dict = {}

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def stream(self, method, url, json=None, headers=None):
        type(self).last_headers = dict(headers or {})
        return _StreamResponse(
            [
                'data: {"choices":[{"index":0,"delta":{"content":"hi"},'
                '"finish_reason":"stop"}],"usage":{"prompt_tokens":2,'
                '"completion_tokens":2,"total_tokens":4}}',
                "",
                "data: [DONE]",
                "",
            ]
        )


def _patch_post_client(monkeypatch, status_code: int = 200):
    _PostClient.last_headers = {}
    _PostClient.status_code = status_code
    monkeypatch.setattr(
        proxy_service.httpx, "AsyncClient", lambda *a, **k: _PostClient(*a, **k)
    )


def _patch_stream_client(monkeypatch):
    _StreamClient.last_headers = {}
    monkeypatch.setattr(
        proxy_service.httpx, "AsyncClient", lambda *a, **k: _StreamClient(*a, **k)
    )


# ── Happy path: user caller + task → run + policy row + usage link ─────────


class TestUserCallerWithTask:
    def test_model_target_creates_and_finishes_task_run(
        self, client: TestClient, db: Session, monkeypatch,
        task_sessions, captured_usage,
    ):
        admin = make_user(db, username="task_admin", role="admin")
        make_model(db, name="task-llm")
        task = _make_task(db, admin)
        _patch_post_client(monkeypatch)

        resp = client.post(
            "/v1/chat/completions",
            headers={**_bearer(_jwt(admin)), "X-ANILA-Task-Id": str(task.id)},
            json={"model": "task-llm",
                  "messages": [{"role": "user", "content": "hi"}]},
        )
        assert resp.status_code == 200, resp.text

        db.expire_all()
        runs = db.query(TaskRun).filter(TaskRun.task_id == task.id).all()
        assert len(runs) == 1
        assert runs[0].dispatch_target == "model"
        assert runs[0].status == "completed"

        decisions = (
            db.query(PolicyDecision)
            .filter(PolicyDecision.task_id == task.id)
            .all()
        )
        assert len(decisions) == 1
        assert decisions[0].action == "task.run"
        assert decisions[0].decision == "allow"
        assert decisions[0].resource_type == "model"
        assert decisions[0].actor_type == "user"

        assert len(captured_usage) == 1
        assert captured_usage[0]["task_id"] == task.id
        assert captured_usage[0]["legacy_runtime_call"] is False
        # No inbound X-ANILA-Trace-Id → usage attribution falls back to the
        # task row's trace id.
        assert captured_usage[0]["trace_id"] == task.trace_id

    def test_model_gateway_headers_never_carry_task_headers(
        self, client: TestClient, db: Session, monkeypatch,
        task_sessions, captured_usage,
    ):
        """doc 04 §3 / AC5:模型出向 header 僅 Bearer key + X-ANILA-User-Id,
        任何狀態都不得出現 task / trace header。"""
        admin = make_user(db, username="task_admin2", role="admin")
        make_model(db, name="task-llm2")
        task = _make_task(db, admin)
        _patch_post_client(monkeypatch)

        resp = client.post(
            "/v1/chat/completions",
            headers={**_bearer(_jwt(admin)), "X-ANILA-Task-Id": str(task.id)},
            json={"model": "task-llm2",
                  "messages": [{"role": "user", "content": "hi"}]},
        )
        assert resp.status_code == 200, resp.text
        h = _PostClient.last_headers
        assert "X-ANILA-Task-Id" not in h
        assert "X-ANILA-Trace-Id" not in h
        assert "X-CSP-Service-Token" not in h

    def test_agent_stream_forwards_task_and_trace_headers(
        self, client: TestClient, db: Session, monkeypatch,
        task_sessions, captured_usage,
    ):
        """doc 05 §4:agent dispatch 帶 X-ANILA-Task-Id + X-ANILA-Trace-Id
        (trace id 取自 task 列)。"""
        user = make_user(db, username="task_user_ag")
        dev = make_user(db, username="task_dev_ag", role="developer")
        agent = make_agent(db, dev, name="task-agent", approval_status="approved")
        db.add(UserAgentPermission(user_id=user.id, agent_id=agent.id))
        db.commit()
        task = _make_task(db, user)
        _patch_stream_client(monkeypatch)

        resp = client.post(
            "/v1/chat/completions",
            headers={**_bearer(_jwt(user)), "X-ANILA-Task-Id": str(task.id)},
            json={"model": "task-agent", "stream": True,
                  "messages": [{"role": "user", "content": "hi"}]},
        )
        assert resp.status_code == 200, resp.text
        _ = resp.text  # drain the SSE body so the stream finalizes

        h = _StreamClient.last_headers
        assert h.get("X-ANILA-Task-Id") == str(task.id)
        assert h.get("X-ANILA-Trace-Id") == task.trace_id

        db.expire_all()
        runs = db.query(TaskRun).filter(TaskRun.task_id == task.id).all()
        assert len(runs) == 1
        assert runs[0].dispatch_target == "agent"
        assert runs[0].status == "completed"
        decisions = (
            db.query(PolicyDecision)
            .filter(PolicyDecision.task_id == task.id)
            .all()
        )
        assert len(decisions) == 1
        assert decisions[0].resource_type == "agent"
        assert captured_usage and captured_usage[0]["task_id"] == task.id

    def test_upstream_failure_marks_run_failed(
        self, client: TestClient, db: Session, monkeypatch,
        task_sessions, captured_usage,
    ):
        admin = make_user(db, username="task_admin3", role="admin")
        make_model(db, name="task-llm3")
        task = _make_task(db, admin)
        _patch_post_client(monkeypatch, status_code=500)

        resp = client.post(
            "/v1/chat/completions",
            headers={**_bearer(_jwt(admin)), "X-ANILA-Task-Id": str(task.id)},
            json={"model": "task-llm3",
                  "messages": [{"role": "user", "content": "hi"}]},
        )
        assert resp.status_code == 502

        db.expire_all()
        runs = db.query(TaskRun).filter(TaskRun.task_id == task.id).all()
        assert len(runs) == 1
        assert runs[0].status == "failed"
        assert runs[0].error  # structured error payload recorded


# ── Legacy compat: no task header → unchanged + marked ─────────────────────


class TestLegacyNoTaskHeader:
    def test_no_header_marks_legacy_and_creates_no_run(
        self, client: TestClient, db: Session, monkeypatch,
        task_sessions, captured_usage,
    ):
        admin = make_user(db, username="legacy_admin", role="admin")
        make_model(db, name="legacy-llm")
        _patch_post_client(monkeypatch)

        resp = client.post(
            "/v1/chat/completions",
            headers=_bearer(_jwt(admin)),
            json={"model": "legacy-llm",
                  "messages": [{"role": "user", "content": "hi"}]},
        )
        assert resp.status_code == 200, resp.text

        assert len(captured_usage) == 1
        assert captured_usage[0]["legacy_runtime_call"] is True
        assert captured_usage[0]["task_id"] is None

        db.expire_all()
        assert db.query(TaskRun).count() == 0
        assert db.query(PolicyDecision).count() == 0


# ── Access control: unknown / foreign / malformed task ─────────────────────


class TestTaskAccessControl:
    def test_unknown_task_returns_404(
        self, client: TestClient, db: Session, monkeypatch,
        task_sessions, captured_usage,
    ):
        admin = make_user(db, username="acl_admin", role="admin")
        make_model(db, name="acl-llm")
        _patch_post_client(monkeypatch)

        resp = client.post(
            "/v1/chat/completions",
            headers={**_bearer(_jwt(admin)), "X-ANILA-Task-Id": "999999"},
            json={"model": "acl-llm",
                  "messages": [{"role": "user", "content": "hi"}]},
        )
        assert resp.status_code == 404
        db.expire_all()
        assert db.query(TaskRun).count() == 0

    def test_foreign_task_returns_403(
        self, client: TestClient, db: Session, monkeypatch,
        task_sessions, captured_usage,
    ):
        # Plain user (admin tier has a documented ensure_task_access bypass);
        # is_router_primary opens the model to every active user so the 403
        # can only come from the task ownership check.
        user = make_user(db, username="acl_plain")
        other = make_user(db, username="acl_other")
        model = make_model(db, name="acl-llm2")
        model.is_router_primary = True
        db.commit()
        foreign_task = _make_task(db, other)
        _patch_post_client(monkeypatch)

        resp = client.post(
            "/v1/chat/completions",
            headers={**_bearer(_jwt(user)),
                     "X-ANILA-Task-Id": str(foreign_task.id)},
            json={"model": "acl-llm2",
                  "messages": [{"role": "user", "content": "hi"}]},
        )
        assert resp.status_code == 403
        assert "任務" in resp.json()["detail"]
        db.expire_all()
        assert db.query(TaskRun).count() == 0

    def test_malformed_task_header_returns_400(
        self, client: TestClient, db: Session, monkeypatch,
        task_sessions, captured_usage,
    ):
        admin = make_user(db, username="acl_admin3", role="admin")
        make_model(db, name="acl-llm3")
        _patch_post_client(monkeypatch)

        resp = client.post(
            "/v1/chat/completions",
            headers={**_bearer(_jwt(admin)), "X-ANILA-Task-Id": "not-a-number"},
            json={"model": "acl-llm3",
                  "messages": [{"role": "user", "content": "hi"}]},
        )
        assert resp.status_code == 400


# ── Service-token caller (Router callback) ──────────────────────────────────


def _fake_identity():
    from app.services.agent_credential_service import CallerIdentity

    return CallerIdentity(
        kind="service_client",
        agent_id=None,
        service_client_id=7,
        credential_id=7,
        is_legacy=False,
        used_previous_token=False,
    )


@pytest.fixture
def service_caller(db, monkeypatch):
    """Router-style caller: sk- api key (service account) + verifiable
    X-CSP-Service-Token + forwarded employee id (acting user)."""
    from app.services import agent_credential_service

    svc = make_user(db, username="router_svc")
    key = make_api_key(db, svc, raw_key="sk-router-task-key")
    model = make_model(db, name="svc-llm")
    db.add(ApiKeyModelPermission(api_key_id=key.id, model_id=model.id))
    db.commit()

    def _fake_verify(_db, *, token):
        return _fake_identity() if token == "csk-router-test" else None

    monkeypatch.setattr(
        agent_credential_service, "verify_service_token", _fake_verify
    )
    return {"key": "sk-router-task-key", "model": "svc-llm"}


class TestServiceTokenCaller:
    def test_acting_user_resolved_from_employee_id(
        self, client: TestClient, db: Session, monkeypatch,
        task_sessions, captured_usage, service_caller,
    ):
        acting = make_user(db, username="123456")  # 員編-shape username
        task = _make_task(db, acting)
        _patch_post_client(monkeypatch)

        resp = client.post(
            "/v1/chat/completions",
            headers={
                **_bearer(service_caller["key"]),
                "X-CSP-Service-Token": "csk-router-test",
                "X-ANILA-User-Id": "123456",
                "X-ANILA-Task-Id": str(task.id),
            },
            json={"model": service_caller["model"],
                  "messages": [{"role": "user", "content": "hi"}]},
        )
        assert resp.status_code == 200, resp.text

        db.expire_all()
        runs = db.query(TaskRun).filter(TaskRun.task_id == task.id).all()
        assert len(runs) == 1
        assert runs[0].status == "completed"
        decisions = (
            db.query(PolicyDecision)
            .filter(PolicyDecision.task_id == task.id)
            .all()
        )
        assert len(decisions) == 1
        assert decisions[0].actor_type == "service"
        assert captured_usage and captured_usage[0]["task_id"] == task.id

    def test_requester_mismatch_returns_403(
        self, client: TestClient, db: Session, monkeypatch,
        task_sessions, captured_usage, service_caller,
    ):
        make_user(db, username="123456")
        other = make_user(db, username="654321")
        foreign_task = _make_task(db, other)
        _patch_post_client(monkeypatch)

        resp = client.post(
            "/v1/chat/completions",
            headers={
                **_bearer(service_caller["key"]),
                "X-CSP-Service-Token": "csk-router-test",
                "X-ANILA-User-Id": "123456",
                "X-ANILA-Task-Id": str(foreign_task.id),
            },
            json={"model": service_caller["model"],
                  "messages": [{"role": "user", "content": "hi"}]},
        )
        assert resp.status_code == 403
        db.expire_all()
        assert db.query(TaskRun).count() == 0

    def test_invalid_service_token_fails_closed_401(
        self, client: TestClient, db: Session, monkeypatch,
        task_sessions, captured_usage, service_caller,
    ):
        acting = make_user(db, username="123456")
        task = _make_task(db, acting)
        _patch_post_client(monkeypatch)

        resp = client.post(
            "/v1/chat/completions",
            headers={
                **_bearer(service_caller["key"]),
                "X-CSP-Service-Token": "csk-bogus",
                "X-ANILA-User-Id": "123456",
                "X-ANILA-Task-Id": str(task.id),
            },
            json={"model": service_caller["model"],
                  "messages": [{"role": "user", "content": "hi"}]},
        )
        assert resp.status_code == 401

    def test_missing_employee_id_fails_closed_403(
        self, client: TestClient, db: Session, monkeypatch,
        task_sessions, captured_usage, service_caller,
    ):
        acting = make_user(db, username="123456")
        task = _make_task(db, acting)
        _patch_post_client(monkeypatch)

        resp = client.post(
            "/v1/chat/completions",
            headers={
                **_bearer(service_caller["key"]),
                "X-CSP-Service-Token": "csk-router-test",
                "X-ANILA-Task-Id": str(task.id),
            },
            json={"model": service_caller["model"],
                  "messages": [{"role": "user", "content": "hi"}]},
        )
        assert resp.status_code == 403


# ── Header builder unit tests ───────────────────────────────────────────────


class TestHeaderBuilders:
    def test_agent_headers_carry_task_and_trace_ids(self):
        h = build_agent_headers(
            user_id=1,
            department=None,
            agent_id=7,
            task_id="42",
            trace_id="trace-abc",
        )
        assert h["Authorization"].startswith("Bearer ")
        assert h["X-ANILA-Task-Id"] == "42"
        assert h["X-ANILA-Trace-Id"] == "trace-abc"
        assert "X-CSP-Service-Token" not in h
        assert "X-ANILA-User-Id" not in h
        assert "X-ANILA-User-Email" not in h
        assert "X-ANILA-User-Groups" not in h

    def test_agent_headers_omit_task_headers_when_absent(self):
        h = build_agent_headers(user_id=1, department=None, agent_id=7)
        assert "X-ANILA-Task-Id" not in h
        assert "X-ANILA-Trace-Id" not in h
        assert h["Authorization"].startswith("Bearer ")

    def test_model_gateway_headers_have_no_task_or_trace_surface(self):
        """doc 04 AC5 regression lock — the model-gateway builder must not
        even expose a way to emit task / trace headers."""
        h = build_model_gateway_headers("1147259")
        assert "X-ANILA-Task-Id" not in h
        assert "X-ANILA-Trace-Id" not in h
        assert "X-CSP-Service-Token" not in h

    def test_proxy_stream_model_destination_drops_task_headers(self, monkeypatch):
        """Even when task ids are threaded through proxy_stream, a MODEL
        destination must not receive them (builder chosen by destination)."""
        _patch_stream_client(monkeypatch)

        async def _fake(**kwargs):
            return None

        monkeypatch.setattr(proxy_impl, "enqueue_usage_task_linked", _fake)
        monkeypatch.setattr(proxy_impl, "enqueue_usage", _fake)

        async def _run():
            async for _ in proxy_service.proxy_stream(
                target_url="http://mock-llm/v1/chat/completions",
                api_key_id=1,
                user_id=2,
                department_id=None,
                usage_model_id=3,
                request_body={"model": "m", "stream": True,
                              "messages": [{"role": "user", "content": "hi"}]},
                user_identity="1147259",
                model_name="m",
                target_agent_id=None,  # MODEL destination
                task_id=42,
                task_trace_id="trace-abc",
                tuning=_PROXY_TUNING,
            ):
                pass

        asyncio.run(_run())
        h = _StreamClient.last_headers
        assert "X-ANILA-Task-Id" not in h
        assert "X-ANILA-Trace-Id" not in h


# ── Usage enqueue helper writes real columns ────────────────────────────────


class TestUsageTaskLinkColumns:
    def test_enqueued_payload_flushes_into_token_usage_columns(
        self, db: Session, db_engine, monkeypatch,
    ):
        """The task-linked payload must bulk-insert cleanly into token_usage
        with the new task_id / legacy_runtime_call columns populated."""
        from app.services import usage_writer
        from app.services.proxy.usage import enqueue_usage_task_linked

        user = make_user(db, username="usage_link_user")
        model = make_model(db, name="usage-link-llm")
        task = _make_task(db, user)

        monkeypatch.setattr(
            usage_writer, "SessionLocal", sessionmaker(bind=db_engine, expire_on_commit=False)
        )
        # Fresh queue so we only see this test's payload.
        monkeypatch.setattr(usage_writer, "_usage_queue", None)

        async def _run():
            await enqueue_usage_task_linked(
                api_key_id=None,
                user_id=user.id,
                department_id=None,
                model_id=model.id,
                prompt_tokens=1,
                completion_tokens=2,
                total_tokens=3,
                task_id=task.id,
                legacy_runtime_call=False,
            )
            queue = usage_writer.get_usage_queue()
            batch = [queue.get_nowait()]
            await usage_writer._flush_batch(batch)

        asyncio.run(_run())

        db.expire_all()
        row = (
            db.query(TokenUsage)
            .filter(TokenUsage.user_id == user.id)
            .one()
        )
        assert row.task_id == task.id
        assert row.legacy_runtime_call is False
