from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from anila_contracts import AgentManifest, ExecutionGrant, PolicyGateResult, RouteDecision
from anila_contracts.agents import ManifestCapabilities
from anila_contracts.grants import MAX_EXECUTION_GRANT_TTL

FIXTURES = Path(__file__).parent / "fixtures"
CONTRACT_FIXTURES = (
    (RouteDecision, "route-decision-v1.json"),
    (PolicyGateResult, "policy-gate-v1.json"),
    (AgentManifest, "agent-manifest-v1.json"),
    (ExecutionGrant, "execution-grant-v1.json"),
)


def _fixture(name: str) -> dict[str, object]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.mark.parametrize(("model", "fixture"), CONTRACT_FIXTURES)
def test_v2_fixtures_round_trip_as_canonical_json(model, fixture: str) -> None:
    payload = _fixture(fixture)
    parsed = model.model_validate(payload)
    assert parsed.model_dump(mode="json", exclude_none=True) == payload
    assert model.model_validate_json(parsed.model_dump_json()).model_dump(
        mode="json", exclude_none=True
    ) == payload


@pytest.mark.parametrize(("model", "fixture"), CONTRACT_FIXTURES)
def test_v2_unknown_fields_and_versions_fail_closed(model, fixture: str) -> None:
    payload = _fixture(fixture)
    with pytest.raises(ValidationError):
        model.model_validate({**payload, "untrusted_extension": True})
    with pytest.raises(ValidationError):
        model.model_validate({**payload, "schema_version": "future/v99"})
    missing = dict(payload)
    missing.pop("schema_version")
    with pytest.raises(ValidationError, match="schema_version"):
        model.model_validate(missing)


def test_v2_scalar_fields_are_strict_but_json_enums_and_times_are_wire_friendly() -> None:
    route = _fixture("route-decision-v1.json")
    with pytest.raises(ValidationError):
        RouteDecision.model_validate({**route, "confidence": "0.97"})
    bad_constraints = copy.deepcopy(route["constraints"])
    assert isinstance(bad_constraints, dict)
    bad_constraints["max_steps"] = 8.0
    with pytest.raises(ValidationError):
        RouteDecision.model_validate({**route, "constraints": bad_constraints})

    policy = _fixture("policy-gate-v1.json")
    with pytest.raises(ValidationError):
        PolicyGateResult.model_validate({**policy, "allowed": "true"})


def test_route_selected_agent_is_conditionally_required_and_bound_to_candidates() -> None:
    payload = _fixture("route-decision-v1.json")
    with pytest.raises(ValidationError, match="selected_agent_id"):
        RouteDecision.model_validate({**payload, "selected_agent_id": None})
    with pytest.raises(ValidationError, match="candidate_agent_ids"):
        RouteDecision.model_validate({**payload, "selected_agent_id": "other-agent"})

    for route_type in ("direct_answer", "clarify", "deny", "multi_agent_plan"):
        invalid = {
            **payload,
            "route_type": route_type,
            "selected_agent_id": "research-agent",
            "execution_plan": {"opaque": True} if route_type == "multi_agent_plan" else None,
        }
        with pytest.raises(ValidationError, match="非 single_agent"):
            RouteDecision.model_validate(invalid)


def test_route_execution_plan_is_only_an_opaque_multi_agent_representation() -> None:
    payload = _fixture("route-decision-v1.json")
    multi = {
        **payload,
        "route_type": "multi_agent_plan",
        "candidate_agent_ids": ["research-agent", "writer-agent"],
        "execution_plan": {"steps": [{"agent_id": "research-agent"}]},
        "selected_agent_id": None,
    }
    parsed = RouteDecision.model_validate(multi)
    assert parsed.execution_plan == {"steps": [{"agent_id": "research-agent"}]}

    with pytest.raises(ValidationError, match="只有 multi_agent_plan"):
        RouteDecision.model_validate({**payload, "execution_plan": {"steps": []}})


def test_policy_gate_never_reports_pending_approval_as_allowed() -> None:
    payload = _fixture("policy-gate-v1.json")
    with pytest.raises(ValidationError, match="approval_required"):
        PolicyGateResult.model_validate({**payload, "approval_required": True})
    denied = {
        **payload,
        "allowed": False,
        "approval_required": True,
        "reason_codes": ["APPROVAL_REQUIRED"],
    }
    assert PolicyGateResult.model_validate(denied).allowed is False


def test_policy_gate_binds_route_registry_and_optional_agent_authority() -> None:
    payload = _fixture("policy-gate-v1.json")
    for field in ("route_decision_id", "registry_snapshot_id"):
        missing = dict(payload)
        missing.pop(field)
        with pytest.raises(ValidationError, match=field):
            PolicyGateResult.model_validate(missing)

    with pytest.raises(ValidationError, match="reason_code"):
        PolicyGateResult.model_validate({**payload, "reason_codes": []})

    non_agent = dict(payload)
    non_agent.pop("target_agent_id")
    assert PolicyGateResult.model_validate(non_agent).target_agent_id is None

    with pytest.raises(ValidationError):
        PolicyGateResult.model_validate({**payload, "target_agent_id": " "})


def test_manifest_classification_and_model_binding_invariants() -> None:
    payload = _fixture("agent-manifest-v1.json")
    with pytest.raises(ValidationError, match="default"):
        AgentManifest.model_validate(
            {
                **payload,
                "classification": {"ceiling": "機密", "default": "極機密"},
            }
        )
    with pytest.raises(ValidationError, match=r"model_binding\.model_id"):
        AgentManifest.model_validate({**payload, "base_model_id": 99})

    for authority_field in (
        "approval_status",
        "health_status",
        "readiness",
        "trace_test_passed_at",
        "manifest_revision",
    ):
        with pytest.raises(ValidationError):
            AgentManifest.model_validate({**payload, authority_field: "untrusted"})

    legacy_capabilities = {
        **payload,
        "capabilities": {"retrieval": True, "tools": [], "streaming": True},
    }
    parsed = AgentManifest.model_validate(legacy_capabilities)
    assert isinstance(parsed.capabilities, ManifestCapabilities)

    # No-special-capability Agents are valid in either representation; the
    # dispatchable fields remain mandatory and non-empty.
    for capabilities in (
        [],
        {"retrieval": False, "tools": [], "streaming": False},
    ):
        empty_capability_manifest = {**payload, "capabilities": capabilities}
        assert AgentManifest.model_validate(empty_capability_manifest)
    for field in ("supported_task_types", "event_protocols", "required_scopes"):
        with pytest.raises(ValidationError):
            AgentManifest.model_validate({**payload, field: []})


def test_grant_binds_identity_target_model_classification_and_short_ttl() -> None:
    payload = _fixture("execution-grant-v1.json")
    parsed = ExecutionGrant.model_validate(payload)
    assert parsed.ttl_seconds == 240
    assert parsed.source_snapshot_id == 3

    with pytest.raises(ValidationError, match="TTL"):
        ExecutionGrant.model_validate(
            {
                **payload,
                "expires_at": "2026-07-15T00:05:01Z",
            }
        )
    with pytest.raises(ValidationError, match="晚於"):
        ExecutionGrant.model_validate({**payload, "expires_at": payload["issued_at"]})
    with pytest.raises(ValidationError, match="timezone"):
        ExecutionGrant.model_validate(
            {
                **payload,
                "issued_at": "2026-07-15T00:00:00",
                "expires_at": "2026-07-15T00:04:00",
            }
        )
    no_model = copy.deepcopy(payload)
    assert isinstance(no_model["target"], dict)
    no_model["target"].pop("model_binding")
    with pytest.raises(ValidationError, match="model_binding"):
        ExecutionGrant.model_validate(no_model)

    too_high = copy.deepcopy(payload)
    too_high["classification"] = "極機密"
    with pytest.raises(ValidationError, match="model binding ceiling"):
        ExecutionGrant.model_validate(too_high)

    for field in (
        "invocation_id",
        "route_decision_id",
        "policy_decision_id",
        "registry_snapshot_id",
    ):
        missing = dict(payload)
        missing.pop(field)
        with pytest.raises(ValidationError, match=field):
            ExecutionGrant.model_validate(missing)

    no_manifest_revision = dict(payload)
    no_manifest_revision.pop("manifest_revision")
    with pytest.raises(ValidationError, match="manifest_revision"):
        ExecutionGrant.model_validate(no_manifest_revision)

    non_agent = copy.deepcopy(payload)
    assert isinstance(non_agent["target"], dict)
    non_agent["target"]["kind"] = "service"
    non_agent.pop("manifest_revision")
    assert ExecutionGrant.model_validate(non_agent).target.kind.value == "service"
    non_agent["manifest_revision"] = "must-not-be-here"
    with pytest.raises(ValidationError, match="非 agent"):
        ExecutionGrant.model_validate(non_agent)


def test_grant_active_helper_uses_explicit_aware_time_and_half_open_interval() -> None:
    grant = ExecutionGrant.model_validate(_fixture("execution-grant-v1.json"))
    issued = datetime(2026, 7, 15, tzinfo=timezone.utc)
    expires = issued + timedelta(minutes=4)
    before = issued - timedelta(microseconds=1)

    assert grant.is_active_at(before) is False
    with pytest.raises(ValueError, match="尚未生效"):
        grant.assert_active_at(before)

    assert grant.is_active_at(issued) is True
    grant.assert_active_at(issued)

    assert grant.is_active_at(expires) is False
    with pytest.raises(ValueError, match="已過期"):
        grant.assert_active_at(expires)

    with pytest.raises(ValueError, match="時區"):
        grant.is_active_at(datetime(2026, 7, 15))
    with pytest.raises(ValueError, match="時區"):
        grant.assert_active_at(datetime(2026, 7, 15))


def test_grant_model_and_nested_json_are_immutable_after_validation() -> None:
    grant = ExecutionGrant.model_validate(_fixture("execution-grant-v1.json"))
    with pytest.raises(ValidationError, match="frozen"):
        grant.task_id = 9  # type: ignore[misc]
    manifest = AgentManifest.model_validate(_fixture("agent-manifest-v1.json"))
    assert manifest.input_schema["required"] == ["query"]
    with pytest.raises(TypeError, match="immutable"):
        manifest.input_schema["required"].append("other")


def test_v2_schema_versions_are_required_literals_and_top_level_closed() -> None:
    for model, _fixture_name in CONTRACT_FIXTURES:
        assert model.model_fields["schema_version"].is_required()
        schema = model.model_json_schema()
        assert schema["additionalProperties"] is False
        assert "schema_version" in schema["required"]
        assert schema["properties"]["schema_version"]["const"].endswith("/v1")


def test_grant_ttl_constant_is_five_minutes() -> None:
    assert MAX_EXECUTION_GRANT_TTL.total_seconds() == 300
