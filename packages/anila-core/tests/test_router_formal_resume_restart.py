"""Gate 5 formal Router resume authority and restart-boundary tests."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from anila_core.api.router_server import (
    ACCESS_COOKIE_NAME,
    LEGACY_DISPATCH_OPT_IN_HEADER,
    LEGACY_RESUME_OPT_IN_HEADER,
    create_router_app,
)
from anila_core.config import settings
from anila_core.router.csp_registry_client import AgentClientError, CspAgentClient


CSP_BASE = settings.csp_base_url.rstrip("/")
CSP_ME_URL = f"{CSP_BASE}/api/auth/me"
CSP_RESUME_URL = f"{CSP_BASE}/internal/v1/agents/resume-by-session"


class _ResumeSpy:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def resume_by_session(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return {
            "content": "resumed",
            "status": "completed",
            "anila_meta": {"status": "completed"},
        }


class _ResumeErrorClient(_ResumeSpy):
    def __init__(self, status_code: int) -> None:
        super().__init__()
        self.status_code = status_code

    async def resume_by_session(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        raise AgentClientError("resume failed", status_code=self.status_code)


def _resume_headers(key: str = "resume-1") -> dict[str, str]:
    return {
        "Authorization": "Bearer public-caller-token",
        "X-ANILA-Idempotency-Key": key,
    }


@respx.mock
def test_formal_resume_survives_recreated_router_without_local_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALLOW_LEGACY_AGENT_DISPATCH", "0")
    respx.get(CSP_ME_URL).mock(return_value=httpx.Response(200, json={"id": 123}))
    spy = _ResumeSpy()

    app1 = create_router_app(agent_client=spy)
    first = TestClient(app1).post(
        "/v1/sessions/restart-safe/answer",
        headers=_resume_headers("resume-1"),
        json={"approval_mode": "approve_all"},
    )
    app2 = create_router_app(agent_client=spy)
    second = TestClient(app2).post(
        "/v1/sessions/restart-safe/answer",
        headers=_resume_headers("resume-2"),
        json={"approval_mode": "approve_all"},
    )

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert [call["session_id"] for call in spy.calls] == [
        "restart-safe",
        "restart-safe",
    ]


@respx.mock
def test_formal_resume_resolves_caller_and_does_not_forward_raw_spoof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALLOW_LEGACY_AGENT_DISPATCH", "0")
    respx.get(CSP_ME_URL).mock(return_value=httpx.Response(200, json={"id": 123}))
    captured: dict[str, Any] = {}

    def resume_handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(request.headers)
        captured["payload"] = json.loads(request.content.decode())
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"role": "assistant", "content": "ok"}}],
                "anila_meta": {"status": "completed"},
            },
            request=request,
        )

    respx.post(CSP_RESUME_URL).mock(side_effect=resume_handler)
    client = CspAgentClient(CSP_BASE, service_token="csk-router-primary")
    response = TestClient(create_router_app(agent_client=client)).post(
        "/v1/sessions/opaque-session/answer",
        headers={
            **_resume_headers(),
            "X-ANILA-Caller-User-Id": "999",
        },
        json={"approval_mode": "approve_all"},
    )

    assert response.status_code == 200, response.text
    assert captured["headers"]["x-anila-caller-user-id"] == "123"
    assert captured["payload"] == {
        "session_id": "opaque-session",
        "approval_mode": "approve_all",
    }
    assert all(
        field not in captured["payload"]
        for field in ("grant", "binding", "interrupt_id", "answer")
    )
    assert "authorization" not in captured["headers"]


@respx.mock
def test_formal_resume_auth_failure_happens_before_csp_resume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALLOW_LEGACY_AGENT_DISPATCH", "0")
    respx.get(CSP_ME_URL).mock(return_value=httpx.Response(401, json={"detail": "denied"}))
    spy = _ResumeSpy()
    response = TestClient(create_router_app(agent_client=spy)).post(
        "/v1/sessions/auth-failure/answer",
        headers=_resume_headers(),
        json={"approval_mode": "approve_all"},
    )

    assert response.status_code == 401
    assert spy.calls == []


@pytest.mark.parametrize(
    ("headers", "expected_status"),
    [
        ({"Authorization": "Bearer public-caller-token"}, 400),
        (
            [
                ("Authorization", "Bearer public-caller-token"),
                ("X-ANILA-Idempotency-Key", "duplicate-a"),
                ("X-ANILA-Idempotency-Key", "duplicate-b"),
            ],
            400,
        ),
        ({**_resume_headers("x" * 256)}, 400),
    ],
)
def test_formal_resume_requires_one_valid_canonical_idempotency_header(
    monkeypatch: pytest.MonkeyPatch,
    headers: dict[str, str] | list[tuple[str, str]],
    expected_status: int,
) -> None:
    monkeypatch.setenv("ALLOW_LEGACY_AGENT_DISPATCH", "0")
    response = TestClient(create_router_app(agent_client=_ResumeSpy())).post(
        "/v1/sessions/idempotency/answer",
        headers=headers,
        json={"approval_mode": "approve_all"},
    )
    assert response.status_code == expected_status


def test_legacy_flag_alone_does_not_downgrade_formal_resume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALLOW_LEGACY_AGENT_DISPATCH", "1")
    response = TestClient(create_router_app(agent_client=_ResumeSpy())).post(
        "/v1/sessions/formal-default/answer",
        headers={"Authorization": "Bearer public-caller-token"},
        json={"approval_mode": "approve_all"},
    )
    assert response.status_code == 400
    assert "Idempotency" in response.text


def test_legacy_dispatch_flag_alone_does_not_downgrade_formal_chat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALLOW_LEGACY_AGENT_DISPATCH", "1")
    response = TestClient(create_router_app(agent_client=_ResumeSpy())).post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer public-caller-token"},
        json={"messages": [{"role": "user", "content": "hello"}]},
    )
    assert response.status_code == 401
    assert "formal path" in response.text


def test_legacy_dispatch_opt_in_rejects_formal_authority_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALLOW_LEGACY_AGENT_DISPATCH", "1")
    response = TestClient(create_router_app(agent_client=_ResumeSpy())).post(
        "/v1/chat/completions",
        headers={
            "Authorization": "Bearer public-caller-token",
            LEGACY_DISPATCH_OPT_IN_HEADER: "1",
            "X-ANILA-Owner-Id": "123",
        },
        json={"messages": [{"role": "user", "content": "hello"}]},
    )
    assert response.status_code == 400
    assert "formal authority" in response.text


def test_legacy_resume_requires_explicit_opt_in_header(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("ALLOW_LEGACY_AGENT_DISPATCH", "1")
    app = create_router_app(
        session_db_path=str(tmp_path / "legacy-resume.db"),
        agent_client=_ResumeSpy(),
    )
    response = TestClient(app).post(
        "/v1/sessions/legacy-opt-in/answer",
        headers={
            "Authorization": "Bearer public-caller-token",
            LEGACY_RESUME_OPT_IN_HEADER: "1",
        },
        json={"interrupt_id": "i-1", "answer": "yes"},
    )
    assert response.status_code == 404
    assert "owning agent" in response.text


def test_legacy_and_formal_resume_headers_are_ambiguous(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALLOW_LEGACY_AGENT_DISPATCH", "1")
    response = TestClient(create_router_app(agent_client=_ResumeSpy())).post(
        "/v1/sessions/mixed-headers/answer",
        headers={
            **_resume_headers(),
            LEGACY_RESUME_OPT_IN_HEADER: "1",
        },
        json={"interrupt_id": "i-1", "answer": "yes"},
    )
    assert response.status_code == 400
    assert "legacy resume opt-in" in response.text


@respx.mock
def test_public_bearer_takes_precedence_over_cookie_for_caller_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALLOW_LEGACY_AGENT_DISPATCH", "0")
    seen: list[str] = []

    def resolve_caller(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("authorization", ""))
        return httpx.Response(200, json={"id": 123}, request=request)

    respx.get(CSP_ME_URL).mock(side_effect=resolve_caller)
    spy = _ResumeSpy()
    response = TestClient(create_router_app(agent_client=spy)).post(
        "/v1/sessions/auth-precedence/answer",
        headers={
            **_resume_headers(),
            "Cookie": f"{ACCESS_COOKIE_NAME}=cookie-token",
        },
        json={"approval_mode": "approve_all"},
    )

    assert response.status_code == 200
    assert seen == ["Bearer public-caller-token"]
    assert spy.calls[0]["caller_user_id"] == 123


def test_duplicate_authorization_headers_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALLOW_LEGACY_AGENT_DISPATCH", "0")
    headers = [
        ("Authorization", "Bearer first-token"),
        ("Authorization", "Bearer second-token"),
        ("X-ANILA-Idempotency-Key", "duplicate-auth"),
    ]
    spy = _ResumeSpy()
    response = TestClient(create_router_app(agent_client=spy)).post(
        "/v1/sessions/duplicate-auth/answer",
        headers=headers,
        json={"approval_mode": "approve_all"},
    )
    assert response.status_code == 400
    assert spy.calls == []


@pytest.mark.parametrize(
    ("csp_status", "expected_status"),
    [(401, 401), (409, 409), (503, 502)],
)
@respx.mock
def test_formal_resume_maps_csp_failures_safely(
    monkeypatch: pytest.MonkeyPatch,
    csp_status: int,
    expected_status: int,
) -> None:
    monkeypatch.setenv("ALLOW_LEGACY_AGENT_DISPATCH", "0")
    respx.get(CSP_ME_URL).mock(return_value=httpx.Response(200, json={"id": 123}))
    spy = _ResumeErrorClient(csp_status)
    response = TestClient(create_router_app(agent_client=spy)).post(
        "/v1/sessions/csp-failure/answer",
        headers=_resume_headers(),
        json={"approval_mode": "approve_all"},
    )
    assert response.status_code == expected_status
    assert len(spy.calls) == 1
