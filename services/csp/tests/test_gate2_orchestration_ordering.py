"""Focused Gate 2 orchestration ordering and zero-egress contracts.

These tests deliberately observe the durable Task/TaskRun spine at each
hidden-inference and foreground-inference boundary.  They keep network I/O
mocked while exercising the real endpoint ordering and proxy admission code.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

os.environ.setdefault("ANILA_ALLOW_DEV_SECRET", "1")

import pytest
from anila_security import PilotTarget, VerifiedPilotAdmission
from anila_contracts import Classification
from anila_core.memory import EMBED_DIM, EMBED_NATIVE_DIM
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app.api import proxy as proxy_api
from app.config import settings
from app.models.conversation import Conversation
from app.models.model_registry import ModelRegistry
from app.models.task import Task, TaskRun
from app.services import memory_service, retrieval_service, startup_security
from app.services.auth_service import create_tokens
from app.services.proxy import service as proxy_service
from app.services.proxy.task_link import (
    TaskRunContext,
    finalize_task_run_in_session,
)
from tests.conftest import make_model, make_user


def _bearer(user) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_tokens(user)['access_token']}"}


def _task(db: Session, user, *, status: str = "submitted") -> Task:
    row = Task(
        title="Gate 2 ordering proof",
        task_type="query",
        requester_user_id=user.id,
        status=status,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _running_context(db: Session, user) -> tuple[Task, TaskRun, TaskRunContext]:
    task = _task(db, user, status="running")
    run = TaskRun(
        task_id=task.id,
        run_sequence=1,
        dispatch_target="model",
        status="running",
        started_at=datetime.now(timezone.utc),
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    return task, run, TaskRunContext(task.id, task.trace_id, run.id)


def _assert_running(db: Session, task_ctx: TaskRunContext) -> None:
    db.expire_all()
    assert db.get(Task, task_ctx.task_id).status == "running"
    assert db.get(TaskRun, task_ctx.task_run_id).status == "running"


def test_task_run_precedes_retrieval_memory_and_foreground_send(
    client: TestClient, db: Session, monkeypatch,
) -> None:
    user = make_user(db, username="ordering-owner", role="admin")
    model = make_model(db, name="ordering-model")
    model.classification_ceiling = Classification.TOP_SECRET.to_storage()
    db.commit()
    task = _task(db, user)
    observed: list[str] = []

    async def retrieval_stage(stage_db, *, body, task_ctx, **kwargs):
        _assert_running(stage_db, task_ctx)
        assert task_ctx.task_id == task.id
        assert body.pop("anila_retrieval")["collection_id"] == 77
        observed.append("retrieval_embedding")
        return None

    async def memory_stage(stage_db, _user_id, _body, *, task_ctx, **kwargs):
        _assert_running(stage_db, task_ctx)
        observed.append("memory_recall_embedding")
        return memory_service.MemoryReadResult(block=None, facts_count=0, chunks=[])

    async def foreground_send(**kwargs):
        task_ctx = TaskRunContext(
            kwargs["task_id"], kwargs["task_trace_id"], kwargs["task_run_id"]
        )
        _assert_running(kwargs["governance_db"], task_ctx)
        observed.append("foreground_send")
        finalize_task_run_in_session(
            kwargs["governance_db"], task_ctx.task_run_id, "completed"
        )
        return {"choices": [{"message": {"content": "ok"}}]}

    monkeypatch.setattr(proxy_api, "_prepare_server_retrieval", retrieval_stage)
    monkeypatch.setattr(proxy_api, "_inject_memory", memory_stage)
    monkeypatch.setattr(proxy_api, "proxy_request", foreground_send)
    monkeypatch.setattr(proxy_api, "_schedule_memory_write", lambda **kwargs: None)

    response = client.post(
        "/v1/chat/completions",
        headers={**_bearer(user), "X-ANILA-Task-Id": str(task.id)},
        json={
            "model": model.name,
            "messages": [{"role": "user", "content": "ordered"}],
            "anila_retrieval": {"collection_id": 77},
        },
    )

    assert response.status_code == 200, response.text
    assert observed == [
        "retrieval_embedding",
        "memory_recall_embedding",
        "foreground_send",
    ]
    db.expire_all()
    run = db.query(TaskRun).filter_by(task_id=task.id).one()
    assert run.status == "completed"
    assert db.get(Task, task.id).status == "completed"


@pytest.mark.parametrize(
    ("target_state", "expected_status"),
    [("missing", 404), ("inactive", 400)],
)
def test_missing_or_inactive_target_is_zero_egress_before_task_run(
    client: TestClient,
    db: Session,
    monkeypatch,
    target_state: str,
    expected_status: int,
) -> None:
    user = make_user(db, username=f"target-{target_state}", role="admin")
    task = _task(db, user)
    model_name = f"target-{target_state}-model"
    if target_state == "inactive":
        model = make_model(db, name=model_name)
        model.is_active = False
        db.commit()

    async def must_not_run(*args, **kwargs):
        raise AssertionError("missing/inactive target must be zero outbound")

    monkeypatch.setattr(proxy_api, "_prepare_server_retrieval", must_not_run)
    monkeypatch.setattr(proxy_api, "_inject_memory", must_not_run)
    monkeypatch.setattr(proxy_api, "proxy_request", must_not_run)

    response = client.post(
        "/v1/chat/completions",
        headers={**_bearer(user), "X-ANILA-Task-Id": str(task.id)},
        json={
            "model": model_name,
            "messages": [{"role": "user", "content": "must not leave"}],
        },
    )

    assert response.status_code == expected_status
    db.expire_all()
    assert db.query(TaskRun).filter_by(task_id=task.id).count() == 0
    assert db.get(Task, task.id).status == "submitted"


@pytest.mark.parametrize(
    ("case", "expected_status"),
    [("malformed", 400), ("foreign", 403), ("terminal", 409)],
)
def test_invalid_task_authority_is_zero_egress(
    client: TestClient,
    db: Session,
    monkeypatch,
    case: str,
    expected_status: int,
) -> None:
    caller = make_user(db, username=f"invalid-task-{case}")
    owner = caller if case != "foreign" else make_user(db, username="task-owner")
    model = make_model(db, name=f"invalid-task-{case}-model")
    model.is_router_primary = True
    db.commit()
    task = _task(db, owner, status="completed" if case == "terminal" else "submitted")
    raw_task_id = "not-an-integer" if case == "malformed" else str(task.id)

    async def must_not_run(*args, **kwargs):
        raise AssertionError("invalid task authority must be zero outbound")

    monkeypatch.setattr(proxy_api, "_prepare_server_retrieval", must_not_run)
    monkeypatch.setattr(proxy_api, "_inject_memory", must_not_run)
    monkeypatch.setattr(proxy_api, "proxy_request", must_not_run)

    response = client.post(
        "/v1/chat/completions",
        headers={**_bearer(caller), "X-ANILA-Task-Id": raw_task_id},
        json={
            "model": model.name,
            "messages": [{"role": "user", "content": "must not leave"}],
        },
    )

    assert response.status_code == expected_status
    db.expire_all()
    assert db.query(TaskRun).filter_by(task_id=task.id).count() == 0
    assert db.get(Task, task.id).status == (
        "completed" if case == "terminal" else "submitted"
    )


def test_pilot_denied_hidden_callsite_closes_open_run_and_stays_zero_egress(
    client: TestClient, db: Session, monkeypatch,
) -> None:
    user = make_user(db, username="pilot-hidden-owner", role="admin")
    model = make_model(db, name="pilot-hidden-model")
    model.classification_ceiling = Classification.TOP_SECRET.to_storage()
    db.commit()
    task = _task(db, user)
    monkeypatch.setattr(settings, "ANILA_PILOT_MODE", True)
    monkeypatch.setattr(
        startup_security,
        "_verified_pilot_admission",
        VerifiedPilotAdmission(
            profile_id="ordering-test",
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
        ),
    )

    async def hidden_memory_call(stage_db, _user_id, body, *, task_ctx, **kwargs):
        _assert_running(stage_db, task_ctx)
        return await proxy_service.proxy_request(
            model=model,
            api_key_id=None,
            user_id=user.id,
            department_id=None,
            request_body={"model": model.name, "input": "classified recall"},
            endpoint_path="/v1/embeddings",
            task_id=task_ctx.task_id,
            task_trace_id=task_ctx.trace_id,
            task_run_id=task_ctx.task_run_id,
            inference_callsite_id="csp.memory_embedding",
            governance_db=stage_db,
            admitted_classification_level=Classification.UNCLASSIFIED.to_storage(),
            finalize_task_run_on_completion=False,
        )

    async def must_not_send(*args, **kwargs):
        raise AssertionError("denied hidden pilot callsite must be zero outbound")

    monkeypatch.setattr(proxy_api, "_inject_memory", hidden_memory_call)
    monkeypatch.setattr(proxy_service, "_proxy_request_impl", must_not_send)
    monkeypatch.setattr(proxy_api, "proxy_request", must_not_send)

    response = client.post(
        "/v1/chat/completions",
        headers={**_bearer(user), "X-ANILA-Task-Id": str(task.id)},
        json={
            "model": model.name,
            "messages": [{"role": "user", "content": "pilot"}],
        },
    )

    assert response.status_code == 403
    db.expire_all()
    run = db.query(TaskRun).filter_by(task_id=task.id).one()
    assert run.status == "failed"
    assert db.get(Task, task.id).status == "blocked_by_policy"


@pytest.mark.parametrize(
    ("failed_stage", "expected_code"),
    [
        ("retrieval", "synthetic_retrieval_failure"),
        ("memory", "memory_failed"),
        ("latch", "memory_classification_latch"),
    ],
)
def test_governance_stage_failure_is_zero_egress_and_closes_open_run(
    client: TestClient,
    db: Session,
    monkeypatch,
    failed_stage: str,
    expected_code: str,
) -> None:
    user = make_user(db, username=f"stage-failure-{failed_stage}", role="admin")
    model = make_model(db, name=f"stage-failure-{failed_stage}-model")
    model.classification_ceiling = Classification.TOP_SECRET.to_storage()
    conversation = Conversation(
        user_id=user.id,
        title="stage failure context",
        classification_level=Classification.UNCLASSIFIED.to_storage(),
    )
    db.add(conversation)
    db.commit()
    task = _task(db, user)

    async def retrieval_stage(*args, **kwargs):
        task_ctx = kwargs["task_ctx"]
        _assert_running(args[0], task_ctx)
        if failed_stage == "retrieval":
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "synthetic_retrieval_failure",
                    "message": "synthetic retrieval failure",
                },
            )
        return None

    async def memory_stage(*args, **kwargs):
        task_ctx = kwargs["task_ctx"]
        _assert_running(args[0], task_ctx)
        if failed_stage == "memory":
            raise RuntimeError("synthetic memory failure")
        if failed_stage == "latch":
            return memory_service.MemoryReadResult(
                block="classified recall",
                facts_count=1,
                chunks=[],
                inherited_classification=Classification.TOP_SECRET,
            )
        return memory_service.MemoryReadResult(block=None, facts_count=0, chunks=[])

    def latch(*args, **kwargs):
        if failed_stage == "latch":
            raise RuntimeError("synthetic latch failure")

    async def must_not_send(*args, **kwargs):
        raise AssertionError("governance stage failure must be zero outbound")

    monkeypatch.setattr(proxy_api, "_prepare_server_retrieval", retrieval_stage)
    monkeypatch.setattr(proxy_api, "_inject_memory", memory_stage)
    monkeypatch.setattr(proxy_api, "_latch_inherited_classification", latch)
    monkeypatch.setattr(proxy_api, "proxy_request", must_not_send)

    response = client.post(
        "/v1/chat/completions",
        headers={
            **_bearer(user),
            "X-ANILA-Task-Id": str(task.id),
            "X-ANILA-Conversation-Id": str(conversation.id),
        },
        json={
            "model": model.name,
            "messages": [{"role": "user", "content": "fail closed"}],
        },
    )

    assert response.status_code == 503
    db.expire_all()
    run = db.query(TaskRun).filter_by(task_id=task.id).one()
    assert run.status == "failed"
    assert run.error["code"] == expected_code
    assert db.get(Task, task.id).status == "failed"


@pytest.mark.asyncio
async def test_nested_proxy_success_keeps_outer_run_running_and_ends_transaction(
    db_engine, monkeypatch,
) -> None:
    factory = sessionmaker(bind=db_engine, expire_on_commit=False)
    seed = factory()
    user = make_user(seed, username="nested-proxy-owner", role="admin")
    model = make_model(seed, name="nested-proxy-model")
    model.classification_ceiling = Classification.TOP_SECRET.to_storage()
    task, run, task_ctx = _running_context(seed, user)
    seed.commit()
    seed.close()

    governance_db = factory()
    governed_model = governance_db.get(ModelRegistry, model.id)

    async def nested_impl(**kwargs):
        _assert_running(kwargs["governance_db"], task_ctx)
        return {"data": [{"embedding": [0.1, 0.2]}]}

    monkeypatch.setattr(settings, "ANILA_PILOT_MODE", False)
    monkeypatch.setattr(proxy_service, "_proxy_request_impl", nested_impl)
    result = await proxy_service.proxy_request(
        model=governed_model,
        api_key_id=None,
        user_id=user.id,
        department_id=None,
        request_body={"model": model.name, "input": "nested"},
        endpoint_path="/v1/embeddings",
        task_id=task.id,
        task_trace_id=task.trace_id,
        task_run_id=run.id,
        inference_callsite_id="csp.server_retrieval_embedding",
        governance_db=governance_db,
        admitted_classification_level=Classification.UNCLASSIFIED.to_storage(),
        finalize_task_run_on_completion=False,
    )

    assert result["data"][0]["embedding"] == [0.1, 0.2]
    assert not governance_db.in_transaction()
    verify = factory()
    try:
        assert verify.get(Task, task.id).status == "running"
        assert verify.get(TaskRun, run.id).status == "running"
    finally:
        verify.close()
        governance_db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("response_dim", [EMBED_DIM, EMBED_NATIVE_DIM])
async def test_retrieval_embedding_carries_context_without_finalizing_outer_run(
    db: Session, db_engine, monkeypatch, response_dim: int,
) -> None:
    user = make_user(
        db, username=f"retrieval-context-owner-{response_dim}", role="admin"
    )
    model = make_model(db, name=f"retrieval-context-embedding-{response_dim}")
    model.model_type = "embedding"
    model.classification_ceiling = Classification.TOP_SECRET.to_storage()
    task, run, task_ctx = _running_context(db, user)
    factory = sessionmaker(bind=db_engine, expire_on_commit=False)
    monkeypatch.setattr(retrieval_service, "SessionLocal", factory)
    observed: dict = {}

    async def nested_proxy(**kwargs):
        observed.update(kwargs)
        _assert_running(kwargs["governance_db"], task_ctx)
        return {"data": [{"embedding": [0.1] * response_dim}]}

    monkeypatch.setattr(retrieval_service, "proxy_request", nested_proxy)
    vector = await retrieval_service.embed_query(
        db,
        user,
        model.name,
        EMBED_DIM,
        "retrieval query",
        task_ctx=task_ctx,
        trusted_classification_level=Classification.UNCLASSIFIED,
    )

    assert vector == [0.1] * EMBED_DIM
    assert observed["task_id"] == task.id
    assert observed["task_run_id"] == run.id
    assert observed["finalize_task_run_on_completion"] is False
    db.expire_all()
    assert db.get(TaskRun, run.id).status == "running"


@pytest.mark.asyncio
# `EMBED_DIM - 1`(3999)先前在這裡,但**維度自適應之後它不再是錯誤**:
# `truncate_embedding` 已改成對 1..EMBED_DIM-1 的短向量補零(為了讓
# nemotron-3-embed 的 2048 維能用),所以 3999 會被補到 4000 而不 raise。
# 那個參數是唯一讓 CI 的「Backend / CSP full suite」變紅的原因
# (1 failed / 1833 passed),而它是 stale test 不是實作缺陷。
#
# 現行的「非預期維度」集合:
#   0                     → raise（空向量)
#   1 .. EMBED_DIM-1      → **補零接受**（見下方 …pads_short_dimension 測試)
#   EMBED_DIM             → 原樣接受
#   EMBED_NATIVE_DIM      → 截斷到 EMBED_DIM
#   其餘 > EMBED_DIM      → raise（盲目截斷非 Matryoshka 模型會靜默破壞檢索)
@pytest.mark.parametrize("response_dim", [0, EMBED_NATIVE_DIM + 512])
async def test_retrieval_embedding_rejects_unexpected_dimension_before_use(
    db: Session, db_engine, monkeypatch, response_dim: int,
) -> None:
    user = make_user(
        db, username=f"retrieval-invalid-dim-owner-{response_dim}", role="admin"
    )
    model = make_model(db, name=f"retrieval-invalid-dim-model-{response_dim}")
    model.model_type = "embedding"
    model.classification_ceiling = Classification.TOP_SECRET.to_storage()
    db.commit()
    factory = sessionmaker(bind=db_engine, expire_on_commit=False)
    monkeypatch.setattr(retrieval_service, "SessionLocal", factory)

    async def wrong_dimension_proxy(**kwargs):
        return {"data": [{"embedding": [0.2] * response_dim}]}

    monkeypatch.setattr(retrieval_service, "proxy_request", wrong_dimension_proxy)
    with pytest.raises(retrieval_service.RetrievalFailure) as excinfo:
        await retrieval_service.embed_query(
            db,
            user,
            model.name,
            EMBED_DIM,
            "retrieval query",
            trusted_classification_level=Classification.UNCLASSIFIED,
        )
    assert excinfo.value.code == "embedding_dimension_mismatch"
    assert str(response_dim) in str(excinfo.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("response_dim", [1, 2048, EMBED_DIM - 1])
async def test_retrieval_embedding_pads_declared_short_dimension(
    db: Session, db_engine, monkeypatch, response_dim: int,
) -> None:
    """**已宣告**維度的短向量補零到 EMBED_DIM,而不是被拒。

    為什麼要有這支:維度自適應(讓 nemotron-3-embed 的 2048 維可用)讓
    `truncate_embedding` 接受較小維度。上面那支測試因此有一個參數過時 ——
    但**光把過時參數拿掉,新行為就沒有任何測試守著**,那才是真正的風險。

    ⚠ 關鍵:補零**必須先宣告** `ANILA_EMBED_SOURCE_DIM`。無條件接受任何短向量
    會讓端點悄悄換模型時(例如 1536 維)語意無意義的向量進索引 —— 那是
    ingestion-worker 與 Python security contracts 兩個 CI job 抓到的真退化,
    見 `test_retrieval_embedding_rejects_undeclared_short_dimension`。

    這支把補零路徑釘住:長度正好 EMBED_DIM、前 response_dim 維原值不動、
    其餘全 0.0。補零對 cosine 相似度無損(索引用 halfvec_cosine_ops)。
    """
    monkeypatch.setattr(
        retrieval_service.settings, "ANILA_EMBED_SOURCE_DIM", response_dim, raising=False
    )
    user = make_user(
        db, username=f"retrieval-pad-owner-{response_dim}", role="admin"
    )
    model = make_model(db, name=f"retrieval-pad-model-{response_dim}")
    model.model_type = "embedding"
    model.classification_ceiling = Classification.TOP_SECRET.to_storage()
    db.commit()
    factory = sessionmaker(bind=db_engine, expire_on_commit=False)
    monkeypatch.setattr(retrieval_service, "SessionLocal", factory)

    async def short_dimension_proxy(**kwargs):
        return {"data": [{"embedding": [0.25] * response_dim}]}

    monkeypatch.setattr(retrieval_service, "proxy_request", short_dimension_proxy)
    vector = await retrieval_service.embed_query(
        db,
        user,
        model.name,
        EMBED_DIM,
        "retrieval query",
        trusted_classification_level=Classification.UNCLASSIFIED,
    )

    assert len(vector) == EMBED_DIM
    assert vector[:response_dim] == [0.25] * response_dim
    assert set(vector[response_dim:]) <= {0.0}


@pytest.mark.asyncio
async def test_retrieval_embedding_rejects_undeclared_short_dimension(
    db: Session, db_engine, monkeypatch,
) -> None:
    """未宣告的短向量必須被拒 —— 這條是端點漂移的唯一防線。

    情境:`ANILA_EMBED_SOURCE_DIM` 未設(預設嚴格),而端點回 1536 維
    (例如有人把 embedding endpoint 指到了別的模型)。若這裡放行補零,
    語意無意義的向量會進索引、無聲摧毀檢索品質,而 collection 的
    `embedding_fingerprint` 抓不到(它守宣告的模型身分,不守端點實際行為)。
    """
    monkeypatch.setattr(
        retrieval_service.settings, "ANILA_EMBED_SOURCE_DIM", None, raising=False
    )
    user = make_user(db, username="retrieval-undeclared-owner", role="admin")
    model = make_model(db, name="retrieval-undeclared-model")
    model.model_type = "embedding"
    model.classification_ceiling = Classification.TOP_SECRET.to_storage()
    db.commit()
    factory = sessionmaker(bind=db_engine, expire_on_commit=False)
    monkeypatch.setattr(retrieval_service, "SessionLocal", factory)

    async def wrong_model_proxy(**kwargs):
        return {"data": [{"embedding": [0.3] * 1536}]}

    monkeypatch.setattr(retrieval_service, "proxy_request", wrong_model_proxy)
    with pytest.raises(retrieval_service.RetrievalFailure) as excinfo:
        await retrieval_service.embed_query(
            db,
            user,
            model.name,
            EMBED_DIM,
            "retrieval query",
            trusted_classification_level=Classification.UNCLASSIFIED,
        )
    assert excinfo.value.code == "embedding_dimension_mismatch"
    assert "1536" in str(excinfo.value)
    # 錯誤訊息要指出出路,否則 operator 只知道壞了不知道怎麼辦
    assert "ANILA_EMBED_SOURCE_DIM" in str(excinfo.value)


@pytest.mark.asyncio
async def test_memory_embedding_carries_running_task_context(
    db: Session, monkeypatch,
) -> None:
    user = make_user(db, username="memory-context-owner", role="admin")
    model = make_model(db, name="memory-context-embedding")
    model.model_type = "embedding"
    model.classification_ceiling = Classification.TOP_SECRET.to_storage()
    task, run, task_ctx = _running_context(db, user)
    monkeypatch.setattr(memory_service, "_EMBED_MODEL_NAME", model.name)
    observed: dict = {}

    async def gateway(*args, **kwargs):
        observed.update(kwargs)
        _assert_running(args[0], task_ctx)
        return {"data": [{"embedding": [0.1] * 4000}]}

    monkeypatch.setattr(memory_service, "_gateway_request", gateway)
    vector = await memory_service._embed(
        db,
        "memory recall query",
        user=user,
        conversation_id=1,
        classification_level=Classification.UNCLASSIFIED,
        task_id=task.id,
        trace_id=task.trace_id,
        task_ctx=task_ctx,
    )

    assert len(vector) == 4000
    assert observed["task_ctx"] == task_ctx
    db.expire_all()
    assert db.get(TaskRun, run.id).status == "running"
