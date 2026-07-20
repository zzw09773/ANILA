"""Service-wrapper Silver vertical checks: task replay, history and cancel."""

from __future__ import annotations

import importlib
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from anila_agent.runtime.task import FileTaskStore, TaskStatus, request_digest

pytestmark = pytest.mark.unit

# CSP mints ``csk-`` + ``secrets.token_urlsafe(32)`` (43 base64url chars).
_VALID_CSK = f"csk-{'A' * 43}"


def _dispatch_headers(module, *, task_id: str, token: str = "", **overrides: str) -> dict[str, str]:
    """Build the full CSP-owned Silver correlation binding for local tests."""

    assert module._ADMISSION is not None
    nonce = uuid.uuid4().hex
    headers = {
        "X-CSP-Service-Token": token,
        "X-ANILA-Agent-Id": module._ADMISSION.manifest.agent_id,
        "X-ANILA-Task-Id": task_id,
        "X-ANILA-Run-Id": f"run-{nonce}",
        "X-ANILA-Session-Id": f"session-{nonce}",
        "X-ANILA-Invocation-Id": f"inv-{nonce}",
        "X-ANILA-Trace-Id": f"trace-{nonce}",
        # HTTP headers are ASCII; the service URL-decodes this canonical
        # classification wire value before contract validation.
        "X-ANILA-Classification-Level": "%E7%84%A1%E6%A9%9F%E5%AF%86",
        "X-ANILA-Idempotency-Key": f"idem-{nonce}",
    }
    headers.update(overrides)
    return headers


@pytest.fixture
def service(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("ANILA_HOME", str(tmp_path / "agent-home"))
    monkeypatch.delenv("ANILA_BASE_URL", raising=False)
    monkeypatch.delenv("CSP_SERVICE_TOKEN", raising=False)
    monkeypatch.setenv("ANILA_ALLOW_NO_SERVICE_TOKEN", "1")
    from anila_agent.serving import service_wrapper

    module = importlib.reload(service_wrapper)
    monkeypatch.setattr(module, "COLLECTION_ID", 12)
    monkeypatch.setattr(module, "ALLOW_NO_SERVICE_TOKEN", True)
    monkeypatch.setattr(module, "CSP_SEARCH_TOKEN", "csk-test")
    monkeypatch.setattr(module, "build_model", lambda *args, **kwargs: object())
    monkeypatch.setattr(module, "build_agent", lambda *args, **kwargs: object())
    return module, tmp_path


def test_service_task_idempotency_history_and_step_events(service, monkeypatch):
    module, tmp_path = service
    calls = 0

    class _Result:
        final_output = "service answer"

    async def _run_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        assert kwargs.get("session") is not None
        return _Result()

    monkeypatch.setattr(module, "run_once", _run_once)
    payload = {
        "model": "anila-agent",
        "messages": [
            {"role": "user", "content": "service history"},
        ],
    }
    with TestClient(module.app) as client:
        headers = _dispatch_headers(
            module,
            task_id="task-service-1",
            **{
                "X-ANILA-Session-Id": "session-service-1",
                "X-ANILA-Run-Id": "run-service-1",
                "X-ANILA-Invocation-Id": "inv-service-1",
                "X-ANILA-Idempotency-Key": "idem-service-1",
            },
        )
        first = client.post("/v1/chat/completions", json=payload, headers=headers)
        replay = client.post("/v1/chat/completions", json=payload, headers=headers)
    assert first.status_code == 200
    assert replay.status_code == 200
    assert replay.json()["choices"][0]["message"]["content"] == "service answer"
    assert calls == 1

    record = FileTaskStore(tmp_path / "agent-home" / "tasks").get("task-service-1")
    assert record.status is TaskStatus.COMPLETED
    assert record.run_id == "run-service-1"
    assert record.invocation_id == "inv-service-1"
    assert record.history[0]["role"] == "user"
    assert record.history[-1] == {"role": "assistant", "content": "service answer"}
    assert record.events
    assert {event["schema_version"] for event in record.events} == {"step-event/v1"}


def test_service_stream_and_cancel_endpoint(service, monkeypatch):
    module, _tmp_path = service

    class _Stream:
        context_wrapper = SimpleNamespace(usage=None)

        async def stream_events(self):
            if False:
                yield None

        def cancel(self, mode: str = "immediate") -> None:
            return None

    monkeypatch.setattr(module, "run_streamed", lambda *args, **kwargs: _Stream())
    payload = {
        "model": "anila-agent",
        "messages": [{"role": "user", "content": "stream"}],
        "stream": True,
    }
    with TestClient(module.app) as client:
        headers = _dispatch_headers(
            module,
            task_id="task-stream-1",
            **{"X-ANILA-Idempotency-Key": "idem-stream-1"},
        )
        response = client.post("/v1/chat/completions", json=payload, headers=headers)
        assert response.status_code == 200
        assert response.text.rstrip().endswith("data: [DONE]")

        store = module._TASK_STORE
        assert store is not None
        running = store.create(
            task_id="task-cancel-1",
            session_id="session-cancel-1",
            invocation_id="inv-cancel-1",
            idempotency_key="idem-cancel-1",
            request_hash=request_digest("hash-cancel-1"),
        )
        running.status = TaskStatus.RUNNING
        store.save(running)
        cancelled = client.post(
            "/v1/tasks/task-cancel-1/cancel",
            headers={"X-CSP-Service-Token": ""},
        )
    assert cancelled.status_code == 200
    assert cancelled.json() == {"task_id": "task-cancel-1", "status": "cancelled"}


def test_no_token_profile_never_advertises_or_accepts_resume(service) -> None:
    module, _tmp_path = service
    with TestClient(module.app) as client:
        manifest = client.get("/.well-known/anila-agent.json")
        assert manifest.status_code == 200
        assert manifest.json()["supports_resume"] is False
        rejected = client.post("/v1/tasks/task-none/approve")
    assert rejected.status_code == 503


def test_gate5_approval_toggle_rejects_production_startup(
    service, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The disposable HITL switch cannot be enabled by a production ambient env."""

    module, _tmp_path = service
    monkeypatch.setenv("ANILA_E2E_REQUIRE_TOOL_APPROVAL", "1")
    monkeypatch.setenv("ANILA_DEPLOYMENT_PROFILE", "production")
    monkeypatch.setenv("ANILA_E2E_HARNESS", "gate5-silver")
    with pytest.raises(module.AgentAdmissionError, match="test-only"), TestClient(module.app):
        pass


@pytest.mark.parametrize(
    "token",
    [
        "dev-service-token",
        "changeme",
        "not-set",
        f"sk-{'A' * 43}",
        "csk-too-short",
        f"csk-{'A' * 42}",
        f"csk-{'A' * 44}",
        f"csk-{'A' * 42}*",
    ],
)
def test_placeholder_or_malformed_token_fails_startup_even_with_local_opt_out(
    service, monkeypatch: pytest.MonkeyPatch, token: str
) -> None:
    """An explicit bad token cannot be masked by local no-token development."""

    module, _tmp_path = service
    monkeypatch.setattr(module, "CSP_SERVICE_TOKEN", token)
    monkeypatch.setattr(module, "ALLOW_NO_SERVICE_TOKEN", True)
    with pytest.raises(module.AgentAdmissionError, match="CSP_SERVICE_TOKEN"), TestClient(module.app):
        pass
    # No readiness/admission state survives the rejected lifespan.
    assert module._ADMISSION is None
    assert module._TASK_STORE is None


def test_csp_approved_resume_survives_restart_and_is_exactly_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """R5: only CSP service auth may resume a persisted SDK interruption."""

    monkeypatch.setenv("ANILA_HOME", str(tmp_path / "agent-home"))
    monkeypatch.delenv("ANILA_BASE_URL", raising=False)
    monkeypatch.setenv("CSP_SERVICE_TOKEN", _VALID_CSK)
    monkeypatch.delenv("ANILA_ALLOW_NO_SERVICE_TOKEN", raising=False)
    from anila_agent.serving import service_wrapper

    module = importlib.reload(service_wrapper)
    monkeypatch.setattr(module, "COLLECTION_ID", 0)
    monkeypatch.setattr(module, "build_model", lambda *args, **kwargs: object())
    monkeypatch.setattr(module, "build_agent", lambda *args, **kwargs: SimpleNamespace(context=None, agent=object()))

    calls = {"initial": 0, "resume": 0}

    class _State:
        def to_string(self, **kwargs) -> str:
            # ``dump_state`` supplies the SDK context serializer so a real
            # AnilaRunContext never crosses the durable task boundary.
            assert kwargs["strict_context"] is True
            assert callable(kwargs["context_serializer"])
            return "durable-state-v1"

        def get_interruptions(self) -> list[str]:
            return ["approval-1"]

        def approve(self, item: str, *, always_approve: bool = False) -> None:
            assert item == "approval-1"
            assert always_approve is False

    class _PausedResult:
        final_output = ""
        interruptions = ("approval-1",)

        def to_state(self) -> _State:
            return _State()

    class _CompletedResult:
        final_output = "resumed answer"
        interruptions: tuple[object, ...] = ()

    async def _run_once(*args, **kwargs):
        calls["initial"] += 1
        return _PausedResult()

    async def _load_state(*args, **kwargs):
        return _State()

    async def _run_once_state(*args, **kwargs):
        calls["resume"] += 1
        return _CompletedResult()

    monkeypatch.setattr(module, "run_once", _run_once)
    monkeypatch.setattr(module, "load_state", _load_state)
    monkeypatch.setattr(module, "run_once_state", _run_once_state)

    payload = {"model": "anila-agent", "messages": [{"role": "user", "content": "pause me"}]}
    with TestClient(module.app) as first_service:
        assert module._ADMISSION is not None
        assert module._ADMISSION.manifest.supports_resume is True
        headers = _dispatch_headers(
            module,
            task_id="task-resume-1",
            token=_VALID_CSK,
            **{
                "X-ANILA-Run-Id": "run-resume-1",
                "X-ANILA-Session-Id": "session-resume-1",
                "X-ANILA-Invocation-Id": "inv-resume-1",
                "X-ANILA-Trace-Id": "trace-resume-1",
                "X-ANILA-Idempotency-Key": "idem-initial-resume-1",
            },
        )
        paused = first_service.post("/v1/chat/completions", json=payload, headers=headers)
        assert paused.status_code == 202
        assert paused.json()["status"] == "paused"
        malformed_resume = first_service.post(
            "/v1/tasks/task-resume-1/approve",
            content=b"{not-json",
            headers={**headers, "X-ANILA-Idempotency-Key": "idem-malformed"},
        )
        assert malformed_resume.status_code == 400
        legacy_fields = first_service.post(
            "/v1/tasks/task-resume-1/approve",
            json={"interrupt_id": "approval-1", "answer": "yes"},
            headers={**headers, "X-ANILA-Idempotency-Key": "idem-legacy-fields"},
        )
        assert legacy_fields.status_code == 400

    # A fresh lifespan instance must use the same FileTaskStore and can resume
    # a durable state produced before restart.
    resume_headers = dict(headers)
    resume_headers["X-ANILA-Idempotency-Key"] = "idem-resume-1"
    with TestClient(module.app) as restarted_service:
        assert restarted_service.get("/ready").status_code == 200
        bad_token = dict(resume_headers)
        bad_token["X-CSP-Service-Token"] = f"csk-{'B' * 43}"
        assert (
            restarted_service.post("/v1/tasks/task-resume-1/approve", headers=bad_token).status_code
            == 401
        )
        wrong_binding = dict(resume_headers)
        wrong_binding["X-ANILA-Agent-Id"] = "another-agent"
        assert (
            restarted_service.post(
                "/v1/tasks/task-resume-1/approve", headers=wrong_binding
            ).status_code
            == 403
        )
        resumed = restarted_service.post(
            "/v1/tasks/task-resume-1/approve", headers=resume_headers
        )
        replay = restarted_service.post(
            "/v1/tasks/task-resume-1/approve", headers=resume_headers
        )

    assert resumed.status_code == 200
    assert replay.status_code == 200
    assert resumed.json()["choices"][0]["message"]["content"] == "resumed answer"
    assert calls == {"initial": 1, "resume": 1}
    record = FileTaskStore(tmp_path / "agent-home" / "tasks").get("task-resume-1")
    assert record.status is TaskStatus.COMPLETED
    assert record.resume_idempotency_key == "idem-resume-1"
    assert [event["sequence"] for event in record.events] == sorted(
        event["sequence"] for event in record.events
    )
