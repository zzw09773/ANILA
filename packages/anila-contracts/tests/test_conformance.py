from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from anila_contracts import AgentError, StepEvent
from anila_contracts.errors import AGENT_ERROR_SCHEMA_VERSION
from anila_contracts.events import STEP_EVENT_SCHEMA_VERSION

FIXTURES = Path(__file__).parent / "fixtures"


def _fixture(name: str) -> dict[str, object]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    ("model", "fixture"),
    [(AgentError, "agent-error-v1.json"), (StepEvent, "step-event-v1.json")],
)
def test_frozen_fixture_round_trips_as_canonical_json(model, fixture: str) -> None:
    payload = _fixture(fixture)
    parsed = model.model_validate(payload)
    assert parsed.model_dump(mode="json", exclude_none=True) == payload
    assert model.model_validate_json(parsed.model_dump_json()).model_dump(
        mode="json", exclude_none=True
    ) == payload


@pytest.mark.parametrize(
    ("model", "fixture"),
    [(AgentError, "agent-error-v1.json"), (StepEvent, "step-event-v1.json")],
)
def test_unknown_fields_and_versions_fail_closed(model, fixture: str) -> None:
    payload = _fixture(fixture)
    with pytest.raises(ValidationError):
        model.model_validate({**payload, "untrusted_extension": True})
    with pytest.raises(ValidationError):
        model.model_validate({**payload, "schema_version": "future/v99"})


def test_step_event_rejects_unknown_kind_status_and_negative_counters() -> None:
    payload = _fixture("step-event-v1.json")
    for patch in (
        {"kind": "reasoning"},
        {"status": "succeeded"},
        {"sequence": -1},
        {"latency_ms": -1},
        {"retry_count": -1},
    ):
        with pytest.raises(ValidationError):
            StepEvent.model_validate({**payload, **patch})


def test_agent_error_schema_freezes_required_fields_and_error_codes() -> None:
    assert list(AgentError.model_fields) == [
        "schema_version",
        "code",
        "retryable",
        "safe_message",
        "trace_id",
        "invocation_id",
        "step_id",
    ]
    assert AgentError.model_fields["schema_version"].default == AGENT_ERROR_SCHEMA_VERSION
    schema = AgentError.model_json_schema()
    assert set(schema["required"]) == {
        "code",
        "retryable",
        "safe_message",
        "trace_id",
        "invocation_id",
    }
    assert schema["$defs"]["AgentErrorCode"]["enum"] == [
        "contract_error",
        "authentication_error",
        "authorization_error",
        "policy_denied",
        "classification_violation",
        "agent_unavailable",
        "timeout",
        "stream_broken",
        "cancelled",
        "downstream_error",
        "internal_error",
    ]


def test_step_event_schema_does_not_implicitly_nest_unrelated_contracts() -> None:
    assert list(StepEvent.model_fields) == [
        "schema_version",
        "event_id",
        "sequence",
        "cursor",
        "trace_id",
        "task_id",
        "session_id",
        "invocation_id",
        "run_id",
        "step_id",
        "parent_step_id",
        "depends_on",
        "kind",
        "status",
        "safe_input_summary",
        "safe_output_summary",
        "agent_id",
        "tool_name",
        "started_at",
        "completed_at",
        "latency_ms",
        "retry_count",
        "error",
        "classification",
    ]
    assert StepEvent.model_fields["schema_version"].default == STEP_EVENT_SCHEMA_VERSION
    schema = StepEvent.model_json_schema()
    schema_text = json.dumps(schema, ensure_ascii=False)
    assert "ClassificationLevel" in schema_text
    assert "AgentError" in schema_text
    definitions = set(schema.get("$defs", {}))
    for gate2_type in (
        "TaskContext",
        "TraceContext",
        "InvocationCommand",
        "SourceSnapshot",
        "SafeSummary",
    ):
        assert gate2_type not in definitions
