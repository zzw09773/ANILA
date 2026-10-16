"""Real engine/lease/ACL with synthetic lower proxy transport; no provider network."""

import asyncio
from datetime import datetime, timedelta, timezone
import json
import threading
from uuid import uuid4

from fastapi import HTTPException
import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from sqlalchemy.pool import QueuePool

from app.api import quick_builder as api
from app.database import Base
from app.models.quick_builder import QuickRun
from app.models.artifact import Artifact, ArtifactVersion
from app.models.user import UserModelPermission
from app.schemas.quick_builder import TestCancelInput
from app.services import quick_execution as execution, quick_test_lifecycle as lifecycle
from app.services import quick_test_stream as stream
from app.services.proxy import service as proxy_service
from app.services import quick_document as documents
from tests.test_quick_builder_review import (
    _assistant,
    _graph,
    review_rows as review_rows,
)
from tests.test_quick_builder import create, http as http, people as people
from tests.test_quick_document_review import DOCUMENT_CONFIG, CONTENT, _rendered


def _chunk(text=None, *, reason=None, usage=None):
    return (
        "data: "
        + json.dumps(
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": text} if text is not None else {},
                        "finish_reason": reason,
                    }
                ],
                **({"usage": usage} if usage else {}),
            },
            ensure_ascii=False,
        )
        + "\n\n"
    )


def _event(frame):
    if frame.startswith(":"):
        return None
    lines = frame.splitlines()
    return next(line[7:] for line in lines if line.startswith("event: ")), json.loads(
        next(line[6:] for line in lines if line.startswith("data: "))
    )


async def _collect(response):
    return [
        item
        for frame in [frame async for frame in response.body_iterator]
        if (item := _event(frame))
    ]


def _transport(monkeypatch, *, parts=("合成即時回答。",), reason="stop"):
    calls = []

    async def synthetic(**kwargs):
        calls.append(kwargs)
        for text in parts:
            yield _chunk(text)
        yield _chunk(
            reason=reason,
            usage={
                "prompt_tokens": 17,
                "completion_tokens": 9,
                "total_tokens": 26,
                "reasoning_tokens": 2,
            },
        )
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(proxy_service, "proxy_stream", synthetic)
    return calls


def test_live_events_use_real_boundaries_safe_delta_and_canonical_usage(
    db, review_rows, monkeypatch
):
    owner = review_rows[0]
    assistant, version, _ = _assistant(db, review_rows, published=False)
    completed = []

    async def synthetic(**kwargs):
        yield _chunk("第一段合成回答。")
        completed.append(True)
        yield _chunk("第二段合成回答。")
        yield _chunk(
            reason="length",
            usage={
                "prompt_tokens": 17,
                "completion_tokens": 9,
                "total_tokens": 26,
                "reasoning_tokens": 2,
            },
        )
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(proxy_service, "proxy_stream", synthetic)

    async def scenario():
        nonce = uuid4()
        response = await stream.start_test_stream(
            db, owner, assistant.id, version.id, "合成問題", nonce
        )
        observed = []
        async for frame in response.body_iterator:
            event = _event(frame)
            if event is None:
                continue
            observed.append(event)
            if event[0] == "quick.step_finished":
                checkpoint = api.read_run(event[1]["run_id"], user=owner, db=db)
                assert any(
                    step["node_id"] == event[1]["payload"]["step"]["node_id"]
                    and step["status"] == event[1]["payload"]["step"]["status"]
                    for step in checkpoint["steps"]
                )
            if (
                event[0] == "quick.output_delta"
                and len([e for e, _ in observed if e == "quick.output_delta"]) == 1
            ):
                assert not completed, "Delta must arrive before transport completed"
                assert any(
                    e == "quick.step_started"
                    and data["payload"]["step"]["node_id"] == "model"
                    for e, data in observed
                )
        return observed, str(nonce)

    observed, nonce = asyncio.run(scenario())
    assert (
        observed[0][0] == "quick.run_started"
        and observed[-1][0] == "quick.run_finished"
    )
    assert [data["seq"] for _, data in observed] == list(range(1, len(observed) + 1))
    for _, data in observed:
        assert set(data) == {
            "schema_version",
            "run_id",
            "version_id",
            "revision",
            "request_id",
            "seq",
            "payload",
        }
        assert (
            data["schema_version"] == 1
            and data["version_id"] == version.id
            and data["request_id"] == nonce
        )
    result = observed[-1][1]["payload"]["result"]
    assert result["status"] == "completed" and result["answer_complete"] is False
    assert result["completion"]["finish_reason"] == "length"
    assert result["completion"]["usage"]["total_tokens"] == 26
    assert result["completion"]["usage_complete"] is True
    assert all(
        "input" not in data["payload"]["step"]
        for event, data in observed
        if event == "quick.step_started"
    )
    assert "execution_owner_token" not in json.dumps(observed)
    assert "request_hash" not in json.dumps(observed)


def test_sensitive_fragments_and_step_trace_are_masked_before_events(
    db, review_rows, monkeypatch
):
    owner = review_rows[0]
    assistant, version, _ = _assistant(db, review_rows, published=False)
    _transport(monkeypatch, parts=("安全內容。password=", "synthetic-private-secret"))

    async def scenario():
        return await _collect(
            await stream.start_test_stream(
                db,
                owner,
                assistant.id,
                version.id,
                "password=synthetic-input-secret",
                uuid4(),
            )
        )

    observed = asyncio.run(scenario())
    encoded = json.dumps(observed, ensure_ascii=False)
    assert (
        "synthetic-private-secret" not in encoded
        and "synthetic-input-secret" not in encoded
    )
    deltas = "".join(
        data["payload"]["text"]
        for event, data in observed
        if event == "quick.output_delta"
    )
    assert "安全內容" in deltas and "[已遮罩]" in deltas


def test_only_terminal_model_has_live_output_delta(db, review_rows, monkeypatch):
    owner = review_rows[0]
    assistant, version, _ = _assistant(
        db,
        review_rows,
        published=False,
        graph=_graph(
            {"id": "intermediate", "kind": "model"}, {"id": "terminal", "kind": "model"}
        ),
    )
    calls = _transport(monkeypatch)

    async def nonstream(**kwargs):
        return {
            "choices": [
                {"message": {"content": "合成中間步驟"}, "finish_reason": "stop"}
            ]
        }

    monkeypatch.setattr(proxy_service, "proxy_request", nonstream)

    async def scenario():
        return await _collect(
            await stream.start_test_stream(
                db, owner, assistant.id, version.id, "合成", uuid4()
            )
        )

    observed = asyncio.run(scenario())
    deltas = [
        data["payload"] for event, data in observed if event == "quick.output_delta"
    ]
    assert deltas and all(item["node_id"] == "terminal" for item in deltas)
    assert (
        all("合成中間步驟" not in item["text"] for item in deltas) and len(calls) == 1
    )


def test_stop_wait_detaches_but_original_run_and_uuid_continue_once(
    db, review_rows, monkeypatch
):
    owner = review_rows[0]
    assistant, version, _ = _assistant(db, review_rows, published=False)
    calls = []

    async def scenario():
        release = asyncio.Event()

        async def synthetic(**kwargs):
            calls.append(True)
            yield _chunk("第一段合成內容。")
            await release.wait()
            yield _chunk("停止等待後仍完成。")
            yield _chunk(reason="stop")
            yield "data: [DONE]\n\n"

        monkeypatch.setattr(proxy_service, "proxy_stream", synthetic)
        nonce = uuid4()
        response = await stream.start_test_stream(
            db, owner, assistant.id, version.id, "合成", nonce
        )
        iterator = response.body_iterator
        while True:
            event, data = _event(await asyncio.wait_for(anext(iterator), 2))
            if event == "quick.output_delta":
                break
        run_id = data["run_id"]
        context = stream._RUNS[run_id]
        await iterator.aclose()
        assert (
            context.observer.closed
            and not context.task.done()
            and not context.task.cancelling()
        )
        lookup = api.test_runs(
            assistant.id, version_id=version.id, request_id=nonce, user=owner, db=db
        )
        assert lookup["runs"][0]["status"] == "running"
        duplicate = await _collect(
            await stream.start_test_stream(
                db, owner, assistant.id, version.id, "合成", nonce
            )
        )
        assert len(duplicate) == 1 and duplicate[0][0] == "quick.run_snapshot"
        assert duplicate[0][1]["payload"]["result"]["id"] == run_id and len(calls) == 1
        release.set()
        await asyncio.wait_for(context.task, 2)
        result = api.read_run(run_id, user=owner, db=db)
        assert result["status"] == "completed" and result["request_id"] == str(nonce)
        assert db.query(QuickRun).count() == 1 and len(calls) == 1

    asyncio.run(scenario())


def test_ack_timeout_without_any_receiver_does_not_hang_or_cancel_run(
    db, review_rows, monkeypatch
):
    owner = review_rows[0]
    assistant, version, _ = _assistant(db, review_rows, published=False)
    _transport(monkeypatch)
    monkeypatch.setattr(stream, "ACK_SECONDS", 0.02)

    async def scenario():
        response = await stream.start_test_stream(
            db, owner, assistant.id, version.id, "合成", uuid4()
        )
        context = next(
            context
            for context in stream._RUNS.values()
            if context.assistant_id == assistant.id
        )
        await asyncio.wait_for(context.task, 2)
        await response.body_iterator.aclose()
        assert context.observer.closed and context.observer.slow
        db.expire_all()
        assert db.get(QuickRun, context.run_id).status == "completed"
        await asyncio.sleep(0)
        assert context.run_id not in stream._RUNS

    asyncio.run(scenario())


def test_real_server_cancel_revokes_before_local_task_and_blocks_late_output(
    db, review_rows, monkeypatch
):
    owner = review_rows[0]
    assistant, version, _ = _assistant(db, review_rows, published=False)

    async def scenario():
        entered = asyncio.Event()

        async def synthetic(**kwargs):
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                # Simulate an uncooperative callback returning after cancellation.
                yield _chunk("late-output-must-not-publish")
            yield _chunk(reason="stop")

        monkeypatch.setattr(proxy_service, "proxy_stream", synthetic)
        nonce = uuid4()
        response = await stream.start_test_stream(
            db, owner, assistant.id, version.id, "合成", nonce
        )
        collector = asyncio.create_task(_collect(response))
        await asyncio.wait_for(entered.wait(), 2)
        run_id = db.query(QuickRun.id).scalar()
        db.commit()
        context = stream._RUNS[run_id]
        cancelled = await api.cancel_test_run(
            run_id,
            TestCancelInput(version_id=version.id, request_id=nonce),
            user=owner,
            db=db,
        )
        assert cancelled["status"] == "cancelled"
        observed = await asyncio.wait_for(collector, 2)
        await asyncio.wait_for(context.task, 2)
        result = api.read_run(run_id, user=owner, db=db)
        assert result["status"] == "cancelled" and "late-output" not in json.dumps(
            result
        )
        assert "late-output" not in json.dumps(observed)
        repeated = await api.cancel_test_run(
            run_id,
            TestCancelInput(version_id=version.id, request_id=nonce),
            user=owner,
            db=db,
        )
        assert repeated["status"] == "cancelled"

    asyncio.run(scenario())


def test_current_permission_revocation_stops_delivery_and_next_callback(
    db, review_rows, monkeypatch
):
    owner, _, _, model, _ = review_rows
    assistant, version, _ = _assistant(db, review_rows, published=False)
    calls = _transport(monkeypatch)

    async def scenario():
        response = await stream.start_test_stream(
            db, owner, assistant.id, version.id, "合成", uuid4()
        )
        iterator = response.body_iterator
        first = _event(await anext(iterator))
        assert first[0] == "quick.run_started"
        db.query(UserModelPermission).filter_by(
            user_id=owner.id, model_id=model.id
        ).delete()
        db.commit()
        rest = [_event(frame) async for frame in iterator]
        assert not calls
        assert all(event != "quick.output_delta" for event, _ in rest if event)
        assert rest[-1][0] == "quick.typed_error"
        assert rest[-1][1]["payload"]["scope"] == "subscription"

    asyncio.run(scenario())


def test_full_registry_rejects_new_run_but_duplicate_is_snapshot(
    db, review_rows, monkeypatch
):
    owner = review_rows[0]
    assistant, version, _ = _assistant(db, review_rows, published=False)
    monkeypatch.setattr(stream, "MAX_ACTOR_RUNS", 1)

    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()

        async def synthetic(**kwargs):
            entered.set()
            await release.wait()
            yield _chunk("合成")
            yield _chunk(reason="stop")

        monkeypatch.setattr(proxy_service, "proxy_stream", synthetic)
        nonce = uuid4()
        response = await stream.start_test_stream(
            db, owner, assistant.id, version.id, "合成", nonce
        )
        collector = asyncio.create_task(_collect(response))
        await asyncio.wait_for(entered.wait(), 2)
        snapshot = await _collect(
            await stream.start_test_stream(
                db, owner, assistant.id, version.id, "合成", nonce
            )
        )
        assert len(snapshot) == 1 and snapshot[0][0] == "quick.run_snapshot"
        with pytest.raises(HTTPException) as raised:
            await stream.start_test_stream(
                db, owner, assistant.id, version.id, "合成", uuid4()
            )
        assert (
            raised.value.status_code == 429
            and raised.value.detail["type"] == "TEST_RUN_BUSY"
        )
        assert db.query(QuickRun).count() == 1
        db.commit()
        release.set()
        await asyncio.wait_for(collector, 2)

    asyncio.run(scenario())


def test_large_trace_and_final_snapshot_have_explicit_bounded_fallback():
    context = stream._Execution(None, 1, 0, 1, 1, 2, 3, str(uuid4()), 4)
    trace = {
        "node_id": "m",
        "kind": "model",
        "status": "completed",
        "elapsed_ms": 1,
        "input": {"text": "合成" * 300000},
        "output": {"text": "合成" * 300000},
        "error": None,
    }
    first = stream._frame(context, "step_finished", {"step": trace})
    assert len(first.encode()) <= stream.MAX_EVENT_BYTES
    _, data = _event(first)
    assert data["payload"]["result_available"] is True
    assert "input" not in data["payload"]["step"]
    final = stream._frame(
        context,
        "run_finished",
        {"result": {"status": "completed", "output": "合成" * 500000}},
    )
    assert len(final.encode()) <= stream.MAX_EVENT_BYTES
    assert _event(final)[1]["payload"] == {
        "status": "completed",
        "result_available": True,
    }


def test_renewal_failure_is_unconfirmed_not_a_false_user_cancel(
    db, review_rows, monkeypatch
):
    owner = review_rows[0]
    assistant, version, _ = _assistant(db, review_rows, published=False)
    monkeypatch.setattr(execution, "TEST_LEASE_HEARTBEAT_SECONDS", 0.01)

    renewal_calls = []

    def unavailable(*args, **kwargs):
        renewal_calls.append(True)
        raise OperationalError("synthetic DB", {}, RuntimeError("synthetic failure"))

    monkeypatch.setattr(lifecycle, "renew_lease", unavailable)

    async def scenario():
        release = asyncio.Event()

        async def synthetic(**kwargs):
            try:
                await release.wait()
            except asyncio.CancelledError:
                # A dispatched provider may still finish cancellation later.
                await release.wait()
            yield _chunk("must-not-publish")

        monkeypatch.setattr(proxy_service, "proxy_stream", synthetic)
        response = await stream.start_test_stream(
            db, owner, assistant.id, version.id, "合成", uuid4()
        )
        context = next(
            context
            for context in stream._RUNS.values()
            if context.assistant_id == assistant.id
        )
        observed = await asyncio.wait_for(_collect(response), 2)
        assert renewal_calls
        release.set()
        await asyncio.wait_for(context.task, 2)
        return observed

    observed = asyncio.run(scenario())
    db.expire_all()
    row = db.query(QuickRun).one()
    assert row.status == "running", (
        "Uncertain renewal must not invent user cancellation"
    )
    lease = lifecycle.RunLease(row.id, row.user_id, row.execution_owner_token)
    row.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.commit()
    result = api.read_run(row.id, user=owner, db=db)
    assert (
        result["status"] == "interrupted"
        and result["error"]["type"] == "RESULT_UNCONFIRMED"
    )
    with pytest.raises(HTTPException):
        execution._finish(
            db,
            owner,
            assistant.id,
            row.id,
            {"status": "completed", "output": "late", "steps": []},
            [],
            {},
            lease=lease,
        )
    assert api.read_run(row.id, user=owner, db=db)["status"] == "interrupted"
    assert not any(event == "quick.run_finished" for event, _ in observed)


def test_dequeued_delta_is_dropped_when_real_renewal_detaches_in_same_turn(
    db, review_rows, monkeypatch
):
    owner = review_rows[0]
    assistant, version, _ = _assistant(db, review_rows, published=False)
    requested_failure = threading.Event()
    original_wait = asyncio.wait
    model_calls = []
    monkeypatch.setattr(execution, "TEST_LEASE_HEARTBEAT_SECONDS", 0.002)

    def renewal(*args, **kwargs):
        if requested_failure.is_set():
            raise OperationalError(
                "synthetic renewal", {}, RuntimeError("synthetic unconfirmed")
            )
        return True

    async def provider(**kwargs):
        model_calls.append(True)
        yield _chunk("queued-before-authority-became-unconfirmed")
        yield _chunk(reason="stop")
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(lifecycle, "renew_lease", renewal)
    monkeypatch.setattr(proxy_service, "proxy_stream", provider)

    async def scenario():
        response = await stream.start_test_stream(
            db, owner, assistant.id, version.id, "合成", uuid4()
        )
        context = next(
            value
            for value in stream._RUNS.values()
            if value.assistant_id == assistant.id
        )

        async def delay_return_until_real_detach(tasks, **kwargs):
            result = await original_wait(tasks, **kwargs)
            for done in result[0]:
                if (
                    done is context.task
                    or done.cancelled()
                    or done.exception() is not None
                ):
                    continue
                item = done.result()
                if (
                    isinstance(item, tuple)
                    and len(item) == 3
                    and item[0] == "output_delta"
                ):
                    requested_failure.set()
                    await asyncio.wait_for(context.observer.detached.wait(), 2)
                    break
            return result

        monkeypatch.setattr(asyncio, "wait", delay_return_until_real_detach)
        observed = await asyncio.wait_for(_collect(response), 2)
        await asyncio.wait_for(context.task, 2)
        assert requested_failure.is_set() and context.observer.closed
        assert context.observer.queue.empty() and not context.observer.waiting
        assert not any(event == "quick.output_delta" for event, _ in observed)
        assert not any(event == "quick.run_finished" for event, _ in observed)
        assert observed[-1][0] == "quick.typed_error"
        assert observed[-1][1]["payload"]["scope"] == "subscription"
        db.expire_all()
        assert db.get(QuickRun, context.run_id).status == "running"
        assert len(model_calls) == 1 and db.query(QuickRun).count() == 1

    asyncio.run(scenario())


def test_single_connection_pool_is_released_before_every_observer_wait(
    tmp_path, monkeypatch
):
    engine = create_engine(
        "sqlite:///" + str(tmp_path / "test-stream-pool.db"),
        poolclass=QueuePool,
        pool_size=1,
        max_overflow=0,
        pool_timeout=0.2,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    try:
        with Session(bind=engine, expire_on_commit=False) as request_db:
            rows = review_rows.__wrapped__(request_db)
            owner = rows[0]
            assistant, version, _ = _assistant(request_db, rows, published=False)
            _transport(monkeypatch)

            async def scenario():
                return await _collect(
                    await stream.start_test_stream(
                        request_db, owner, assistant.id, version.id, "合成", uuid4()
                    )
                )

            events = asyncio.run(scenario())
            assert events[-1][0] == "quick.run_finished"
            assert events[-1][1]["payload"]["result"]["status"] == "completed"
            assert not request_db.in_transaction() and engine.pool.checkedout() == 0
    finally:
        engine.dispose()


def test_busy_renewal_retries_metadata_without_repeating_model_or_cancelling(
    db, review_rows, monkeypatch
):
    owner = review_rows[0]
    assistant, version, _ = _assistant(db, review_rows, published=False)
    monkeypatch.setattr(execution, "TEST_LEASE_HEARTBEAT_SECONDS", 0.005)
    monkeypatch.setattr(execution, "TEST_LEASE_BUSY_RETRY_SECONDS", 0.005)
    renewals, model_calls = [], []

    class Busy(Exception):
        pgcode = "55P03"

    def renewal(*args, **kwargs):
        renewals.append(True)
        if len(renewals) == 1:
            raise OperationalError("synthetic SQL", {}, Busy("synthetic lock busy"))
        return True

    async def synthetic(**kwargs):
        model_calls.append(True)
        deadline = asyncio.get_running_loop().time() + 2
        while len(renewals) < 2:
            assert asyncio.get_running_loop().time() < deadline
            await asyncio.sleep(0.001)
        return {
            "choices": [{"message": {"content": "合成回覆"}, "finish_reason": "stop"}]
        }

    monkeypatch.setattr(lifecycle, "renew_lease", renewal)
    monkeypatch.setattr(proxy_service, "proxy_request", synthetic)
    result = asyncio.run(
        execution.run(
            db,
            owner,
            assistant.id,
            version.id,
            "合成",
            testing=True,
            test_request_id=uuid4(),
        )
    )
    assert (
        result["status"] == "completed" and len(model_calls) == 1 and len(renewals) >= 2
    )


def test_http_optin_generates_uuid_and_duplicate_is_snapshot_without_redispatch(
    http, people, monkeypatch
):
    calls = _transport(monkeypatch)
    item, _, headers = create(http, people)
    assistant_id, version_id = item["id"], item["versions"][0]["id"]
    body = {"version_id": version_id, "message": "合成 HTTP 串流", "stream": True}
    response = http.post(
        f"/api/quick-builder/{assistant_id}/test", json=body, headers=headers
    )
    assert response.status_code == 200 and response.headers["content-type"].startswith(
        "text/event-stream"
    )
    assert response.headers["cache-control"] == "no-store"
    events = [_event(block) for block in response.text.strip().split("\n\n")]
    assert events[0][0] == "quick.run_started" and events[-1][0] == "quick.run_finished"
    result = events[-1][1]["payload"]["result"]
    assert result["status"] == "completed" and result["request_id"]
    assert result["completion"]["usage"]["total_tokens"] == 26
    body["request_id"] = result["request_id"]
    duplicate = http.post(
        f"/api/quick-builder/{assistant_id}/test", json=body, headers=headers
    )
    snapshots = [_event(block) for block in duplicate.text.strip().split("\n\n")]
    assert (
        len(snapshots) == 1
        and snapshots[0][0] == "quick.run_snapshot"
        and len(calls) == 1
    )
    legacy = http.post(
        f"/api/quick-builder/{assistant_id}/test",
        json={**body, "stream": False},
        headers=headers,
    )
    assert (
        legacy.status_code == 200
        and legacy.json()["id"] == result["id"]
        and len(calls) == 1
    )
    cancel_path = f"/api/quick-builder/runs/{result['id']}/cancel"
    cancel_body = {"version_id": version_id, "request_id": result["request_id"]}
    terminal = http.post(cancel_path, json=cancel_body, headers=headers)
    assert terminal.status_code == 200 and terminal.json()["status"] == "completed"
    wrong = http.post(
        cancel_path, json={**cancel_body, "request_id": str(uuid4())}, headers=headers
    )
    assert wrong.status_code == 404
    changed = http.post(
        cancel_path, json=cancel_body, headers={**headers, "X-ANILA-Actor-Id": "99999"}
    )
    assert (
        changed.status_code == 409
        and changed.json()["detail"]["type"] == "IDENTITY_CHANGED"
    )


def test_actor_live_cap_spans_sessions_and_stream_json_but_duplicates_still_read(
    tmp_path, monkeypatch
):
    engine = create_engine(
        "sqlite:///" + str(tmp_path / "test-actor-cap.db"),
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    try:
        with (
            Session(engine, expire_on_commit=False) as stream_db,
            Session(engine, expire_on_commit=False) as json_db,
        ):
            rows = review_rows.__wrapped__(stream_db)
            owner = rows[0]
            assistant, version, _ = _assistant(stream_db, rows, published=False)
            from app.models.user import User

            second_actor = json_db.get(User, owner.id)
            assert stream_db is not json_db
            first_connection = stream_db.connection().connection.driver_connection
            second_connection = json_db.connection().connection.driver_connection
            assert first_connection is not second_connection
            stream_db.commit()
            json_db.commit()

            async def scenario():
                stream_entered, json_entered, release = (
                    asyncio.Event(),
                    asyncio.Event(),
                    asyncio.Event(),
                )
                model_calls = []

                async def stream_transport(**kwargs):
                    model_calls.append("stream")
                    stream_entered.set()
                    await release.wait()
                    yield _chunk("合成串流")
                    yield _chunk(reason="stop")

                async def json_transport(**kwargs):
                    model_calls.append("json")
                    json_entered.set()
                    await release.wait()
                    return {
                        "choices": [
                            {
                                "message": {"content": "合成JSON"},
                                "finish_reason": "stop",
                            }
                        ]
                    }

                monkeypatch.setattr(proxy_service, "proxy_stream", stream_transport)
                monkeypatch.setattr(proxy_service, "proxy_request", json_transport)
                first_nonce, second_nonce = uuid4(), uuid4()
                response = await stream.start_test_stream(
                    stream_db, owner, assistant.id, version.id, "合成", first_nonce
                )
                receiver = asyncio.create_task(_collect(response))
                await asyncio.wait_for(stream_entered.wait(), 2)
                second = asyncio.create_task(
                    execution.run(
                        json_db,
                        second_actor,
                        assistant.id,
                        version.id,
                        "合成",
                        testing=True,
                        test_request_id=second_nonce,
                    )
                )
                await asyncio.wait_for(json_entered.wait(), 2)
                with Session(engine, expire_on_commit=False) as third_db:
                    third_actor = third_db.get(User, owner.id)
                    for streamed in (False, True):
                        with pytest.raises(HTTPException) as raised:
                            if streamed:
                                await stream.start_test_stream(
                                    third_db,
                                    third_actor,
                                    assistant.id,
                                    version.id,
                                    "合成",
                                    uuid4(),
                                )
                            else:
                                await execution.run(
                                    third_db,
                                    third_actor,
                                    assistant.id,
                                    version.id,
                                    "合成",
                                    testing=True,
                                    test_request_id=uuid4(),
                                )
                        assert (
                            raised.value.status_code == 429
                            and raised.value.detail["type"] == "TEST_RUN_BUSY"
                        )
                    duplicate_json = await execution.run(
                        third_db,
                        third_actor,
                        assistant.id,
                        version.id,
                        "合成",
                        testing=True,
                        test_request_id=first_nonce,
                    )
                    duplicate_stream = await _collect(
                        await stream.start_test_stream(
                            third_db,
                            third_actor,
                            assistant.id,
                            version.id,
                            "合成",
                            second_nonce,
                        )
                    )
                    assert duplicate_json["status"] == "running"
                    assert duplicate_stream[0][0] == "quick.run_snapshot"
                    assert (
                        duplicate_stream[0][1]["payload"]["result"]["status"]
                        == "running"
                    )
                    assert (
                        third_db.query(QuickRun).count() == 2 and len(model_calls) == 2
                    )
                    third_db.commit()
                release.set()
                assert (await asyncio.wait_for(second, 2))["status"] == "completed"
                assert (await asyncio.wait_for(receiver, 2))[-1][
                    0
                ] == "quick.run_finished"

            asyncio.run(scenario())
    finally:
        engine.dispose()


def test_expired_and_orphaned_runs_do_not_consume_cap_or_gain_a_new_producer(
    db, review_rows, monkeypatch
):
    owner = review_rows[0]
    assistant, version, _ = _assistant(db, review_rows, published=False)
    old = []
    for index in range(2):
        nonce = str(uuid4())
        row = QuickRun(
            user_id=owner.id, version_id=version.id, status="running", result={}
        )
        db.add(row)
        lifecycle.claim_new(
            db,
            row,
            request_id=nonce,
            message="合成",
            actor_department_id=owner.department_id,
        )
        if index == 0:
            row.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        else:
            row.execution_owner_token = None
        old.append((row.id, nonce))
    db.commit()
    calls = []

    async def transport(**kwargs):
        calls.append(True)
        return {"choices": [{"message": {"content": "合成"}, "finish_reason": "stop"}]}

    monkeypatch.setattr(proxy_service, "proxy_request", transport)
    result = asyncio.run(
        execution.run(
            db,
            owner,
            assistant.id,
            version.id,
            "合成",
            testing=True,
            test_request_id=uuid4(),
        )
    )
    assert result["status"] == "completed" and len(calls) == 1
    for run_id, nonce in old:
        recovered = asyncio.run(
            execution.run(
                db,
                owner,
                assistant.id,
                version.id,
                "合成",
                testing=True,
                test_request_id=nonce,
            )
        )
        assert recovered["id"] == run_id and recovered["status"] == "interrupted"
    assert len(calls) == 1 and db.query(QuickRun).count() == 3


def test_trace_artifact_projection_removes_handles_without_touching_rag_citations():
    original = {
        "steps": [
            {
                "node_id": "document",
                "input": {
                    "text": "完整合成輸入",
                    "sources": [
                        {
                            "url": "https://source.invalid/reference",
                            "download_url": "source-citation",
                        }
                    ],
                    "artifacts": [
                        {
                            "filename": "draft.html",
                            "download_url": "/api/artifacts/77",
                            "storage_ref": "private-storage",
                            "file_refs": ["private-file"],
                            "text": "完整草稿",
                        }
                    ],
                },
                "output": {
                    "text": "完整合成輸出",
                    "artifacts": [
                        {"download_url": "/api/quick-builder/runs/1/artifacts/0"}
                    ],
                },
            }
        ]
    }
    safe = execution._mask_flow_result(original)
    state = safe["steps"][0]["input"]
    assert state["artifacts"] == [{"filename": "draft.html", "text": "完整草稿"}]
    assert state["sources"][0]["url"] == "https://source.invalid/reference"
    assert state["sources"][0]["download_url"] == "source-citation"
    assert safe["steps"][0]["output"]["text"] == "完整合成輸出"
    assert (
        original["steps"][0]["input"]["artifacts"][0]["storage_ref"]
        == "private-storage"
    )


@pytest.mark.parametrize("outcome", ["complete", "cancel", "expire"])
def test_document_checkpoint_and_events_hide_handles_but_guarded_final_download_works(
    db, review_rows, monkeypatch, outcome
):
    owner = review_rows[0]
    assistant, version, _ = _assistant(
        db,
        review_rows,
        published=False,
        graph=_graph(
            {"id": "document", "kind": "document", "config": DOCUMENT_CONFIG},
            {"id": "model", "kind": "model"},
        ),
    )

    async def resolve(*args, **kwargs):
        return "https://studio.internal"

    async def render(*args, **kwargs):
        kwargs["before_send"]()
        return _rendered()

    monkeypatch.setattr(documents, "resolve_studio_url", resolve)
    monkeypatch.setattr(documents, "render_document", render)
    calls = _transport(monkeypatch)

    async def scenario():
        nonce = uuid4()
        response = await stream.start_test_stream(
            db, owner, assistant.id, version.id, "完整合成草稿", nonce
        )
        iterator = response.body_iterator
        observed = []
        while True:
            event = _event(await asyncio.wait_for(anext(iterator), 2))
            observed.append(event)
            if (
                event[0] == "quick.step_finished"
                and event[1]["payload"]["step"]["node_id"] == "document"
            ):
                break
        run_id = event[1]["run_id"]
        trace = event[1]["payload"]["step"]
        checkpoint = api.read_run(run_id, user=owner, db=db)
        assert checkpoint["status"] == "running" and checkpoint["artifacts"] == []
        for result in (
            trace,
            next(step for step in checkpoint["steps"] if step["node_id"] == "document"),
        ):
            assert (
                result["input"]["text"] == "完整合成草稿"
                and result["output"]["text"] == "主旨：完整合成草稿"
            )
            assert all(
                key not in artifact
                for artifact in result["output"]["artifacts"]
                for key in ("download_url", "storage_ref", "file_refs")
            )
        artifact = db.query(Artifact).one()
        artifact_id = artifact.id
        private_version = db.query(ArtifactVersion).one()
        assert (
            private_version.storage_ref
            == f"/api/quick-builder/runs/{run_id}/artifacts/0"
        )
        db.commit()
        with pytest.raises(HTTPException) as unavailable:
            api.download_artifact(run_id, 0, user=owner, db=db)
        assert unavailable.value.status_code == 404
        db.commit()
        if outcome == "cancel":
            cancelled = await api.cancel_test_run(
                run_id,
                TestCancelInput(version_id=version.id, request_id=nonce),
                user=owner,
                db=db,
            )
            assert cancelled["status"] == "cancelled"
        elif outcome == "expire":
            row = db.get(QuickRun, run_id)
            row.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            db.commit()
        observed.extend([_event(frame) async for frame in iterator])
        result = api.read_run(run_id, user=owner, db=db)
        if outcome == "complete":
            assert result["status"] == "completed" and len(calls) == 1
            assert (
                result["artifacts"][0]["download_url"]
                == f"/api/quick-builder/runs/{run_id}/artifacts/0"
            )
            downloaded = api.download_artifact(run_id, 0, user=owner, db=db)
            assert downloaded.body == CONTENT
        else:
            assert result["status"] == (
                "cancelled" if outcome == "cancel" else "interrupted"
            )
            assert result["artifacts"] == [] and not calls
            with pytest.raises(HTTPException):
                api.download_artifact(run_id, 0, user=owner, db=db)
            db.expire_all()
            assert db.get(Artifact, artifact_id).status == "failed"
            stored_version = db.query(ArtifactVersion).one()
            assert stored_version.storage_ref is None and stored_version.file_refs == []
        assert all(
            key not in artifact
            for step in result["steps"]
            for state in (step.get("input"), step.get("output"))
            if isinstance(state, dict)
            for artifact in state.get("artifacts", [])
            for key in ("download_url", "storage_ref", "file_refs")
        )

    asyncio.run(scenario())
