from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

import anila_contracts
from anila_contracts import (
    InvocationCommand,
    SafeSummary,
    SourceSnapshot,
    TaskContext,
    TraceContext,
)
from anila_contracts.contexts import (
    TASK_CONTEXT_SCHEMA_VERSION,
    TRACE_CONTEXT_SCHEMA_VERSION,
)
from anila_contracts.invocations import INVOCATION_COMMAND_SCHEMA_VERSION
from anila_contracts.sources import SOURCE_SNAPSHOT_SCHEMA_VERSION
from anila_contracts.summaries import SAFE_SUMMARY_SCHEMA_VERSION

FIXTURES = Path(__file__).parent / "fixtures"
CONTRACT_FIXTURES = (
    (TaskContext, "task-context-v1.json"),
    (TraceContext, "trace-context-v1.json"),
    (InvocationCommand, "invocation-command-v1.json"),
    (SourceSnapshot, "source-snapshot-v1.json"),
    (SafeSummary, "safe-summary-v1.json"),
)


def _fixture(name: str) -> dict[str, object]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.mark.parametrize(("model", "fixture"), CONTRACT_FIXTURES)
def test_gate2_fixture_round_trips_as_canonical_json(model, fixture: str) -> None:
    payload = _fixture(fixture)
    parsed = model.model_validate(payload)
    assert parsed.model_dump(mode="json", exclude_none=True) == payload
    assert model.model_validate_json(parsed.model_dump_json()).model_dump(
        mode="json", exclude_none=True
    ) == payload


@pytest.mark.parametrize(("model", "fixture"), CONTRACT_FIXTURES)
def test_gate2_contracts_reject_unknown_fields_and_versions(model, fixture: str) -> None:
    payload = _fixture(fixture)
    with pytest.raises(ValidationError):
        model.model_validate({**payload, "untrusted_extension": True})
    with pytest.raises(ValidationError):
        model.model_validate({**payload, "schema_version": "future/v99"})
    missing_version = dict(payload)
    missing_version.pop("schema_version")
    with pytest.raises(ValidationError, match="schema_version"):
        model.model_validate(missing_version)


def test_gate2_contracts_are_frozen() -> None:
    task = TaskContext.model_validate(_fixture("task-context-v1.json"))
    with pytest.raises(ValidationError, match="frozen"):
        task.task_id = 999  # type: ignore[misc]
    assert isinstance(task.collection_ids, tuple)


def test_task_context_has_no_caller_supplied_clearance_and_rejects_source_conflicts() -> None:
    payload = _fixture("task-context-v1.json")
    with pytest.raises(ValidationError, match="caller_clearance"):
        TaskContext.model_validate({**payload, "caller_clearance": "絕對機密"})

    no_scope = {**payload, "source_scope": "none"}
    with pytest.raises(ValidationError, match="source_scope=none"):
        TaskContext.model_validate(no_scope)

    missing_snapshot = {**payload, "source_snapshot_id": None}
    with pytest.raises(ValidationError, match="source_snapshot_id"):
        TaskContext.model_validate(missing_snapshot)

    explicit_no_source = {
        **payload,
        "source_scope": "none",
        "source_snapshot_id": 99,
        "collection_ids": [],
    }
    assert TaskContext.model_validate(explicit_no_source).source_snapshot_id == 99


def test_auth_assurance_requires_aware_time_and_unique_methods() -> None:
    payload = _fixture("task-context-v1.json")
    assurance = copy.deepcopy(payload["auth_assurance"])
    assert isinstance(assurance, dict)
    assurance["auth_time"] = "2026-07-12T01:00:00"
    with pytest.raises(ValidationError, match="timezone"):
        TaskContext.model_validate({**payload, "auth_assurance": assurance})

    assurance["auth_time"] = "2026-07-12T01:00:00Z"
    assurance["amr"] = ["sc", "sc"]
    with pytest.raises(ValidationError, match="amr"):
        TaskContext.model_validate({**payload, "auth_assurance": assurance})


def test_source_snapshot_rejects_incomplete_or_ambiguous_evidence() -> None:
    payload = _fixture("source-snapshot-v1.json")
    for patch in (
        {"document_versions": {"100": "generation-3"}},
        {"document_versions": None},
        {"content_hash": None},
        {"collection_ids": [11, 11]},
        {"document_ids": [100, 100]},
        {"content_hash": "A" * 64},
        {"content_hash": "0" * 63},
        {"created_at": "2026-07-12T01:02:03"},
    ):
        with pytest.raises(ValidationError):
            SourceSnapshot.model_validate({**payload, **patch})

    none_with_sources = {
        **payload,
        "origin": "none",
        "source_scope": "none",
    }
    with pytest.raises(ValidationError, match="origin=none"):
        SourceSnapshot.model_validate(none_with_sources)


def test_source_snapshot_accepts_an_explicit_no_source_record() -> None:
    payload = _fixture("source-snapshot-v1.json")
    no_source = {
        **payload,
        "origin": "none",
        "source_scope": "none",
        "collection_ids": [],
        "document_ids": [],
        "chunk_ids": [],
        "document_versions": None,
        "retrieval_queries": [],
        "content_hash": None,
        "payload_ref": None,
    }
    parsed = SourceSnapshot.model_validate(no_source)
    assert parsed.origin.value == "none"
    assert parsed.document_ids == ()
    assert parsed.document_versions is None

    no_source["document_versions"] = {}
    assert SourceSnapshot.model_validate(no_source).document_versions == {}

    with pytest.raises(ValidationError, match="content_hash"):
        SourceSnapshot.model_validate({**no_source, "content_hash": "0" * 64})


@pytest.mark.parametrize(
    ("path", "replacement"),
    [
        (("trace_context", "invocation_id"), "wrong-invocation"),
        (("trace_context", "task_id"), 999),
        (("trace_context", "run_id"), 999),
        (("trace_context", "session_id"), "wrong-session"),
        (("task_context", "source_snapshot_id"), 999),
        (("trace_context", "classification"), "極機密"),
        (("safe_input_summary", "classification"), "極機密"),
    ],
)
def test_invocation_command_rejects_authority_context_mismatches(
    path: tuple[str, str], replacement: object
) -> None:
    payload = _fixture("invocation-command-v1.json")
    nested = copy.deepcopy(payload[path[0]])
    assert isinstance(nested, dict)
    nested[path[1]] = replacement
    with pytest.raises(ValidationError, match="context 不一致"):
        InvocationCommand.model_validate({**payload, path[0]: nested})


def test_invocation_command_rejects_unknown_protocol_duplicate_tools_and_empty_input() -> None:
    payload = _fixture("invocation-command-v1.json")
    for patch in (
        {"event_protocol": "legacy-sse/v0"},
        {"allowed_tools": ["approved_search", "approved_search"]},
        {"input": {}},
        {"input": {"metrics": [1.0, float("nan")]}},
        {"input": {"metrics": [float("inf")]}},
        {"timeout_ms": 0},
        {"max_steps": 0},
    ):
        with pytest.raises(ValidationError):
            InvocationCommand.model_validate({**payload, **patch})


@pytest.mark.parametrize("nested_field", ["task_context", "trace_context", "safe_input_summary"])
def test_invocation_command_rejects_missing_nested_schema_version(nested_field: str) -> None:
    payload = _fixture("invocation-command-v1.json")
    nested = copy.deepcopy(payload[nested_field])
    assert isinstance(nested, dict)
    nested.pop("schema_version")
    with pytest.raises(ValidationError, match="schema_version"):
        InvocationCommand.model_validate({**payload, nested_field: nested})


def test_safe_summary_requires_bounded_text_strict_redaction_and_lowercase_hash() -> None:
    payload = _fixture("safe-summary-v1.json")
    for patch in (
        {"text": ""},
        {"text": "x" * 4097},
        {"redacted": False},
        {"redacted": "true"},
        {"policy_ids": []},
        {"policy_ids": ["policy-017", "policy-017"]},
        {"source_content_hash": "F" * 64},
    ):
        with pytest.raises(ValidationError):
            SafeSummary.model_validate({**payload, **patch})


def test_gate2_schema_versions_are_required_literals() -> None:
    expected = {
        TaskContext: TASK_CONTEXT_SCHEMA_VERSION,
        TraceContext: TRACE_CONTEXT_SCHEMA_VERSION,
        InvocationCommand: INVOCATION_COMMAND_SCHEMA_VERSION,
        SourceSnapshot: SOURCE_SNAPSHOT_SCHEMA_VERSION,
        SafeSummary: SAFE_SUMMARY_SCHEMA_VERSION,
    }
    for model, version in expected.items():
        assert model.model_fields["schema_version"].is_required()
        schema = model.model_json_schema()
        assert schema["properties"]["schema_version"]["const"] == version
        assert "schema_version" in schema["required"]
        assert schema["additionalProperties"] is False


def test_invocation_nested_fixtures_match_the_standalone_contract_fixtures() -> None:
    invocation = _fixture("invocation-command-v1.json")
    assert invocation["task_context"] == _fixture("task-context-v1.json")
    assert invocation["trace_context"] == _fixture("trace-context-v1.json")
    assert invocation["safe_input_summary"] == _fixture("safe-summary-v1.json")


def test_package_reports_gate2_v1_version() -> None:
    assert anila_contracts.__version__ == "1.0.0"
    pyproject = (Path(__file__).parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    assert 'version = "1.0.0"' in pyproject
