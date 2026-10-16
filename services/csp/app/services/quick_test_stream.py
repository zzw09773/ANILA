"""Bounded Builder observers; disconnecting an observer never cancels its run.

Execution authority lives in the database lease. The process registry provides
bounded admission and local cancellation only, without replay or reassignment.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from copy import deepcopy
import json
from uuid import uuid4

from fastapi import HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session, defer

from app.models.quick_builder import QuickRun, QuickVersion
from app.models.user import User
from app.services.quick_builder import fail
from app.services.quick_stream_mask import OutputMasker, OutputMaskError
from app.services.quick_test_lifecycle import check_in_session

MAX_ACTIVE_RUNS = 32
MAX_ACTOR_RUNS = 2
QUEUE_SIZE = 8
ACK_SECONDS = 5
HEARTBEAT_SECONDS = 15
MAX_EVENT_BYTES = 1024 * 1024
_RESERVATIONS: dict[str, int] = {}
_RUNS: dict[int, "_Execution"] = {}


@dataclass
class _Observer:
    queue: asyncio.Queue = field(
        default_factory=lambda: asyncio.Queue(maxsize=QUEUE_SIZE)
    )
    closed: bool = False
    slow: bool = False
    waiting: set = field(default_factory=set)
    detached: asyncio.Event = field(default_factory=asyncio.Event)

    def detach(self, *, slow=False):
        self.closed = True
        self.detached.set()
        self.slow = self.slow or slow
        for acknowledgement in list(self.waiting):
            if not acknowledgement.done():
                acknowledgement.set_result(None)
        while not self.queue.empty():
            _, _, acknowledgement = self.queue.get_nowait()
            if not acknowledgement.done():
                acknowledgement.set_result(None)

    async def send(self, event, payload):
        if self.closed:
            return
        acknowledgement = asyncio.get_running_loop().create_future()
        self.waiting.add(acknowledgement)
        try:
            await asyncio.wait_for(
                self.queue.put((event, deepcopy(payload), acknowledgement)), ACK_SECONDS
            )
            await asyncio.wait_for(asyncio.shield(acknowledgement), ACK_SECONDS)
        except asyncio.TimeoutError:
            self.detach(slow=True)
        finally:
            self.waiting.discard(acknowledgement)


@dataclass
class _Execution:
    bind: object
    actor_id: int
    token_version: int
    department_id: int | None
    assistant_id: int
    version_id: int
    revision: int
    request_id: str
    run_id: int
    lease: object = field(default=None, repr=False)
    observer: _Observer = field(default_factory=_Observer)
    task: object = None
    sequence: int = 0
    current_node: str | None = None


def _envelope(context, payload):
    return {
        "schema_version": 1,
        "run_id": context.run_id,
        "version_id": context.version_id,
        "revision": context.revision,
        "request_id": context.request_id,
        "seq": context.sequence + 1,
        "payload": payload,
    }


def _frame(context, event, payload):
    def encode(value):
        return (
            f"event: quick.{event}\nid: {context.run_id}:{context.sequence + 1}\ndata: "
            + json.dumps(
                _envelope(context, value),
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            )
            + "\n\n"
        )

    frame = encode(payload)
    if len(frame.encode("utf-8")) > MAX_EVENT_BYTES:
        if event == "step_finished":
            trace = payload["step"]
            compact = {
                key: trace[key]
                for key in (
                    "node_id",
                    "kind",
                    "status",
                    "elapsed_ms",
                    "error",
                    "branch",
                    "completion",
                )
                if key in trace
            }
            payload = {"step": compact, "result_available": True}
        elif event in {"run_finished", "run_snapshot"}:
            result = payload["result"]
            payload = {"status": result["status"], "result_available": True}
            if "completion" in result:
                payload["completion"] = result["completion"]
        else:
            raise ValueError("TEST_STREAM_EVENT_LIMIT")
        frame = encode(payload)
        if len(frame.encode("utf-8")) > MAX_EVENT_BYTES:
            raise ValueError("TEST_STREAM_EVENT_LIMIT")
    context.sequence += 1
    return frame


def _read(context, *, live=False, expected_status=None, project=False):
    from app.services.quick_execution import (
        _lease_phase,
        authorize_test_read,
        recover_test_runs,
        stored_public_result,
        test_tracking,
    )

    with Session(bind=context.bind, expire_on_commit=False) as db:
        actor = db.get(User, context.actor_id, populate_existing=True)
        row = (
            db.query(QuickRun)
            .filter_by(id=context.run_id)
            .options(*([] if project else [defer(QuickRun.result)]))
            .populate_existing()
            .first()
        )
        if (
            not actor
            or not actor.is_active
            or not actor.is_approved
            or actor.token_version != context.token_version
            or actor.department_id != context.department_id
            or not row
            or row.user_id != context.actor_id
            or not row.is_test
            or row.version_id != context.version_id
            or row.test_request_id != context.request_id
        ):
            fail(404, "NOT_FOUND", "試跑事件的存取權限已變更")
        _, version = authorize_test_read(
            db,
            actor,
            context.assistant_id,
            context.version_id,
            tracking=test_tracking(row),
        )
        if live:
            if context.lease is None or not _lease_phase(
                db, lambda: check_in_session(db, context.lease)
            ):
                fail(
                    409, "TEST_RUN_INACTIVE", "這筆試跑的執行權已失效，請查詢原執行紀錄"
                )
        elif project:
            recover_test_runs(db, actor, context.assistant_id, run_id=row.id)
            db.refresh(row)
        if expected_status is not None and row.status != expected_status:
            fail(409, "TEST_RUN_CHANGED", "試跑狀態已變更，請查詢原執行紀錄")
        result = stored_public_result(row, version) if project else None
        db.commit()
        return result


def _subscription_error(*, slow=False):
    return {
        "scope": "subscription",
        "error": {
            "type": "TEST_STREAM_SLOW" if slow else "TEST_STREAM_INTERRUPTED",
            "message": (
                "事件接收等待過久，已停止即時顯示；原試跑仍會保存，請查詢執行結果。"
                if slow
                else "試跑事件無法繼續顯示，請確認權限並查詢原執行結果。"
            ),
        },
    }


async def _consume(context):
    observer = context.observer
    try:
        while True:
            if observer.closed:
                try:
                    _read(context)
                except Exception:
                    pass
                yield _frame(
                    context, "typed_error", _subscription_error(slow=observer.slow)
                )
                return
            item = None
            pending = asyncio.create_task(observer.queue.get())
            detached = asyncio.create_task(observer.detached.wait())
            try:
                await asyncio.wait(
                    (pending, context.task, detached),
                    timeout=HEARTBEAT_SECONDS,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if pending.done():
                    item = pending.result()
                elif context.task.done() and not observer.queue.empty():
                    item = observer.queue.get_nowait()
            finally:
                if not pending.done():
                    pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
                if not detached.done():
                    detached.cancel()
                await asyncio.gather(detached, return_exceptions=True)
                if not pending.cancelled():
                    item = pending.result()
            # A renewal failure may detach us in the same event-loop turn
            # that queue.get dequeues a frame. That item is no longer in the
            # queue drained by detach(), so acknowledge and discard it here.
            if observer.closed:
                if item is not None and not item[2].done():
                    item[2].set_result(None)
                yield _frame(
                    context, "typed_error", _subscription_error(slow=observer.slow)
                )
                return
            if item is None:
                if observer.closed or context.task.done():
                    try:
                        _read(context)
                    except Exception:
                        pass
                    yield _frame(
                        context, "typed_error", _subscription_error(slow=observer.slow)
                    )
                    return
                try:
                    _read(context, live=True)
                except Exception:
                    observer.detach()
                    yield _frame(context, "typed_error", _subscription_error())
                    return
                if observer.closed:
                    yield _frame(
                        context, "typed_error", _subscription_error(slow=observer.slow)
                    )
                    return
                yield ": keep-alive\n\n"
                continue
            event, payload, acknowledgement = item
            try:
                terminal = event in {"run_finished", "typed_error"}
                expected = (
                    payload.get("result", {}).get("status")
                    if event == "run_finished"
                    else None
                )
                _read(context, live=not terminal, expected_status=expected)
                if observer.closed:
                    yield _frame(
                        context, "typed_error", _subscription_error(slow=observer.slow)
                    )
                    return
                yield _frame(context, event, payload)
            except (GeneratorExit, asyncio.CancelledError):
                raise
            except Exception:
                observer.detach()
                yield _frame(context, "typed_error", _subscription_error())
                return
            finally:
                if not acknowledgement.done():
                    acknowledgement.set_result(None)
            if event == "run_finished" or (
                event == "typed_error" and payload.get("scope") == "subscription"
            ):
                return
    finally:
        # This removes only the observer. Execution has independent authority.
        observer.detach()


def _response(iterator):
    return StreamingResponse(
        iterator,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


def _reserve(actor_id):
    if (
        len(_RESERVATIONS) >= MAX_ACTIVE_RUNS
        or sum(owner == actor_id for owner in _RESERVATIONS.values()) >= MAX_ACTOR_RUNS
    ):
        fail(
            429,
            "TEST_RUN_BUSY",
            "目前試跑已達同時執行上限，請稍後再試；系統未建立新執行。",
        )
    reservation = uuid4().hex
    _RESERVATIONS[reservation] = actor_id
    return reservation


def cancel_local(run_id):
    context = _RUNS.get(run_id)
    if context is not None and context.task is not None and not context.task.done():
        context.task.cancel()


async def start_test_stream(
    db, user, assistant_id, version_id, message, request_id=None
):
    from app.services.quick_execution import _start_test

    actor_id, token_version, department_id = (
        user.id,
        user.token_version,
        user.department_id,
    )
    nonce = str(request_id) if request_id is not None else str(uuid4())
    reservation = None

    def admit():
        nonlocal reservation
        reservation = _reserve(actor_id)

    try:
        started, previous = _start_test(
            db, user, assistant_id, version_id, message, nonce, admit=admit
        )
    except BaseException:
        if reservation is not None:
            _RESERVATIONS.pop(reservation, None)
        raise
    if previous is not None:
        context = _Execution(
            db.get_bind(),
            actor_id,
            token_version,
            department_id,
            assistant_id,
            previous["version_id"],
            previous["revision"],
            previous["request_id"],
            previous["id"],
        )

        async def snapshot():
            try:
                result = _read(context, project=True)
                yield _frame(context, "run_snapshot", {"result": result})
            except Exception:
                yield _frame(context, "typed_error", _subscription_error())

        return _response(snapshot())
    assistant, version, _, _, row, _, _, _, lease = started
    context = _Execution(
        db.get_bind(),
        actor_id,
        token_version,
        department_id,
        assistant.id,
        version.id,
        version.revision,
        row.test_request_id,
        row.id,
        lease=lease,
    )

    async def produce():
        from app.services.quick_execution import _finish, run, stored_public_result
        from app.services.quick_flow import QuickFlowExecutionError

        masker = OutputMasker()
        saw_delta = False
        execution_db = Session(bind=context.bind, expire_on_commit=False)

        async def delta(text, *, before_send):
            nonlocal saw_delta
            before_send()
            if not text:
                return
            saw_delta = True
            try:
                safe = masker.feed(text)
            except OutputMaskError as exc:
                raise QuickFlowExecutionError(exc.code, str(exc)) from None
            if safe:
                await context.observer.send(
                    "output_delta", {"node_id": context.current_node, "text": safe}
                )

        async def step(event, trace, *, before_send):
            before_send()
            if event == "step_started":
                context.current_node = trace["node_id"]
                payload = {
                    "step": {
                        key: trace[key]
                        for key in ("node_id", "kind", "status", "elapsed_ms")
                    }
                }
            else:
                if (
                    saw_delta
                    and trace["node_id"] == context.current_node
                    and trace["error"] is None
                ):
                    try:
                        tail = masker.finish()
                    except OutputMaskError as exc:
                        raise QuickFlowExecutionError(exc.code, str(exc)) from None
                    if tail:
                        await context.observer.send(
                            "output_delta", {"node_id": trace["node_id"], "text": tail}
                        )
                payload = {"step": trace}
            await context.observer.send(event, payload)

        try:
            await context.observer.send("run_started", {"status": "running"})
            actor = execution_db.get(User, actor_id, populate_existing=True)
            if (
                not actor
                or actor.token_version != token_version
                or actor.department_id != department_id
            ):
                fail(404, "NOT_FOUND", "試跑的登入身分已變更")
            result = await run(
                execution_db,
                actor,
                assistant_id,
                version_id,
                message,
                testing=True,
                test_request_id=context.request_id,
                test_lease=lease,
                on_delta=delta,
                on_step=step,
                on_lease_lost=context.observer.detach,
            )
        except BaseException as exc:
            execution_db.rollback()
            stored = execution_db.get(QuickRun, context.run_id, populate_existing=True)
            unknown_authority = (
                isinstance(exc, HTTPException)
                and isinstance(exc.detail, dict)
                and exc.detail.get("type") == "TEST_RUN_INACTIVE"
            )
            if (
                stored is not None
                and stored.status == "running"
                and not unknown_authority
            ):
                status = (
                    "cancelled" if isinstance(exc, asyncio.CancelledError) else "failed"
                )
                try:
                    _finish(
                        execution_db,
                        execution_db.get(User, actor_id),
                        assistant_id,
                        context.run_id,
                        {
                            "status": status,
                            "output": "",
                            "steps": stored.result.get("steps", []),
                            "answer_complete": False,
                            "usage_complete": False,
                            "error": {
                                "type": "CANCELLED"
                                if status == "cancelled"
                                else "TEST_EXECUTION_FAILED",
                                "message": "執行已停止，結果尚未完成。"
                                if status == "cancelled"
                                else "試跑未能完成，請查詢原執行紀錄。",
                            },
                        },
                        [],
                        stored.result.get("execution_context", {}),
                        lease=lease,
                    )
                except Exception:
                    execution_db.rollback()
            stored = execution_db.get(QuickRun, context.run_id, populate_existing=True)
            if stored is None or stored.status == "running":
                await context.observer.send("typed_error", _subscription_error())
                return
            result = stored_public_result(
                stored, execution_db.get(QuickVersion, stored.version_id)
            )
        finally:
            execution_db.close()
        if result.get("error"):
            await context.observer.send(
                "typed_error", {"scope": "run", "error": result["error"]}
            )
        await context.observer.send("run_finished", {"result": result})

    context.task = asyncio.create_task(produce())
    _RUNS[context.run_id] = context

    def finished(task):
        _RESERVATIONS.pop(reservation, None)
        if _RUNS.get(context.run_id) is context:
            _RUNS.pop(context.run_id, None)
        if not task.cancelled():
            task.exception()  # Retrieve failures without logging provider/private values.

    context.task.add_done_callback(finished)
    return _response(_consume(context))
