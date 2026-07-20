"""Gate 4 live wiring tests for the CSP Agent SSE trust boundary."""

from __future__ import annotations

import json

import pytest
from anila_contracts import Classification, StepEvent

from app.services.proxy import service as proxy_service
from app.services.proxy.stream_bridge import MAX_EVENT_BYTES, StreamValidator


class _StreamResponse:
    status_code = 200

    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def aiter_bytes(self, chunk_size: int | None = None):
        assert chunk_size == 64 * 1024
        for offset in range(0, len(self._payload), 17):
            yield self._payload[offset : offset + 17]


def _install_stream(monkeypatch, blocks: list[str]) -> dict[str, object]:
    captured: dict[str, object] = {}
    payload = ("\n\n".join(blocks) + "\n\n").encode()

    class _StreamClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        def stream(self, method: str, url: str, *, json: dict, headers: dict):
            captured.update(method=method, url=url, body=json, headers=dict(headers))
            return _StreamResponse(payload)

    monkeypatch.setattr(
        proxy_service.httpx,
        "AsyncClient",
        lambda *_args, **_kwargs: _StreamClient(),
    )
    return captured


def _step(**updates) -> dict:
    event = StepEvent(
        event_id="upstream-event",
        sequence=1,
        cursor="1",
        trace_id="forged-trace",
        task_id="forged-task",
        session_id="forged-session",
        invocation_id="upstream-invocation",
        run_id="forged-run",
        step_id="tool:1",
        kind="tool",
        status="running",
        agent_id="forged-agent",
        tool_name="search_documents",
        safe_input_summary="read documents",
        classification=Classification.UNCLASSIFIED,
    ).model_dump(mode="json")
    event.update(updates)
    return event


def _event_payloads(chunks: list[str], event_name: str) -> list[dict]:
    payloads: list[dict] = []
    for chunk in chunks:
        if f"event: {event_name}\n" not in chunk:
            continue
        data = next(
            line[6:] for line in chunk.splitlines() if line.startswith("data: ")
        )
        payloads.append(json.loads(data))
    return payloads


@pytest.fixture(autouse=True)
def _isolate_outbound_dependencies(monkeypatch):
    monkeypatch.setattr(proxy_service, "_guard_outbound", lambda *_args, **_kw: None)

    async def _discard_usage(**_kwargs):
        return None

    monkeypatch.setattr(proxy_service, "enqueue_usage", _discard_usage)
    monkeypatch.setattr(proxy_service, "enqueue_usage_task_linked", _discard_usage)


async def _drain(*, target_agent_id: int | None, **overrides) -> list[str]:
    arguments = {
        "target_url": "http://mock-agent/v1/chat/completions",
        "api_key_id": 1,
        "user_id": 2,
        "department_id": None,
        "usage_model_id": 3,
        "request_body": {
            "model": "registered-agent",
            "messages": [{"role": "user", "content": "hello"}],
            "anila_session_id": "trusted-session",
        },
        "model_name": "registered-agent",
        "conversation_id": "trusted-conversation",
        "trace_id": "fallback-trace",
        "target_agent_id": target_agent_id,
    }
    arguments.update(overrides)
    return [
        chunk
        async for chunk in proxy_service._proxy_stream_impl(**arguments)
    ]


@pytest.mark.asyncio
async def test_agent_stream_rebinds_event_to_csp_context(monkeypatch) -> None:
    captured = _install_stream(
        monkeypatch,
        [
            "event: anila.step\ndata: " + json.dumps(_step()),
            "data: [DONE]",
        ],
    )
    builder_agent_ids: list[int] = []

    def _agent_headers(*_args, **kwargs):
        builder_agent_ids.append(kwargs["target_agent_id"])
        return {"Content-Type": "application/json"}

    monkeypatch.setattr(proxy_service, "build_agent_headers", _agent_headers)

    chunks = await _drain(
        target_agent_id=7,
        task_id=42,
        task_trace_id="trusted-trace",
        task_run_id=99,
        usage_capture={},
        admitted_classification_level="機密",
    )

    assert builder_agent_ids == [7]
    assert captured["headers"] == {
        "Content-Type": "application/json",
        "X-ANILA-Agent-Id": "7",
        "X-ANILA-Session-Id": "trusted-session",
        "X-ANILA-Run-Id": "99",
        "X-ANILA-Classification-Level": "機密",
    }
    payloads = _event_payloads(chunks, "anila.step")
    assert len(payloads) == 1
    assert {
        field: payloads[0][field]
        for field in (
            "task_id",
            "trace_id",
            "agent_id",
            "session_id",
            "run_id",
            "classification",
        )
    } == {
        "task_id": "42",
        "trace_id": "trusted-trace",
        "agent_id": "7",
        "session_id": "trusted-session",
        "run_id": "99",
        "classification": "機密",
    }


@pytest.mark.asyncio
async def test_agent_stream_drops_reasoning_secret_malformed_and_oversize(
    monkeypatch, caplog
) -> None:
    secret = "sk-abcdefghijklmnopqrstuv"
    unknown_payload = "unique-unknown-agent-event-payload"
    captured = _install_stream(
        monkeypatch,
        [
            'event: anila.reasoning\ndata: {"delta":"raw hidden reasoning"}',
            "event: anila.future\ndata: " + unknown_payload,
            "event: anila.step\ndata: "
            + json.dumps(_step(safe_output_summary=f"api_key={secret}")),
            "event: anila.step\ndata: not-json",
            "event: anila.step\ndata: " + "x" * (MAX_EVENT_BYTES + 1),
            "data: [DONE]",
        ],
    )
    monkeypatch.setattr(
        proxy_service,
        "build_agent_headers",
        lambda *_args, **_kwargs: {"Content-Type": "application/json"},
    )

    chunks = await _drain(target_agent_id=7)

    joined = "".join(chunks)
    assert _event_payloads(chunks, "anila.step") == []
    assert "anila.reasoning" not in joined
    assert "raw hidden reasoning" not in joined
    assert "anila.future" not in joined
    assert unknown_payload not in joined
    assert secret not in joined
    assert "not-json" not in joined
    assert unknown_payload not in caplog.text
    assert secret not in caplog.text
    assert captured["headers"]["X-ANILA-Agent-Id"] == "7"


@pytest.mark.asyncio
async def test_agent_stream_drops_event_flood_through_live_loop(monkeypatch) -> None:
    blocks = [
        "event: anila.step\ndata: "
        + json.dumps(_step(event_id=f"event-{index}", sequence=index, cursor=str(index)))
        for index in range(1, 5)
    ]
    blocks.append("data: [DONE]")
    _install_stream(monkeypatch, blocks)
    monkeypatch.setattr(
        proxy_service,
        "build_agent_headers",
        lambda *_args, **_kwargs: {"Content-Type": "application/json"},
    )
    monkeypatch.setattr(
        proxy_service,
        "StreamValidator",
        lambda context: StreamValidator(
            context,
            max_events_per_second=2,
            max_events_per_run=3,
            clock=lambda: 10.0,
        ),
    )

    chunks = await _drain(target_agent_id=7)

    assert len(_event_payloads(chunks, "anila.step")) == 2


@pytest.mark.asyncio
async def test_model_stream_has_no_agent_headers_or_validator(monkeypatch) -> None:
    captured = _install_stream(
        monkeypatch,
        [
            'event: anila.reasoning\ndata: {"delta":"model-owned reasoning"}',
            "data: [DONE]",
        ],
    )

    def _forbidden(*_args, **_kwargs):
        raise AssertionError("model stream must not enter the Agent trust boundary")

    monkeypatch.setattr(proxy_service, "build_agent_headers", _forbidden)
    monkeypatch.setattr(proxy_service, "StreamValidator", _forbidden)

    chunks = await _drain(
        target_agent_id=None,
        target_url="http://mock-model/v1/chat/completions",
        request_body={"model": "model", "messages": []},
        model_name="model",
        task_id=42,
        task_trace_id="trusted-trace",
        task_run_id=99,
        usage_capture={},
    )

    headers = captured["headers"]
    assert not any(name.startswith("X-ANILA-Agent-") for name in headers)
    assert "X-ANILA-Session-Id" not in headers
    assert "X-ANILA-Run-Id" not in headers
    assert "X-ANILA-Classification-Level" not in headers
    assert "X-ANILA-Task-Id" not in headers
    assert "X-ANILA-Trace-Id" not in headers
    assert "X-CSP-Service-Token" not in headers
    assert "event: anila.reasoning" in "".join(chunks)
