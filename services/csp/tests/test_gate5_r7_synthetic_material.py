from __future__ import annotations

import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi import HTTPException
from starlette.requests import Request

import app.api.proxy as proxy_api
from app.models.audit_log import AuditLog
from app.models.model_governance_receipt import ModelGovernanceReceipt
from app.models.token_usage import TokenUsage
from app.services import model_governance_runtime as runtime_module
from app.services.agent_credential_service import CallerIdentity
from app.services.model_governance_receipts import (
    GovernedModelInvocation,
    ReceiptSubject,
)
from app.services.proxy import service as proxy_service
from tests.conftest import make_agent, make_model, make_user


ROOT = Path(__file__).parents[3]
GENERATOR = ROOT / "infra/deployment/scripts/generate-gate5-test-material.py"
GATEWAY_ENDPOINT = "https://csp-model-gateway/v1"


def _request(*headers: tuple[str, str]) -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/v1/chat/completions",
            "headers": [
                (name.lower().encode(), value.encode())
                for name, value in headers
            ],
        }
    )


def _generator():
    spec = importlib.util.spec_from_file_location("gate5_material_generator", GENERATOR)
    if spec is None or spec.loader is None:
        raise AssertionError(f"cannot import generator: {GENERATOR}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _runtime(tmp_path: Path, *, callsite_ids: tuple[str, ...] | None = None):
    paths = _generator().generate(
        tmp_path / "material",
        callsite_ids=callsite_ids,
    )
    runtime = runtime_module.ModelGovernanceRuntime(
        enabled=True,
        inventory_path=paths["inventory"],
        profile_path=paths["profile"],
        trust_store_path=paths["trust_store"],
        observed_facts_path=paths["observed_facts"],
        gateway_endpoint=GATEWAY_ENDPOINT,
    )
    material = {
        name: json.loads(path.read_text(encoding="utf-8"))
        for name, path in paths.items()
    }
    now = datetime.now(timezone.utc)
    assert runtime.bootstrap(now=now).ready is True
    return runtime, material, now


def test_generated_memory_material_records_db_receipts_once_around_mock_downstream(
    tmp_path: Path,
    db,
) -> None:
    runtime, _material, now = _runtime(tmp_path)
    user = make_user(db, username="gate5-r7-synthetic-memory")
    model = make_model(db, name="gate5-r7-synthetic-memory-model")
    governed = GovernedModelInvocation.from_runtime(
        db,
        runtime=runtime,
        subject=ReceiptSubject(
            user_id=user.id,
            model_id=model.id,
            department_id=user.department_id,
            actor_username=user.username,
        ),
    )
    network_calls: list[dict] = []

    authorization = governed.authorize(
        callsite_id="r7.csp.memory",
        classification="營業秘密",
        endpoint=f"{GATEWAY_ENDPOINT}/chat/completions",
        invocation_id="synthetic-memory-invocation",
        now=now,
    )

    def mock_downstream(payload: dict) -> dict:
        network_calls.append(payload)
        return {"choices": [{"message": {"content": "synthetic"}}]}

    response = mock_downstream({"model": "artifact.gate5-synthetic"})
    completion = governed.complete(
        authorization,
        {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5},
        now=now,
    )

    assert response["choices"][0]["message"]["content"] == "synthetic"
    assert len(network_calls) == 1
    assert completion is not None
    ledger = db.query(ModelGovernanceReceipt).one()
    assert ledger.invocation_id == "synthetic-memory-invocation"
    assert ledger.callsite_id == "r7.csp.memory"
    assert ledger.status == "completed"
    usage = db.query(TokenUsage).filter(TokenUsage.id == ledger.usage_record_id).one()
    assert (usage.prompt_tokens, usage.completion_tokens, usage.total_tokens) == (2, 3, 5)
    audits = (
        db.query(AuditLog)
        .filter(AuditLog.id.in_([ledger.pre_audit_id, ledger.post_audit_id]))
        .order_by(AuditLog.id)
        .all()
    )
    assert [row.action for row in audits] == [
        "model.governance.pre",
        "model.governance.post",
    ]


def test_forged_agent_header_on_human_auth_stays_on_generic_callsite(
    db, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = make_user(db, username="gate5-r7-human-generic")
    human = proxy_api.Caller(user=user, api_key_id=None)
    monkeypatch.setattr(proxy_api, "get_caller", lambda _request, _db: human)
    request = _request(
        ("Authorization", "Bearer human-token"),
        ("X-ANILA-Agent-Id", "forged-agent"),
    )

    caller = proxy_api._proxy_caller(request, db)

    assert caller == human
    assert proxy_api._verified_proxy_agent_context(request, db) is None
    assert proxy_api._proxy_governance_callsite(None) == "r7.csp.proxy"


@pytest.mark.asyncio
async def test_verified_agent_csk_selects_agent_callsite_and_records_db_attribution(
    tmp_path: Path,
    db,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SECRET_KEY", "gate5-positive-test-secret")
    runtime, _material, _now = _runtime(
        tmp_path,
        callsite_ids=("r7.csp.proxy-agent",),
    )
    owner = make_user(db, username="gate5-r7-agent-owner")
    agent = make_agent(
        db,
        owner,
        name="research-agent-positive",
        approval_status="approved",
    )
    _credential, csk = proxy_api.agent_credential_service.issue_static_credential(
        db,
        agent=agent,
        issuer=owner,
        label="gate5-positive",
    )
    db.commit()
    model = make_model(db, name="gate5-r7-agent-model")
    request = _request(
        ("Authorization", f"Bearer {csk}"),
        ("X-CSP-Service-Token", csk),
        ("X-ANILA-Agent-Id", "forged-agent"),
    )
    caller = proxy_api._proxy_caller(request, db)
    agent_context = proxy_api._verified_proxy_agent_context(request, db)
    assert agent_context == (agent.id, agent.name)
    assert proxy_api._proxy_governance_callsite(agent_context) == "r7.csp.proxy-agent"

    monkeypatch.setattr(
        proxy_service,
        "resolve_model_governance_runtime",
        lambda: runtime,
    )

    async def fake_impl(**kwargs):
        kwargs["usage_capture"]["governance_usage"] = {
            "prompt_tokens": 2,
            "completion_tokens": 1,
            "total_tokens": 3,
        }
        return {"id": "agent-gate5-positive", "usage": {"total_tokens": 3}}

    monkeypatch.setattr(proxy_service, "_proxy_request_impl", fake_impl)
    response = await proxy_service.proxy_request(
        model=model,
        api_key_id=caller.api_key_id,
        user_id=caller.user.id,
        department_id=caller.user.department_id,
        request_body={"model": model.name, "messages": []},
        endpoint_path="/v1/chat/completions",
        governance_callsite_id=proxy_api._proxy_governance_callsite(agent_context),
        governance_agent_id=agent_context[1],
        caller_agent_id=agent_context[0],
        governance_db=db,
    )

    assert response["id"] == "agent-gate5-positive"
    ledger = db.query(ModelGovernanceReceipt).one()
    usage = db.query(TokenUsage).filter(TokenUsage.id == ledger.usage_record_id).one()
    metadata = json.loads(ledger.metadata_json)
    assert ledger.callsite_id == "r7.csp.proxy-agent"
    assert ledger.status == "completed"
    assert usage.caller_agent_id == agent.id
    assert metadata["receipt_context"]["agent_id"] == agent.name
    assert metadata["receipt_context"]["caller_agent_id"] == agent.id


def test_service_client_csk_stays_generic_with_user_auth(
    db, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = make_user(db, username="gate5-r7-service-user")
    caller = proxy_api.Caller(user=user, api_key_id=17)
    identity = CallerIdentity(
        kind="service_client",
        agent_id=None,
        service_client_id=99,
        credential_id=99,
        is_legacy=False,
        used_previous_token=False,
    )
    monkeypatch.setattr(
        proxy_api.agent_credential_service,
        "verify_service_token",
        lambda _db, *, token: identity,
    )
    monkeypatch.setattr(proxy_api, "get_caller", lambda _request, _db: caller)
    request = _request(
        ("Authorization", "Bearer user-api-key"),
        ("X-CSP-Service-Token", "csk-router"),
        ("X-ANILA-Agent-Id", "forged-agent"),
    )

    resolved = proxy_api._proxy_caller(request, db)

    assert resolved == caller
    assert request.state.csp_caller == identity
    assert not hasattr(request.state, "csp_caller_agent_id")
    assert not hasattr(request.state, "csp_caller_agent_name")
    assert proxy_api._verified_proxy_agent_context(request, db) is None
    assert proxy_api._proxy_governance_callsite(None) == "r7.csp.proxy"


def test_service_client_csk_cannot_double_as_user_bearer(
    db, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity = CallerIdentity(
        kind="service_client",
        agent_id=None,
        service_client_id=99,
        credential_id=99,
        is_legacy=False,
        used_previous_token=False,
    )
    monkeypatch.setattr(
        proxy_api.agent_credential_service,
        "verify_service_token",
        lambda _db, *, token: identity,
    )
    request = _request(
        ("Authorization", "Bearer csk-router"),
        ("X-CSP-Service-Token", "csk-router"),
    )

    with pytest.raises(HTTPException) as raised:
        proxy_api._proxy_caller(request, db)

    assert raised.value.status_code == 401


@pytest.mark.parametrize(
    "callsite_id", ("r7.csp.proxy-agent", "r7.csp.proxy-service-agent")
)
def test_scoped_proxy_callsite_without_caller_agent_context_fails_before_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    callsite_id: str,
    db,
) -> None:
    runtime, material, now = _runtime(
        tmp_path,
        callsite_ids=(
            "r7.csp.proxy",
            "r7.csp.proxy-agent",
            "r7.csp.proxy-service",
            "r7.csp.proxy-service-agent",
            "r7.csp.memory",
        ),
    )
    profile = material["profile"]
    artifact = profile["model_artifacts"][0]
    deployment = profile["deployments"][0]
    monkeypatch.setattr(proxy_service, "resolve_model_governance_runtime", lambda: runtime)

    governed = proxy_service._build_governed_model_invocation(
        governance_db=db,
        governance_callsite_id=callsite_id,
        model_type="llm",
        target_agent_id=None,
        user_id=11,
        model_id=7,
        department_id=None,
        conversation_id=None,
        trace_id="synthetic-trace",
    )
    assert governed is not None
    network_calls: list[object] = []

    with pytest.raises(HTTPException) as raised:
        proxy_service._authorize_governance(
            governed,
            callsite_id=callsite_id,
            classification=proxy_service.Classification.CONFIDENTIAL,
            invocation_id="scoped-proxy-no-agent",
        )
        network_calls.append("must-not-run")

    assert raised.value.status_code == 503
    assert "admission failed" in str(raised.value.detail)
    assert network_calls == []
    assert runtime.authority is not None
    binding = runtime.authority.bindings[callsite_id]
    assert binding.agent_scope == ("registered-agent",)
    assert artifact["artifact_id"] == deployment["artifact_id"]


@pytest.mark.parametrize("callsite_id", ("r7.flux.backend", "r7.not-in-inventory"))
def test_disabled_raw_or_unknown_callsite_is_rejected_before_network(
    tmp_path: Path,
    callsite_id: str,
) -> None:
    runtime, material, now = _runtime(tmp_path)
    profile = material["profile"]
    artifact = profile["model_artifacts"][0]
    deployment = profile["deployments"][0]
    network_calls: list[object] = []

    with pytest.raises(runtime_module.ModelGovernanceRuntimeError, match="not enabled"):
        runtime.authorize_model_invocation(
            callsite_id,
            "營業秘密",
            None,
            f"{GATEWAY_ENDPOINT}/chat/completions",
            artifact,
            deployment,
            now=now,
        )
        network_calls.append("must-not-run")

    assert network_calls == []
