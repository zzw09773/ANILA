from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import uuid

import pytest

from app.models.audit_log import AuditLog
from app.services import proxy_service
from app.services.proxy import service as proxy_impl
from app.services.proxy.cancellation import (
    CancellationDisposition,
    InSessionCancellationRegistry,
    StreamCancelled,
    cancellable_iter,
    registry,
)
from app.models.task import Task, TaskRun
from app.services.proxy.closure import TaskCallClosure, persist_task_call_closure
from tests.conftest import make_user


async def _wait_forever():
    while True:
        await asyncio.sleep(60)
        yield "never"


async def test_registry_signal_stops_and_closes_downstream_iterator():
    closed = asyncio.Event()

    async def source():
        try:
            yield "first"
            await asyncio.Event().wait()
        finally:
            closed.set()

    registry = InSessionCancellationRegistry()
    async with registry.register(42) as event:
        stream = cancellable_iter(source(), event)
        assert await anext(stream) == "first"
        assert (await registry.cancel(42)).disposition is CancellationDisposition.ACCEPTED
        with pytest.raises(StreamCancelled):
            await anext(stream)
    await stream.aclose()
    assert closed.is_set()
    assert (await registry.cancel(42)).disposition is CancellationDisposition.IN_PROGRESS
    assert await registry.complete(42) is True
    assert (await registry.cancel(42)).disposition is CancellationDisposition.NO_ACTIVE_STREAM


async def test_duplicate_cancel_signal_has_single_terminal_exception():
    async def source():
        while True:
            await asyncio.sleep(60)
            yield "never"

    registry = InSessionCancellationRegistry()
    async with registry.register(7) as event:
        stream = cancellable_iter(source(), event)
        assert (await registry.cancel(7)).accepted is True
        assert (await registry.cancel(7)).in_progress is True
        with pytest.raises(StreamCancelled):
            await anext(stream)
        with pytest.raises(StopAsyncIteration):
            await anext(stream)


async def test_cancel_is_accepted_once_and_terminal_claim_is_unique():
    registry = InSessionCancellationRegistry()
    async with registry.register(9) as event:
        assert (await registry.cancel(9)).disposition is CancellationDisposition.ACCEPTED
        assert (await registry.cancel(9)).disposition is CancellationDisposition.IN_PROGRESS
        assert await registry.claim_cancel_terminal(9, event) is True
        assert await registry.claim_cancel_terminal(9, event) is False
        assert await registry.finish(9, event) == (True, False)
        assert (await registry.cancel(9)).in_progress is True
        assert await registry.complete(9) is True
    assert (await registry.cancel(9)).disposition is CancellationDisposition.NO_ACTIVE_STREAM


async def test_terminal_owner_wins_when_root_and_nested_streams_cancel_together():
    registry = InSessionCancellationRegistry()
    async with registry.register(10) as root_event:
        async with registry.register(10) as nested_event:
            assert (await registry.cancel(10)).accepted is True
            with pytest.raises(StreamCancelled):
                await anext(cancellable_iter(_wait_forever(), nested_event))
            assert await registry.claim_cancel_terminal(10, nested_event) is False
            with pytest.raises(StreamCancelled):
                await anext(cancellable_iter(_wait_forever(), root_event))
            assert await registry.claim_cancel_terminal(10, root_event) is True
            assert (await registry.cancel(10)).in_progress is True
        assert (await registry.cancel(10)).in_progress is True
    assert await registry.complete(10) is True
    assert (await registry.cancel(10)).disposition is CancellationDisposition.NO_ACTIVE_STREAM


async def test_finish_race_cannot_let_nested_stream_claim_root_terminal():
    """The browser-facing registration is the sole terminal owner.

    ``finish`` runs in a different teardown path from the explicit
    ``StreamCancelled`` handler, so it must enforce the same owner lease or a
    nested Router hop could consume the one cancelled terminal frame first.
    """
    registry = InSessionCancellationRegistry()
    async with registry.register(11) as root_event:
        async with registry.register(11) as nested_event:
            assert (await registry.cancel(11)).disposition is CancellationDisposition.ACCEPTED
            assert await registry.finish(11, nested_event) == (True, False)
            assert await registry.finish(11, root_event) == (True, True)
            assert (await registry.cancel(11)).in_progress is True
        assert (await registry.cancel(11)).in_progress is True
    assert await registry.complete(11) is True
    assert (await registry.cancel(11)).disposition is CancellationDisposition.NO_ACTIVE_STREAM


async def test_stream_teardown_releases_cancel_state_when_closure_write_fails(monkeypatch):
    """A failed durable closure must not pin the process-local cancel lease."""
    task_id = 12

    async def upstream(**_kwargs):
        yield "data: first\n\n"
        await asyncio.Event().wait()

    monkeypatch.setattr(proxy_impl, "_proxy_stream_impl", upstream)
    monkeypatch.setattr(
        proxy_impl, "_require_pilot_sink_admission", lambda **_kwargs: None
    )
    monkeypatch.setattr(
        proxy_impl, "_lock_task_run_admission", lambda **_kwargs: "無機密"
    )
    monkeypatch.setattr(proxy_impl, "_lock_registry_admission", lambda **_kwargs: None)
    monkeypatch.setattr(proxy_impl, "_commit_stream_admission", lambda _db: None)

    def fail_closure(*_args, **_kwargs):
        raise RuntimeError("simulated closure database outage")

    monkeypatch.setattr(proxy_impl, "persist_task_call_closure", fail_closure)

    class _DB:
        def rollback(self):
            pass

    async def run():
        stream = proxy_service.proxy_stream(
            target_url="http://mock-llm/v1/chat/completions",
            api_key_id=1,
            user_id=2,
            department_id=None,
            usage_model_id=3,
            request_body={"model": "m", "messages": []},
            model_name="m",
            task_id=task_id,
            task_trace_id="trace-cleanup",
            task_run_id=13,
            governance_db=_DB(),
            admitted_classification_level="無機密",
        )
        assert await anext(stream) == "data: first\n\n"
        assert (await registry.cancel(task_id)).disposition is CancellationDisposition.ACCEPTED
        async for _chunk in stream:
            pass

    await run()
    # ``persist_task_call_closure`` failed, but the in-memory lease is still
    # released in proxy_stream's final teardown path.
    assert (await registry.cancel(task_id)).disposition is CancellationDisposition.NO_ACTIVE_STREAM


def test_cancelled_closure_preserves_cancelled_task_and_run_terminal(db):
    user = make_user(db, username="gate4-cancel-closure")
    task = Task(
        title="cancel closure",
        task_type="query",
        requester_user_id=user.id,
        status="running",
        classification_level="無機密",
    )
    db.add(task)
    db.flush()
    run = TaskRun(
        task_id=task.id,
        run_sequence=1,
        dispatch_target="agent",
        status="running",
        classification_level="無機密",
        started_at=datetime.now(timezone.utc),
    )
    db.add(run)
    db.commit()

    persist_task_call_closure(
        db,
        TaskCallClosure(
            closure_id=uuid.uuid4().hex,
            task_id=task.id,
            task_run_id=run.id,
            trace_id=task.trace_id,
            started_at=datetime.now(timezone.utc),
            status="cancelled",
            is_agent=True,
            target_id=7,
            target_name="agent-7",
            error={"code": "cancelled"},
            classification_level="無機密",
        ),
    )

    db.expire_all()
    assert db.get(TaskRun, run.id).status == "cancelled"
    assert db.get(Task, task.id).status == "cancelled"
    audit = db.query(AuditLog).filter_by(
        action="task.run.finished", resource_id=str(task.id)
    ).one()
    # ``cancelled`` is an intentional terminal outcome, so its ledger audit
    # is successful even though the Task/TaskRun state is not ``completed``.
    assert audit.status == "success"
