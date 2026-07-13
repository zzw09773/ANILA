"""Gate 2 G19 runtime sink, registry-race and pilot surface evidence."""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

os.environ.setdefault("ANILA_ALLOW_DEV_SECRET", "1")

import pytest
from anila_security import PilotTarget, VerifiedPilotAdmission
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api import proxy as proxy_api
from app.config import settings
from app.models.audit_log import AuditLog
from app.models.conversation import Conversation
from app.models.policy_decision import PolicyDecision
from app.models.task import Task, TaskRun
from app.services.auth_service import create_tokens
from app.services.proxy import service as proxy_service
from app.services import startup_security
from tests.conftest import make_agent, make_model, make_user


class _OutboundMustNotRun:
    def __init__(self, *args, **kwargs):
        raise AssertionError("outbound network call must remain zero")


class _SuccessfulResponse:
    status_code = 200
    text = '{"choices":[{"message":{"content":"ok"}}]}'
    headers = {"content-type": "application/json"}

    def json(self):
        return {
            "choices": [{"message": {"content": "ok"}}],
            "usage": {
                "prompt_tokens": 1,
                "completion_tokens": 1,
                "total_tokens": 2,
            },
        }


class _SuccessfulClient:
    calls = 0

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, *args, **kwargs):
        type(self).calls += 1
        return _SuccessfulResponse()


def _bearer(user) -> dict[str, str]:
    token = create_tokens(user)["access_token"]
    return {"Authorization": f"Bearer {token}"}


def _pilot_admission(model) -> VerifiedPilotAdmission:
    return VerifiedPilotAdmission(
        profile_id="runtime-test",
        enabled_callsites=frozenset({"csp.chat_model"}),
        allowed_targets=(PilotTarget(
            callsite="csp.chat_model",
            name=model.name,
            model_type=model.model_type,
            endpoint_url=model.endpoint_url,
            classification_ceiling=model.classification_ceiling,
        ),),
        collection_ids=frozenset({1}),
        data_classification_ceiling="營業秘密",
        valid_from=datetime.now(timezone.utc) - timedelta(minutes=1),
        valid_until=datetime.now(timezone.utc) + timedelta(hours=1),
    )


def _running_task(db: Session, *, user, level: str = "無機密"):
    task = Task(
        title="pilot runtime admission",
        task_type="query",
        requester_user_id=user.id,
        status="running",
        classification_level=level,
    )
    db.add(task)
    db.flush()
    run = TaskRun(
        task_id=task.id,
        run_sequence=1,
        dispatch_target="model",
        status="running",
        classification_level=level,
    )
    db.add(run)
    db.commit()
    return task, run


@pytest.mark.asyncio
async def test_exact_signed_target_reaches_sink_once(
    db: Session, monkeypatch,
) -> None:
    user = make_user(db, username="pilot_exact_target")
    model = make_model(db, name="pilot-exact-target")
    task, run = _running_task(db, user=user)
    monkeypatch.setattr(settings, "ANILA_PILOT_MODE", True)
    monkeypatch.setattr(
        startup_security, "_verified_pilot_admission", _pilot_admission(model)
    )
    monkeypatch.setattr(proxy_service, "_guard_outbound", lambda *args, **kwargs: None)
    monkeypatch.setattr(proxy_service.httpx, "AsyncClient", _SuccessfulClient)

    async def ignore_usage(**kwargs):
        return None

    monkeypatch.setattr(proxy_service, "enqueue_usage_task_linked", ignore_usage)
    _SuccessfulClient.calls = 0
    result = await proxy_service.proxy_request(
        model=model,
        api_key_id=None,
        user_id=user.id,
        department_id=None,
        request_body={"model": model.name, "messages": []},
        endpoint_path="/v1/chat/completions",
        task_id=task.id,
        task_run_id=run.id,
        inference_callsite_id="csp.chat_model",
        governance_db=db,
        admitted_classification_level="無機密",
    )

    assert result["choices"][0]["message"]["content"] == "ok"
    assert _SuccessfulClient.calls == 1


@pytest.mark.asyncio
async def test_expired_signed_admission_is_zero_egress(
    db: Session, monkeypatch,
) -> None:
    user = make_user(db, username="pilot_expired_target")
    model = make_model(db, name="pilot-expired-target")
    task, run = _running_task(db, user=user)
    current = _pilot_admission(model)
    expired = VerifiedPilotAdmission(
        profile_id=current.profile_id,
        enabled_callsites=current.enabled_callsites,
        allowed_targets=current.allowed_targets,
        collection_ids=current.collection_ids,
        data_classification_ceiling=current.data_classification_ceiling,
        valid_from=datetime.now(timezone.utc) - timedelta(hours=2),
        valid_until=datetime.now(timezone.utc) - timedelta(hours=1),
    )
    monkeypatch.setattr(settings, "ANILA_PILOT_MODE", True)
    monkeypatch.setattr(startup_security, "_verified_pilot_admission", expired)
    monkeypatch.setattr(proxy_service.httpx, "AsyncClient", _OutboundMustNotRun)

    with pytest.raises(HTTPException, match="no longer effective"):
        await proxy_service.proxy_request(
            model=model,
            api_key_id=None,
            user_id=user.id,
            department_id=None,
            request_body={"model": model.name, "messages": []},
            endpoint_path="/v1/chat/completions",
            task_id=task.id,
            task_run_id=run.id,
            inference_callsite_id="csp.chat_model",
            governance_db=db,
            admitted_classification_level="無機密",
        )


@pytest.mark.asyncio
async def test_over_signed_data_ceiling_is_zero_egress(
    db: Session, monkeypatch,
) -> None:
    user = make_user(db, username="pilot_over_ceiling")
    model = make_model(db, name="pilot-over-ceiling")
    task, run = _running_task(db, user=user, level="機密")
    monkeypatch.setattr(settings, "ANILA_PILOT_MODE", True)
    monkeypatch.setattr(
        startup_security, "_verified_pilot_admission", _pilot_admission(model)
    )
    monkeypatch.setattr(proxy_service.httpx, "AsyncClient", _OutboundMustNotRun)

    with pytest.raises(HTTPException, match="classification ceiling"):
        await proxy_service.proxy_request(
            model=model,
            api_key_id=None,
            user_id=user.id,
            department_id=None,
            request_body={"model": model.name, "messages": []},
            endpoint_path="/v1/chat/completions",
            task_id=task.id,
            task_run_id=run.id,
            inference_callsite_id="csp.chat_model",
            governance_db=db,
            admitted_classification_level="無機密",
        )


@pytest.mark.parametrize("mutation", ["name", "endpoint", "model_type"])
def test_chat_rejects_unsigned_or_type_confused_target_before_hidden_or_foreground_egress(
    client: TestClient, db: Session, monkeypatch, mutation: str,
) -> None:
    user = make_user(db, username=f"pilot_unsigned_{mutation}", role="admin")
    model = make_model(db, name=f"pilot-authorized-before-{mutation}")
    admission = _pilot_admission(model)
    if mutation == "name":
        model.name = f"pilot-unsigned-after-{mutation}"
    elif mutation == "endpoint":
        model.endpoint_url = "http://unsigned-target:8080"
    else:
        model.model_type = "embedding"
    db.commit()
    task = Task(
        title="unsigned pilot target",
        task_type="query",
        requester_user_id=user.id,
        status="submitted",
    )
    db.add(task)
    db.commit()
    monkeypatch.setattr(settings, "ANILA_PILOT_MODE", True)
    monkeypatch.setattr(startup_security, "_verified_pilot_admission", admission)
    monkeypatch.setattr(proxy_service.httpx, "AsyncClient", _OutboundMustNotRun)

    response = client.post(
        "/v1/chat/completions",
        headers={**_bearer(user), "X-ANILA-Task-Id": str(task.id)},
        json={
            "model": model.name,
            "messages": [{"role": "user", "content": "must not egress"}],
        },
    )

    assert response.status_code == 403
    db.expire_all()
    run = db.query(TaskRun).filter_by(task_id=task.id).one()
    assert run.status == "failed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "callsite", [None, "csp.memory_extract", "csp.standalone_search_embedding"]
)
async def test_signed_chat_only_profile_denies_missing_or_other_sink_before_outbound(
    db: Session, monkeypatch, callsite: str | None,
) -> None:
    model = make_model(db, name=f"pilot-sink-{callsite or 'missing'}")
    monkeypatch.setattr(settings, "ANILA_PILOT_MODE", True)
    monkeypatch.setattr(
        startup_security, "_verified_pilot_admission", _pilot_admission(model)
    )
    monkeypatch.setattr(proxy_service.httpx, "AsyncClient", _OutboundMustNotRun)

    with pytest.raises(HTTPException) as caught:
        await proxy_service.proxy_request(
            model=model,
            api_key_id=None,
            user_id=1,
            department_id=None,
            request_body={"model": model.name, "messages": []},
            endpoint_path="/v1/chat/completions",
            task_id=1,
            inference_callsite_id=callsite,
            governance_db=db,
            admitted_classification_level="無機密",
        )
    assert caught.value.status_code == 403


@pytest.mark.asyncio
async def test_signed_chat_callsite_still_requires_task_and_ceiling_authority(
    db: Session, monkeypatch,
) -> None:
    model = make_model(db, name="pilot-chat-authority")
    monkeypatch.setattr(settings, "ANILA_PILOT_MODE", True)
    monkeypatch.setattr(
        startup_security, "_verified_pilot_admission", _pilot_admission(model)
    )
    monkeypatch.setattr(proxy_service.httpx, "AsyncClient", _OutboundMustNotRun)
    with pytest.raises(HTTPException, match="Task"):
        await proxy_service.proxy_request(
            model=model,
            api_key_id=None,
            user_id=1,
            department_id=None,
            request_body={"model": model.name, "messages": []},
            endpoint_path="/v1/chat/completions",
            inference_callsite_id="csp.chat_model",
            governance_db=db,
            admitted_classification_level="無機密",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["ceiling", "endpoint"])
async def test_registry_mutation_after_initial_ceiling_check_is_zero_egress(
    db: Session, monkeypatch, mutation: str,
) -> None:
    row = make_model(db, name=f"toctou-{mutation}")
    row.classification_ceiling = "機密"
    db.commit()
    stale = SimpleNamespace(
        id=row.id,
        name=row.name,
        display_name=row.display_name,
        model_type=row.model_type,
        endpoint_url=row.endpoint_url,
        api_version=row.api_version,
    )
    if mutation == "ceiling":
        row.classification_ceiling = "無機密"
    else:
        row.endpoint_url = "http://swapped-model:8080"
    db.commit()
    monkeypatch.setattr(proxy_service.httpx, "AsyncClient", _OutboundMustNotRun)

    with pytest.raises(HTTPException) as caught:
        await proxy_service.proxy_request(
            model=stale,
            api_key_id=None,
            user_id=1,
            department_id=None,
            request_body={"model": stale.name, "messages": []},
            endpoint_path="/v1/chat/completions",
            task_id=1,
            inference_callsite_id="csp.chat_model",
            governance_db=db,
            admitted_classification_level="機密",
        )
    assert caught.value.status_code in {403, 409}


@pytest.mark.asyncio
async def test_terminal_run_between_ceiling_and_sink_is_zero_egress(
    db: Session, monkeypatch,
) -> None:
    user = make_user(db, username="terminal_sink_owner")
    model = make_model(db, name="terminal-sink-model")
    task = Task(
        title="terminal race",
        task_type="query",
        requester_user_id=user.id,
        status="cancelled",
    )
    db.add(task)
    db.flush()
    run = TaskRun(
        task_id=task.id,
        run_sequence=1,
        dispatch_target="model",
        status="cancelled",
    )
    db.add(run)
    db.commit()
    monkeypatch.setattr(proxy_service.httpx, "AsyncClient", _OutboundMustNotRun)

    with pytest.raises(HTTPException, match="終態後"):
        await proxy_service.proxy_request(
            model=model,
            api_key_id=None,
            user_id=user.id,
            department_id=None,
            request_body={"model": model.name, "messages": []},
            endpoint_path="/v1/chat/completions",
            task_id=task.id,
            task_run_id=run.id,
            inference_callsite_id="csp.chat_model",
            governance_db=db,
            admitted_classification_level="無機密",
        )


@pytest.mark.parametrize("mutation", ["ceiling", "endpoint"])
def test_agent_registry_mutation_after_ceiling_check_is_denied_under_row_lock(
    db: Session, mutation: str,
) -> None:
    owner = make_user(db, username=f"agent_toctou_{mutation}", role="developer")
    agent = make_agent(
        db,
        owner,
        name=f"agent-toctou-{mutation}",
        approval_status="approved",
    )
    agent.classification_ceiling = "機密"
    db.commit()
    expected_endpoint = agent.endpoint_url
    if mutation == "ceiling":
        agent.classification_ceiling = "無機密"
    else:
        agent.endpoint_url = "http://swapped-agent:9100"
    db.commit()

    with pytest.raises(HTTPException) as caught:
        proxy_service.lock_agent_registry_admission(
            governance_db=db,
            agent_id=agent.id,
            endpoint_url=expected_endpoint,
            admitted_classification_level="機密",
        )
    assert caught.value.status_code in {403, 409}


def test_pilot_hides_agents_and_hard_denies_public_inference_bypasses(
    client: TestClient, db: Session, monkeypatch,
) -> None:
    user = make_user(db, username="pilot_surface_user", role="admin")
    make_model(db, name="pilot-public-embed")
    monkeypatch.setattr(settings, "ANILA_PILOT_MODE", True)
    headers = _bearer(user)

    agents = client.get("/v1/agents", headers=headers)
    assert agents.status_code == 200
    assert agents.json()["data"] == []
    for path in ("/v1/embeddings", "/v2/embeddings"):
        response = client.post(
            path,
            headers=headers,
            json={"model": "pilot-public-embed", "input": "secret"},
        )
        assert response.status_code == 403
    resumed = client.post(
        "/v1/agents/anything/sessions/session-1/answer",
        headers=headers,
        json={"interrupt_id": "i", "answer": "secret"},
    )
    assert resumed.status_code == 403
    image = client.post(
        "/v1/images/generations",
        headers=headers,
        json={"prompt": "secret"},
    )
    assert image.status_code == 403


def test_classification_propagation_failure_atomically_fails_task_zero_egress(
    client: TestClient, db: Session, monkeypatch,
) -> None:
    user = make_user(db, username="propagation_fail_user", role="admin")
    model = make_model(db, name="propagation-fail-model")
    conversation = Conversation(
        user_id=user.id,
        title="classified source",
        classification_level="機密",
    )
    task = Task(
        title="propagation failure",
        task_type="query",
        requester_user_id=user.id,
        status="submitted",
    )
    db.add_all([conversation, task])
    db.commit()

    def fail_propagation(*args, **kwargs):
        raise RuntimeError("synthetic propagation failure")

    monkeypatch.setattr(
        proxy_api, "_propagate_conversation_level_to_task", fail_propagation
    )
    monkeypatch.setattr(proxy_service.httpx, "AsyncClient", _OutboundMustNotRun)
    response = client.post(
        "/v1/chat/completions",
        headers={
            **_bearer(user),
            "X-ANILA-Task-Id": str(task.id),
            "X-ANILA-Conversation-Id": str(conversation.id),
        },
        json={
            "model": model.name,
            "messages": [{"role": "user", "content": "classified"}],
        },
    )
    assert response.status_code == 503
    db.expire_all()
    assert db.get(Task, task.id).status == "failed"
    run = db.query(TaskRun).filter_by(task_id=task.id).one()
    assert run.status == "failed"
    assert run.error["code"] == "task_classification_propagation"
    assert db.query(PolicyDecision).filter_by(
        task_id=task.id, action="model.invoke", decision="deny"
    ).count() == 1
    assert db.query(AuditLog).filter_by(
        resource_type="task",
        resource_id=str(task.id),
        action="task.pre_dispatch.failed",
    ).count() == 1
