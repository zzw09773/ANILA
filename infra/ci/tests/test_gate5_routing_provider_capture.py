from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from anila_contracts import RouteDecision

from infra.ci import run_gate5_routing_contract as runner
from infra.ci.gate5_routing_provider_capture import (
    EXPECTED_PROVIDER_MODEL_ID,
    PROMPT_SHA256,
    PROMPT_VERSION,
    ProviderCaptureError,
    ROUTE_DECISION_EXAMPLE,
    SYSTEM_PROMPT,
    atomic_write_new,
    build_fixture_document,
    capture_outputs,
    chat_completions_url,
    request_sha256,
)


def _request() -> runner.RoutingRequest:
    case = {
        "id": "must-not-be-visible",
        "input": "route this visible request",
        "messages": [{"role": "user", "content": "route this visible request"}],
        "context": {
            "classification": "無機密",
            "capabilities": ["text"],
            "registry_snapshot_id": "snapshot-1",
        },
        "expected": {"secret_label": "DO_NOT_LEAK_EXPECTED"},
        "must_not_route_to": ["DO_NOT_LEAK_FORBIDDEN"],
    }
    return runner._request_from_case(case)


def test_capture_sends_only_visible_projection_and_preserves_raw_output() -> None:
    request = _request()
    seen: list[httpx.Request] = []
    raw_output = '  {"route_type":"clarify"}\n'

    def handler(http_request: httpx.Request) -> httpx.Response:
        seen.append(http_request)
        body = json.loads(http_request.content)
        serialized = json.dumps(body, ensure_ascii=False)
        assert "DO_NOT_LEAK_EXPECTED" not in serialized
        assert "DO_NOT_LEAK_FORBIDDEN" not in serialized
        assert "must-not-be-visible" not in serialized
        assert body["model"] == EXPECTED_PROVIDER_MODEL_ID
        assert body["temperature"] == 0
        assert body["max_tokens"] == 4096
        visible = json.loads(body["messages"][1]["content"])
        assert set(visible) == {"input", "messages", "context"}
        assert http_request.headers["authorization"] == "Bearer secret-token"
        return httpx.Response(
            200,
            json={
                "model": EXPECTED_PROVIDER_MODEL_ID,
                "choices": [
                    {
                        "message": {"content": raw_output},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 11,
                    "completion_tokens": 7,
                    "total_tokens": 18,
                },
            },
        )

    outputs = capture_outputs(
        [request],
        base_url="http://127.0.0.1:7000",
        model_id=EXPECTED_PROVIDER_MODEL_ID,
        api_key="secret-token",
        transport=httpx.MockTransport(handler),
    )

    assert len(seen) == 1
    assert outputs[0]["elapsed_ms"] >= 0
    actual = dict(outputs[0])
    actual.pop("elapsed_ms")
    assert actual == {
        "request_sha256": request_sha256(request),
        "provider_output": raw_output,
        "response_model_id": EXPECTED_PROVIDER_MODEL_ID,
        "finish_reason": "stop",
        "usage": {
            "prompt_tokens": 11,
            "completion_tokens": 7,
            "total_tokens": 18,
        },
        "output_sha256": hashlib.sha256(raw_output.encode()).hexdigest(),
    }
    fixture = build_fixture_document(
        dataset_identity={
            "dataset_id": "test",
            "dataset_version": "1",
            "dataset_sha256": "a" * 64,
            "case_count": 1,
        },
        model_id=EXPECTED_PROVIDER_MODEL_ID,
        captured_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        outputs=outputs,
    )
    assert fixture["prompt_version"] == PROMPT_VERSION
    assert fixture["prompt_sha256"] == PROMPT_SHA256
    assert "secret-token" not in json.dumps(fixture)


@pytest.mark.parametrize(
    ("base_url", "expected"),
    [
        ("http://127.0.0.1:7000", "http://127.0.0.1:7000/v1/chat/completions"),
        ("https://router.example/v1", "https://router.example/v1/chat/completions"),
    ],
)
def test_capture_url_accepts_ip_or_domain(base_url: str, expected: str) -> None:
    assert chat_completions_url(base_url) == expected


def test_frozen_prompt_carries_strict_route_decision_contract() -> None:
    validated = RouteDecision.model_validate(ROUTE_DECISION_EXAMPLE)

    assert validated.route_type.value == "single_agent"
    for field_name in RouteDecision.model_fields:
        assert f'"{field_name}"' in SYSTEM_PROMPT
    for snippet in (
        "direct_answer, single_agent, clarify, or deny",
        "direct_answer, clarify, deny, or null",
        "[A-Za-z0-9][A-Za-z0-9_.:/@-]*",
        "exactly copy the supplied",
        "selected_agent_id contained in candidate_agent_ids",
        "strict JSON integers",
        "Use 1..3 and 1..120000",
        "Never emit multi_agent_plan",
        "Never output",
    ):
        assert snippet in SYSTEM_PROMPT


def test_capture_duplicate_requests_and_transport_failures_fail_closed() -> None:
    request = _request()
    with pytest.raises(ProviderCaptureError, match="duplicate"):
        capture_outputs(
            [request, request],
            base_url="https://router.example",
            model_id=EXPECTED_PROVIDER_MODEL_ID,
            transport=httpx.MockTransport(lambda _: httpx.Response(200)),
        )

    def timeout(http_request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("synthetic timeout", request=http_request)

    with pytest.raises(ProviderCaptureError, match="transport"):
        capture_outputs(
            [request],
            base_url="https://router.example",
            model_id=EXPECTED_PROVIDER_MODEL_ID,
            transport=httpx.MockTransport(timeout),
        )


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(
            503, json={"model": EXPECTED_PROVIDER_MODEL_ID, "detail": "unavailable"}
        ),
        httpx.Response(200, json={"model": EXPECTED_PROVIDER_MODEL_ID, "choices": []}),
        httpx.Response(
            200,
            json={"model": EXPECTED_PROVIDER_MODEL_ID, "choices": [{"message": {}}]},
        ),
    ],
)
def test_capture_invalid_provider_response_fails_closed(
    response: httpx.Response,
) -> None:
    with pytest.raises(ProviderCaptureError):
        capture_outputs(
            [_request()],
            base_url="https://router.example",
            model_id=EXPECTED_PROVIDER_MODEL_ID,
            transport=httpx.MockTransport(lambda _: response),
        )


@pytest.mark.parametrize(
    "response",
    [
        {"choices": [{"message": {"content": "{}"}}]},
        {"model": "other-model", "choices": [{"message": {"content": "{}"}}]},
        {"model": "", "choices": [{"message": {"content": "{}"}}]},
    ],
)
def test_capture_requires_exact_response_model_provenance(
    response: dict[str, object],
) -> None:
    with pytest.raises(ProviderCaptureError, match="model"):
        capture_outputs(
            [_request()],
            base_url="https://router.example",
            model_id=EXPECTED_PROVIDER_MODEL_ID,
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json=response)),
        )


def test_capture_and_fixture_builder_reject_unapproved_model_id() -> None:
    with pytest.raises(ProviderCaptureError, match="approved provider model"):
        capture_outputs(
            [_request()],
            base_url="https://router.example",
            model_id="other-model",
            transport=httpx.MockTransport(
                lambda _: pytest.fail("must not call provider")
            ),
        )

    with pytest.raises(ProviderCaptureError, match="approved provider model"):
        build_fixture_document(
            dataset_identity={
                "dataset_id": "test",
                "dataset_version": "1",
                "dataset_sha256": "a" * 64,
                "case_count": 0,
            },
            model_id="other-model",
            captured_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            outputs=[],
        )


def test_atomic_fixture_write_refuses_overwrite(tmp_path: Path) -> None:
    target = tmp_path / "capture.json"
    atomic_write_new(target, {"value": "first"})
    first = target.read_bytes()
    with pytest.raises(ProviderCaptureError, match="overwrite"):
        atomic_write_new(target, {"value": "second"})
    assert target.read_bytes() == first
