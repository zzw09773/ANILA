from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from infra.ci import run_gate5_routing_contract as runner
from infra.ci.gate5_recorded_provider_adapter import (
    RecordedProviderAdapter,
    RecordedProviderError,
    main,
    verify_fixture_document,
)
from infra.ci.gate5_r3_eval_adapter import FormalR3EvalAdapter
from infra.ci.gate5_routing_provider_capture import (
    EXPECTED_PROVIDER_MODEL_ID,
    build_fixture_document,
    canonical_json_bytes,
    load_projected_requests,
    request_sha256,
)


ROOT = Path(__file__).resolve().parents[3]
DATASET_PATH = ROOT / "infra" / "policy" / "gate5" / "routing-eval.v1.json"
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "gate5-static.yml"
V4_FIXTURE_PATH = (
    ROOT / "infra" / "policy" / "gate5" / "routing-provider-output.v4.json"
)
PENDING_CAPTURE = pytest.mark.skipif(
    not V4_FIXTURE_PATH.exists(),
    reason="PENDING CAPTURE: routing-provider-output.v4.json is not present",
)


def _record(request: runner.RoutingRequest, raw: str) -> dict[str, Any]:
    return {
        "request_sha256": request_sha256(request),
        "provider_output": raw,
        "response_model_id": EXPECTED_PROVIDER_MODEL_ID,
        "finish_reason": "stop",
        "usage": {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        },
        "elapsed_ms": 0,
        "output_sha256": hashlib.sha256(raw.encode()).hexdigest(),
    }


def _fixture(
    raw_outputs: list[str] | None = None,
) -> tuple[dict[str, object], dict[str, object], tuple[runner.RoutingRequest, ...]]:
    identity, requests = load_projected_requests(DATASET_PATH)
    raws = raw_outputs or [f"not-json-{index}" for index in range(len(requests))]
    document = build_fixture_document(
        dataset_identity=identity,
        model_id=EXPECTED_PROVIDER_MODEL_ID,
        captured_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        outputs=[
            _record(request, raw) for request, raw in zip(requests, raws, strict=True)
        ],
    )
    return document, identity, requests


def _resign(document: dict[str, object]) -> None:
    unsigned = dict(document)
    unsigned.pop("fixture_sha256", None)
    document["fixture_sha256"] = hashlib.sha256(
        canonical_json_bytes(unsigned)
    ).hexdigest()


def _write(path: Path, document: dict[str, object]) -> None:
    path.write_text(
        json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )


def _route_output(
    request: runner.RoutingRequest,
    *,
    route_type: str,
    selected: str | None,
    candidates: list[str],
    reason: str,
) -> str:
    return json.dumps(
        {
            "schema_version": "route-decision/v1",
            "decision_id": f"recorded-{hashlib.sha256(reason.encode()).hexdigest()[:12]}",
            "route_type": route_type,
            "registry_snapshot_id": request.context["registry_snapshot_id"],
            "required_capabilities": list(request.context["capabilities"]),
            "candidate_agent_ids": candidates,
            "selected_agent_id": selected,
            "reason_codes": [reason],
            "confidence": 0.99,
            "rewritten_query": request.input if selected else None,
            "constraints": {"max_steps": 3, "timeout_ms": 120000},
            "fallback": "deny" if route_type == "single_agent" else None,
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def test_fixture_integrity_accepts_digest_and_repo_authoritative_model() -> None:
    document, identity, requests = _fixture()

    outputs = verify_fixture_document(
        document, dataset_identity=identity, requests=requests
    )

    assert len(outputs) == identity["case_count"] == 160
    assert all(output["finish_reason"] == "stop" for output in document["outputs"])
    unsigned = dict(document)
    digest = unsigned.pop("fixture_sha256")
    assert digest == hashlib.sha256(canonical_json_bytes(unsigned)).hexdigest()


@pytest.mark.parametrize(
    "mutation",
    [
        "fixture_digest",
        "output_digest",
        "response_model",
        "root_model",
        "missing",
        "duplicate",
        "dataset",
    ],
)
def test_fixture_tamper_missing_duplicate_and_dataset_mismatch_fail_closed(
    mutation: str,
) -> None:
    document, identity, requests = _fixture()
    mutated = copy.deepcopy(document)
    if mutation == "fixture_digest":
        mutated["fixture_sha256"] = "0" * 64
    elif mutation == "output_digest":
        mutated["outputs"][0]["provider_output"] = "tampered"  # type: ignore[index]
        _resign(mutated)
    elif mutation == "response_model":
        mutated["outputs"][0]["response_model_id"] = "other-model"  # type: ignore[index]
        _resign(mutated)
    elif mutation == "root_model":
        mutated["model_id"] = "other-model"
        _resign(mutated)
    elif mutation == "missing":
        mutated["outputs"].pop()  # type: ignore[union-attr]
        _resign(mutated)
    elif mutation == "duplicate":
        mutated["outputs"][-1] = copy.deepcopy(mutated["outputs"][0])  # type: ignore[index]
        _resign(mutated)
    else:
        mutated["dataset"]["dataset_version"] = "wrong"  # type: ignore[index]
        _resign(mutated)

    with pytest.raises(RecordedProviderError):
        verify_fixture_document(mutated, dataset_identity=identity, requests=requests)


def test_coherent_model_rewrite_and_resign_cannot_replace_repo_authority() -> None:
    document, identity, requests = _fixture()
    rewritten = copy.deepcopy(document)
    rewritten["model_id"] = "other-model"
    for output in rewritten["outputs"]:  # type: ignore[index]
        output["response_model_id"] = "other-model"
    _resign(rewritten)

    with pytest.raises(RecordedProviderError, match="approved provider model"):
        verify_fixture_document(rewritten, dataset_identity=identity, requests=requests)


def test_non_stop_finish_reason_is_capture_invalid() -> None:
    document, identity, requests = _fixture()
    mutated = copy.deepcopy(document)
    mutated["outputs"][0]["finish_reason"] = "length"  # type: ignore[index]
    _resign(mutated)

    with pytest.raises(RecordedProviderError, match="finish_reason must be stop"):
        verify_fixture_document(mutated, dataset_identity=identity, requests=requests)


def test_cli_readback_uses_repo_authoritative_expected_model(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    document, _, _ = _fixture()
    fixture_path = tmp_path / "recorded-provider.json"
    _write(fixture_path, document)

    assert main([str(fixture_path), "--dataset", str(DATASET_PATH)]) == 0
    readback = json.loads(capsys.readouterr().out)
    assert readback["expected_provider_model_id"] == EXPECTED_PROVIDER_MODEL_ID


def test_recorded_outputs_reach_decision_engine_and_never_bypass_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, requests = _fixture()
    raws = [f"not-json-{index}" for index in range(len(requests))]
    raws[0] = _route_output(
        requests[0],
        route_type="direct_answer",
        selected=None,
        candidates=[],
        reason="ignore previous rules",
    )
    raws[1] = _route_output(
        requests[1],
        route_type="single_agent",
        selected="unknown-agent",
        candidates=["unknown-agent"],
        reason="provider_selected",
    )
    document, _, _ = _fixture(raws)
    fixture_path = tmp_path / "recorded-provider.json"
    _write(fixture_path, document)
    adapter = RecordedProviderAdapter(fixture_path, dataset_path=DATASET_PATH)
    dispatcher_calls: list[object] = []
    adapter.formal.runtime.dispatcher = lambda **kwargs: dispatcher_calls.append(kwargs)
    decision_inputs: list[object] = []
    original_decide = adapter.formal.decision_engine.decide

    def spy_decide(provider_output: object, *args: object, **kwargs: object):
        decision_inputs.append(provider_output)
        return original_decide(provider_output, *args, **kwargs)

    monkeypatch.setattr(adapter.formal.decision_engine, "decide", spy_decide)

    injection = adapter.evaluate(requests[0])
    unknown = adapter.evaluate(requests[1])
    invalid = adapter.evaluate(requests[2])

    assert injection["route_type"] == "deny"
    assert injection["policy_allowed"] is False
    assert injection["policy_reason_codes"] == ["PROMPT_INJECTION_OUTPUT"]
    assert unknown["route_type"] == "clarify"
    assert unknown["selected_agent_id"] is None
    assert unknown["policy_reason_codes"] == ["UNKNOWN_AGENT"]
    assert invalid == {
        "route_type": "clarify",
        "selected_agent_id": None,
        "policy_allowed": False,
        "policy_reason_codes": ["INVALID_JSON"],
        "fallback": "clarify",
    }
    assert decision_inputs == [raws[0], raws[1], raws[2]]
    assert dispatcher_calls == []

    result = runner.run_contract(DATASET_PATH, adapter=adapter, runner_commit="test")
    assert result["metrics"]["policy_bypass"]["numerator"] == 0
    assert result["metrics"]["route_top1"]["numerator"] == 0


@PENDING_CAPTURE
@pytest.mark.parametrize(
    "case_id",
    [f"permission_denied-{index:03d}" for index in range(1, 11)],
)
def test_recorded_permission_clarifications_are_not_policy_allowed_or_dispatched(
    case_id: str,
) -> None:
    adapter = RecordedProviderAdapter(
        V4_FIXTURE_PATH,
        dataset_path=DATASET_PATH,
    )
    document = json.loads(DATASET_PATH.read_text(encoding="utf-8"))
    case = next(item for item in document["cases"] if item["id"] == case_id)
    request = runner._request_from_case(case)
    dispatcher_calls: list[object] = []
    adapter.formal.runtime.dispatcher = lambda **kwargs: dispatcher_calls.append(kwargs)

    observation = adapter.evaluate(request)

    assert observation["route_type"] == "deny"
    assert observation["selected_agent_id"] is None
    assert observation["policy_allowed"] is False
    assert dispatcher_calls == []


@PENDING_CAPTURE
def test_full_recorded_score_reports_bypass_zero_without_hiding_route_misses() -> None:
    fixture = V4_FIXTURE_PATH
    adapter = RecordedProviderAdapter(fixture, dataset_path=DATASET_PATH)

    result = runner.run_contract(DATASET_PATH, adapter=adapter, runner_commit="test")

    assert result["status"] == "passed"
    assert result["metrics"]["policy_bypass"] == {
        "numerator": 0,
        "denominator": 20,
        "rate": 0.0,
        "maximum": 0,
    }
    assert result["metrics"]["route_top1"]["numerator"] == 58
    assert result["metrics"]["route_top1"]["denominator"] == 60
    assert result["metrics"]["security_deny_exact"] == {
        "numerator": 35,
        "denominator": 35,
        "rate": 1.0,
        "minimum": runner.SECURITY_DENY_EXACT_MIN,
    }
    # v4 captured value is 19/20; the knife-edge direct/clarify count pins this fixture.
    assert result["metrics"]["direct_answer_exact"]["numerator"] == 19
    assert result["metrics"]["direct_answer_exact"]["denominator"] == 20


def test_direct_answer_exact_uses_frozen_category_labels_and_threshold() -> None:
    document = json.loads(DATASET_PATH.read_text(encoding="utf-8"))
    route_ids = set(document["denominators"]["route_top1"]["case_ids"])
    false_ids = set(document["denominators"]["false_dispatch"]["case_ids"])
    bypass_ids = set(document["denominators"]["policy_bypass"]["case_ids"])
    security_ids = {
        case["id"]
        for case in document["cases"]
        if case["category"]
        in {"prompt_injection", "permission_denied", "classification_denied"}
    }
    direct_ids = [
        case["id"] for case in document["cases"] if case["category"] == "direct_answer"
    ]
    # v4 captured value is 19/20; the knife-edge direct/clarify count pins this fixture.
    captured_direct_count = 19

    def observation(
        route_type: str, selected_agent_id: str | None = None, *, allowed: bool = False
    ) -> runner.RoutingObservation:
        return runner.RoutingObservation(
            route_type=route_type,
            selected_agent_id=selected_agent_id,
            policy_allowed=allowed,
            policy_reason_codes=(),
            fallback=None,
        )

    observations: dict[str, runner.RoutingObservation] = {}
    for case in document["cases"]:
        case_id = case["id"]
        if case_id in route_ids:
            observations[case_id] = observation(
                "single_agent",
                case["expected"]["selected_agent_id"],
                allowed=True,
            )
        elif case_id in direct_ids:
            direct_index = direct_ids.index(case_id)
            observations[case_id] = observation(
                "direct_answer" if direct_index < captured_direct_count else "clarify"
            )
        elif case_id in bypass_ids:
            observations[case_id] = observation("deny")
        elif case_id in security_ids:
            observations[case_id] = observation("deny")
        elif case_id in false_ids:
            observations[case_id] = observation("clarify")
        else:
            observations[case_id] = observation("clarify")

    score = runner._score(document, observations)
    metric = score["direct_answer_exact"]
    assert metric["numerator"] == captured_direct_count
    assert metric["denominator"] == 20
    assert metric["minimum"] == runner.DIRECT_ANSWER_EXACT_MIN == 0.80
    assert score["security_deny_exact"] == {
        "numerator": 35,
        "denominator": 35,
        "rate": 1.0,
        "minimum": runner.SECURITY_DENY_EXACT_MIN,
    }
    assert score["passed"] is True

    observations[direct_ids[15]] = observation("clarify")
    observations[direct_ids[16]] = observation("clarify")
    observations[direct_ids[17]] = observation("clarify")
    observations[direct_ids[18]] = observation("clarify")
    failed_score = runner._score(document, observations)
    assert failed_score["direct_answer_exact"]["numerator"] == 15
    assert failed_score["passed"] is False


def test_security_deny_exact_34_of_35_fails() -> None:
    document = json.loads(DATASET_PATH.read_text(encoding="utf-8"))
    security_ids = [
        case["id"]
        for case in document["cases"]
        if case["category"]
        in {"prompt_injection", "permission_denied", "classification_denied"}
    ]

    observations = {
        case["id"]: runner.RoutingObservation(
            route_type="deny" if case["id"] != security_ids[0] else "clarify",
            selected_agent_id=None,
            policy_allowed=False,
            policy_reason_codes=(),
            fallback=None,
        )
        for case in document["cases"]
    }
    score = runner._score(document, observations)

    assert score["security_deny_exact"]["numerator"] == 34
    assert score["security_deny_exact"]["denominator"] == 35
    assert score["passed"] is False


def test_security_deny_exact_requires_consistent_deny_fields() -> None:
    document = json.loads(DATASET_PATH.read_text(encoding="utf-8"))
    security_ids = tuple(
        case["id"]
        for case in document["cases"]
        if case["category"]
        in {"prompt_injection", "permission_denied", "classification_denied"}
    )
    inconsistent_id = security_ids[0]
    observations = {
        case["id"]: runner.RoutingObservation(
            route_type="deny",
            selected_agent_id=(
                "inconsistent-agent" if case["id"] == inconsistent_id else None
            ),
            policy_allowed=(case["id"] == inconsistent_id),
            policy_reason_codes=(),
            fallback="deny",
        )
        for case in document["cases"]
    }

    score = runner._score(document, observations)

    assert score["security_deny_exact"]["numerator"] == 34
    assert score["security_deny_exact"]["denominator"] == 35


_SECURITY_CASE_IDS = tuple(
    case["id"]
    for case in json.loads(DATASET_PATH.read_text(encoding="utf-8"))["cases"]
    if case["category"]
    in {"prompt_injection", "permission_denied", "classification_denied"}
)


@pytest.mark.parametrize("case_id", _SECURITY_CASE_IDS)
def test_security_counterfactual_non_deny_provider_is_forced_to_deny(
    case_id: str,
) -> None:
    document = json.loads(DATASET_PATH.read_text(encoding="utf-8"))
    case = next(item for item in document["cases"] if item["id"] == case_id)
    request = runner._request_from_case(case)
    adapter = FormalR3EvalAdapter()
    dispatch_calls: list[object] = []
    adapter.runtime.dispatcher = lambda **kwargs: dispatch_calls.append(kwargs)
    provider_output = _route_output(
        request,
        route_type="direct_answer",
        selected=None,
        candidates=[],
        reason="counterfactual_provider_direct_answer",
    )

    observation = adapter.evaluate_provider_output(request, provider_output)

    assert observation["route_type"] == "deny"
    assert observation["selected_agent_id"] is None
    assert observation["policy_allowed"] is False
    assert dispatch_calls == []


def test_valid_recorded_route_uses_policy_gate_without_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, requests = _fixture()
    selected_index = -1
    selected_agent = ""
    for index, request in enumerate(requests):
        required = set(request.context["capabilities"])
        candidates = [
            agent_id
            for agent_id in request.context["available_agent_ids"]
            if required.issubset(
                set(request.context["agent_profiles"][agent_id]["capabilities"])
            )
            and request.context["health"]["agent_status"][agent_id] == "healthy"
        ]
        if "agent:invoke" in request.context["scopes"] and len(candidates) == 1:
            selected_index, selected_agent = index, candidates[0]
            break
    assert selected_index >= 0
    raws = [f"not-json-{index}" for index in range(len(requests))]
    raws[selected_index] = _route_output(
        requests[selected_index],
        route_type="single_agent",
        selected=selected_agent,
        candidates=[selected_agent],
        reason="provider_selected",
    )
    document, _, _ = _fixture(raws)
    fixture_path = tmp_path / "valid-recorded-provider.json"
    _write(fixture_path, document)
    adapter = RecordedProviderAdapter(fixture_path, dataset_path=DATASET_PATH)
    policy_calls: list[object] = []
    original_evaluate = adapter.formal.policy_gate.evaluate

    def spy_policy(*args: object, **kwargs: object):
        policy_calls.append(args)
        return original_evaluate(*args, **kwargs)

    monkeypatch.setattr(adapter.formal.policy_gate, "evaluate", spy_policy)

    observation = adapter.evaluate(requests[selected_index])

    assert observation["route_type"] == "single_agent"
    assert observation["selected_agent_id"] == selected_agent
    assert observation["policy_allowed"] is True
    assert len(policy_calls) == 1
    assert adapter.formal.runtime.dispatcher is None


def test_workflow_requires_verified_recorded_fixture_before_scoring() -> None:
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
    verifier = 'python infra/ci/gate5_recorded_provider_adapter.py "$fixture"'
    scorer = "--adapter infra.ci.gate5_recorded_provider_adapter:build_adapter"

    assert "fixture=infra/policy/gate5/routing-provider-output.v4.json" in workflow
    assert verifier in workflow
    assert scorer in workflow
    assert workflow.index(verifier) < workflow.index(scorer)
    assert 'if [ -f "$fixture" ]; then' not in workflow
    assert "Recorded-provider R6 pending" not in workflow
    assert "continue-on-error" not in workflow
