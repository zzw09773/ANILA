"""Regression: an in-flight SSE must not pin a pooled DB connection.

2026-07-25 cold-burst outage (100 concurrent chats, slow upstream): chat
0/100 completed and **36/36 control-plane probes failed** — the whole platform
was down.  ``pg`` showed ``pg_active=31, idle_tx=30`` against a
``pool_size=10 + max_overflow=20`` pool, i.e. every pooled connection was
held by a session that had begun a transaction and never committed.

Two mechanics produced that:

1. FastAPI resolves ``get_db``'s ``finally: db.close()`` through its
   ``AsyncExitStack`` only **after** ``StreamingResponse`` has fully drained,
   so the request-scoped Session outlives the whole SSE body.
2. ``expire_on_commit=True`` (SQLAlchemy default) arms every ORM instance for
   a lazy re-SELECT at ``commit()``.  ``_commit_stream_admission`` commits
   just before the upstream call, so *any* later attribute touch — e.g. the
   ``user.id`` read inside the deferred ``_schedule_memory_write`` closure —
   re-opened a transaction on the event-loop thread and re-pinned that
   connection until the stream finished.

Both assertions below are taken **from inside the stream body**.  That is the
only place the bug is observable: after the generator is exhausted the Session
has been committed/closed again, so every post-hoc assertion passes even on
the broken code.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import event
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm import Session, sessionmaker

from app.models.task import Task, TaskRun
from app.models.token_usage import TokenUsage
from app.models.trace_span import TraceSpan
from app.services import proxy_service
from app.services.proxy import service as proxy_impl

from tests.conftest import make_model, make_user


@pytest.fixture(autouse=True)
def _dev_ssrf_allowances(monkeypatch):
    """Mock upstreams are single-label http hosts; allow them through the
    call-time SSRF guard exactly like test_proxy_task_wiring.py does.  The
    guard itself is never weakened — only this fixture's hostnames."""
    monkeypatch.setenv("ANILA_ALLOW_HTTP_ENDPOINT", "1")
    monkeypatch.setenv("ANILA_TRUSTED_HOSTS", "mock-llm")


class _StreamResponse:
    status_code = 200
    headers = {"content-type": "text/event-stream"}

    def __init__(self, lines, on_first_chunk=None):
        self._lines = lines
        self._on_first_chunk = on_first_chunk

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def aread(self):
        return b""

    async def aiter_bytes(self, chunk_size: int | None = None):
        del chunk_size
        for index, line in enumerate(self._lines):
            if index == 0 and self._on_first_chunk is not None:
                # Mid-stream observation point: admission has committed and
                # the upstream body is being forwarded.  This is exactly the
                # window in which production held 30 idle-in-transaction
                # connections.
                self._on_first_chunk()
            yield (line + "\n").encode()


_SSE_LINES = [
    'data: {"choices":[{"index":0,"delta":{"content":"hi"},'
    '"finish_reason":"stop"}],"usage":{"prompt_tokens":2,'
    '"completion_tokens":2,"total_tokens":4}}',
    "",
    "data: [DONE]",
    "",
]


def _patch_stream_client(monkeypatch, on_first_chunk):
    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def stream(self, method, url, json=None, headers=None):
            return _StreamResponse(_SSE_LINES, on_first_chunk=on_first_chunk)

    monkeypatch.setattr(proxy_service.httpx, "AsyncClient", _Client)


def _pool_watch(engine):
    """Count connections currently checked out of ``engine``'s pool.

    Pool-agnostic (StaticPool has no ``checkedout()``), and it measures the
    thing that actually ran out in production rather than a proxy for it.
    """
    state = {"out": 0, "peak": 0}

    @event.listens_for(engine, "checkout")
    def _checkout(*_args):  # pragma: no cover - event hook
        state["out"] += 1
        state["peak"] = max(state["peak"], state["out"])

    @event.listens_for(engine, "checkin")
    def _checkin(*_args):  # pragma: no cover - event hook
        state["out"] -= 1

    return state


def _seed_task_stream(db: Session, username: str, model_name: str):
    user = make_user(db, username=username)
    model = make_model(db, name=model_name)
    task = Task(
        title="pool release",
        task_type="query",
        requester_user_id=user.id,
        status="running",
    )
    db.add(task)
    db.flush()
    run = TaskRun(
        task_id=task.id,
        run_sequence=1,
        dispatch_target="model",
        status="running",
    )
    db.add(run)
    db.commit()
    return user, model, task, run


def test_request_session_holds_no_pool_connection_during_stream(
    db: Session, db_engine, monkeypatch
):
    """The request-scoped Session must own zero pooled connections mid-SSE."""
    user, model, task, run = _seed_task_stream(
        db, "pool_release_user", "pool-release-model"
    )
    watch = _pool_watch(db_engine)
    observed: dict = {}

    def observe():
        # The admission Session must have been handed back before the upstream
        # body starts, not merely happen to be idle.
        observed["admission_released"] = sa_inspect(user).detached
        # A deferred ORM attribute read is what the real streaming exits do:
        # ``_schedule_memory_write(user_id=user.id, ...)`` is evaluated inside
        # an ``on_complete`` closure, long after admission committed.
        observed["user_id"] = user.id
        observed["out"] = watch["out"]
        observed["session_in_transaction"] = db.in_transaction()

    _patch_stream_client(monkeypatch, observe)
    monkeypatch.setattr(proxy_impl, "persist_task_call_closure", lambda _db, _c: 7)

    async def _run():
        chunks = []
        async for chunk in proxy_service.proxy_stream(
            target_url="http://mock-llm/v1/chat/completions",
            api_key_id=None,
            user_id=user.id,
            department_id=None,
            usage_model_id=model.id,
            request_body={"model": model.name, "messages": []},
            model_name=model.name,
            task_id=task.id,
            task_trace_id=task.trace_id,
            task_run_id=run.id,
            governance_db=db,
            registry_endpoint_url=model.endpoint_url,
            admitted_classification_level="無機密",
        ):
            chunks.append(chunk)
        return chunks

    chunks = asyncio.run(_run())

    assert chunks, "stream produced no bytes"
    assert observed, "mid-stream observation never ran"
    # The deferred ORM read must still return the right value — the fix may not
    # be "stop reading the attribute", it must be "read it without a query".
    assert observed["user_id"] == user.id
    assert observed["admission_released"] is True, (
        "admission Session was never released before the upstream body; the "
        "stream is still sitting on the request-scoped Session"
    )
    assert observed["session_in_transaction"] is False, (
        "request-scoped Session still had an open transaction mid-stream; "
        "it is holding a pooled connection for the whole SSE lifetime"
    )
    assert observed["out"] == 0, (
        f"{observed['out']} pooled connection(s) checked out mid-stream; "
        "a slow upstream will exhaust pool_size+max_overflow and take the "
        "control plane down with it"
    )


def test_stream_teardown_still_persists_closure_after_release(
    db: Session, db_engine, monkeypatch
):
    """Releasing the Session must not cost us the durable closure."""
    user, model, task, run = _seed_task_stream(
        db, "pool_release_closure", "pool-release-closure-model"
    )
    calls: list = []

    def capture(_db, closure):
        calls.append(closure)
        return 11

    _patch_stream_client(monkeypatch, None)
    monkeypatch.setattr(proxy_impl, "persist_task_call_closure", capture)

    async def _run():
        chunks = []
        async for chunk in proxy_service.proxy_stream(
            target_url="http://mock-llm/v1/chat/completions",
            api_key_id=None,
            user_id=user.id,
            department_id=None,
            usage_model_id=model.id,
            request_body={"model": model.name, "messages": []},
            model_name=model.name,
            task_id=task.id,
            task_trace_id=task.trace_id,
            task_run_id=run.id,
            governance_db=db,
            registry_endpoint_url=model.endpoint_url,
            admitted_classification_level="無機密",
        ):
            chunks.append(chunk)
        return chunks

    chunks = asyncio.run(_run())

    assert any("[DONE]" in chunk for chunk in chunks)
    # Exactly one closure — the success path must not double-persist with the
    # ``finally`` retry after the Session was released.
    assert len(calls) == 1
    assert calls[0].status == "completed"
    assert calls[0].task_id == task.id
    assert calls[0].task_run_id == run.id
    assert calls[0].trace_id == task.trace_id


def test_cancelled_stream_releases_pool_connection_and_still_closes(
    db: Session, db_engine, monkeypatch
):
    """Client disconnect (GeneratorExit) must not leak a pinned connection."""
    user, model, task, run = _seed_task_stream(
        db, "pool_release_abort", "pool-release-abort-model"
    )
    watch = _pool_watch(db_engine)
    observed: dict = {}
    calls: list = []

    def observe():
        observed["out"] = watch["out"]
        observed["session_in_transaction"] = db.in_transaction()

    _patch_stream_client(monkeypatch, observe)
    monkeypatch.setattr(
        proxy_impl,
        "persist_task_call_closure",
        lambda _db, closure: calls.append(closure) or 5,
    )

    async def _run():
        agen = proxy_service.proxy_stream(
            target_url="http://mock-llm/v1/chat/completions",
            api_key_id=None,
            user_id=user.id,
            department_id=None,
            usage_model_id=model.id,
            request_body={"model": model.name, "messages": []},
            model_name=model.name,
            task_id=task.id,
            task_trace_id=task.trace_id,
            task_run_id=run.id,
            governance_db=db,
            registry_endpoint_url=model.endpoint_url,
            admitted_classification_level="無機密",
        )
        async for _chunk in agen:
            break  # simulate a client that walks away mid-stream
        await agen.aclose()

    asyncio.run(_run())

    assert observed, "mid-stream observation never ran"
    assert observed["session_in_transaction"] is False
    assert observed["out"] == 0
    # Teardown closure is still recorded for the abandoned run.
    assert len(calls) == 1
    assert calls[0].task_run_id == run.id


def test_released_session_durably_commits_closure_rows_to_the_database(
    db: Session, db_engine, monkeypatch
):
    """End-to-end durability of the post-``close()`` write, unmocked.

    Every other test in this file substitutes ``persist_task_call_closure``,
    so none of them ever drives real SQLAlchemy through a Session that
    ``_release_stream_admission_session`` has already closed — the exact code
    path the pool-release change introduced.  Only the upstream HTTP client is
    faked here.  Durability is verified from an **independent** Session so a
    row that merely lives in ``db``'s identity map cannot pass for a commit.
    """
    user, model, task, run = _seed_task_stream(
        db, "pool_release_durable", "pool-release-durable-model"
    )
    task_id, run_id, trace_id = task.id, run.id, task.trace_id
    released: dict = {}

    real_release = proxy_impl._release_stream_admission_session

    def tracking_release(governance_db):
        real_release(governance_db)
        released["closed"] = not db.in_transaction()

    monkeypatch.setattr(
        proxy_impl, "_release_stream_admission_session", tracking_release
    )
    _patch_stream_client(monkeypatch, None)
    # persist_task_call_closure is deliberately NOT patched.

    async def _run():
        chunks = []
        async for chunk in proxy_service.proxy_stream(
            target_url="http://mock-llm/v1/chat/completions",
            api_key_id=None,
            user_id=user.id,
            department_id=None,
            usage_model_id=model.id,
            request_body={"model": model.name, "messages": []},
            model_name=model.name,
            task_id=task_id,
            task_trace_id=trace_id,
            task_run_id=run_id,
            governance_db=db,
            registry_endpoint_url=model.endpoint_url,
            admitted_classification_level="無機密",
        ):
            chunks.append(chunk)
        return chunks

    chunks = asyncio.run(_run())
    assert any("[DONE]" in chunk for chunk in chunks)
    assert released.get("closed") is True, (
        "the admission Session was never actually released before the body"
    )

    # Independent Session: nothing below can be satisfied by ``db``'s memory.
    verify = sessionmaker(bind=db_engine, expire_on_commit=False)()
    try:
        spans = (
            verify.query(TraceSpan)
            .filter(TraceSpan.task_id == task_id)
            .order_by(TraceSpan.id)
            .all()
        )
        assert [s.span_type for s in spans] == [
            "agent.run.finished",
            "agent.model_call.finished",
        ], "closure trace spans were not committed through the released Session"
        assert all(s.status == "ok" for s in spans)
        assert spans[1].parent_span_id == spans[0].span_id

        usage_rows = (
            verify.query(TokenUsage).filter(TokenUsage.task_id == task_id).all()
        )
        assert len(usage_rows) == 1, "token usage was not durably written"
        assert usage_rows[0].total_tokens == 4
        assert spans[0].attributes["usage_record_id"] == usage_rows[0].id

        persisted_run = verify.get(TaskRun, run_id)
        assert persisted_run.status == "completed"
        assert persisted_run.finished_at is not None
        assert persisted_run.usage_record_id == usage_rows[0].id
        # task_type='query' parks the parent task for the next turn.
        assert verify.get(Task, task_id).status == "waiting_for_user"
    finally:
        verify.close()


@pytest.mark.parametrize("attribute", ["id", "username", "role"])
def test_loaded_attributes_survive_the_stream_session_release(
    db: Session, monkeypatch, attribute
):
    """Values already loaded before the stream must stay readable afterwards.

    The release is only safe because ``SessionLocal`` no longer expires ORM
    state at commit; a detached-and-expired instance would raise
    ``DetachedInstanceError`` here instead.
    """
    user, model, task, run = _seed_task_stream(
        db, f"pool_release_attr_{attribute}", f"pool-release-attr-{attribute}"
    )
    expected = getattr(user, attribute)
    _patch_stream_client(monkeypatch, None)
    monkeypatch.setattr(proxy_impl, "persist_task_call_closure", lambda _db, _c: 3)

    async def _run():
        async for _chunk in proxy_service.proxy_stream(
            target_url="http://mock-llm/v1/chat/completions",
            api_key_id=None,
            user_id=user.id,
            department_id=None,
            usage_model_id=model.id,
            request_body={"model": model.name, "messages": []},
            model_name=model.name,
            task_id=task.id,
            task_trace_id=task.trace_id,
            task_run_id=run.id,
            governance_db=db,
            registry_endpoint_url=model.endpoint_url,
            admitted_classification_level="無機密",
        ):
            pass

    asyncio.run(_run())

    assert getattr(user, attribute) == expected
