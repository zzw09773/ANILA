"""OpenAI SSE delivery with bounded backpressure and a stream-owned DB session."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid

from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.models.user import User
from app.services.quick_stream_mask import OutputMasker, OutputMaskError

logger = logging.getLogger(__name__)


def _content(result):
    text = result["output"]
    if result.get("artifacts"):
        text += "\n\n" + "\n".join(
            f"[{item['filename']}]({item['download_url']})"
            for item in result["artifacts"]
        )
    return text


def flow_stream_response(
    *,
    bind,
    user_id,
    token_version,
    department_id,
    assistant_id,
    model_name,
    execute,
    finalize,
):
    completion_id, created = "chatcmpl-quick-" + uuid.uuid4().hex, int(time.time())

    def frame(content=None, *, finish=False, finish_reason="stop", usage=None):
        return (
            "data: "
            + json.dumps(
                {
                    "id": completion_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model_name,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {} if finish else {"content": content},
                            "finish_reason": finish_reason if finish else None,
                        }
                    ],
                    **({"usage": usage} if finish and usage is not None else {}),
                },
                ensure_ascii=False,
            )
            + "\n\n"
        )

    async def stream():
        queue = asyncio.Queue(maxsize=8)
        raw_parts = []
        masker = OutputMasker()
        delivery_guard = None

        async def deliver(text):
            if not text:
                return
            acknowledgement = asyncio.get_running_loop().create_future()
            await queue.put(("delta", text, delivery_guard, acknowledgement))
            await acknowledgement

        async def on_delta(text, *, before_send):
            nonlocal delivery_guard
            delivery_guard = before_send
            if text:
                raw_parts.append(text)
                try:
                    safe = masker.feed(text)
                except OutputMaskError as exc:
                    from app.services.quick_flow import QuickFlowExecutionError

                    raise QuickFlowExecutionError(exc.code, str(exc)) from None
                await deliver(safe)

        async def produce():
            db = Session(bind=bind, expire_on_commit=False)
            result = None
            user = None

            def invalidate(status):
                if result is None or result.get("status") != "completed":
                    return
                from app.models.quick_builder import QuickRun
                from app.services.quick_execution import _finish

                stored = db.get(QuickRun, result["id"])
                if stored is not None:
                    _finish(
                        db,
                        user,
                        assistant_id,
                        stored.id,
                        {
                            "output": "",
                            "steps": [],
                            "status": status,
                            "error": {
                                "type": "STREAM_INTERRUPTED",
                                "message": "回答傳送已中止，請重新提問（STREAM_INTERRUPTED）",
                            },
                        },
                        [],
                        stored.result.get("execution_context", {}),
                    )

            def recover(status):
                failed = False
                for operation in (
                    lambda: invalidate(status),
                    lambda: finalize("failed"),
                ):
                    try:
                        operation()
                    except Exception:
                        failed = True
                        try:
                            db.rollback()
                        except Exception:
                            pass
                if failed:
                    logger.error("快製串流終局紀錄無法保存 assistant_id=%s", assistant_id)
                return failed

            try:
                user = db.query(User).filter_by(id=user_id).populate_existing().first()
                if (
                    not user
                    or not user.is_active
                    or not user.is_approved
                    or user.token_version != token_version
                    or user.department_id != department_id
                ):
                    raise ValueError("identity_changed")
                result = await execute(db, user, on_delta)
                from app.services.quick_execution import completion_envelope

                terminal_meta = (
                    completion_envelope(result["completion"])
                    if result.get("completion") else None
                )
                if result.get("status") != "completed":
                    finalize("failed")
                    detail = dict(result.get("error") or {})
                    if terminal_meta is not None:
                        detail["completion"] = terminal_meta["completion"]
                    await queue.put(("error", detail, None, None))
                    return
                text = _content(result)
                if raw_parts:
                    from app.services.quick_execution import mask

                    await deliver(masker.finish())
                    terminal = mask({"output": "".join(raw_parts)})["output"]
                    if not text.startswith(terminal):
                        raise ValueError("output_changed_after_stream")
                    await deliver(text[len(terminal) :])
                else:
                    # Structured/validated outputs and non-streaming models
                    # become visible only once their final value is known.
                    from app.services.quick_execution import mask

                    text = mask(text)
                    for index in range(0, len(text), 64):
                        await deliver(text[index : index + 64])
                finalize("completed")
                await queue.put(("finish", terminal_meta, None, None))
            except asyncio.CancelledError:
                recover("cancelled")
                raise
            except GeneratorExit:
                recover("cancelled")
                await queue.put(("error", None, None, None))
            except Exception as exc:
                persistence_failed = recover("failed")
                detail = (
                    {"type": exc.code, "message": str(exc)}
                    if isinstance(exc, OutputMaskError)
                    else None
                )
                if persistence_failed:
                    detail = {
                        "type": "STREAM_PERSIST_FAILED",
                        "message": "回答已中止，但終局紀錄保存失敗，請通知管理員（STREAM_PERSIST_FAILED）",
                    }
                await queue.put(("error", detail, None, None))
            finally:
                db.close()

        producer = asyncio.create_task(produce())
        try:
            while True:
                pending = asyncio.create_task(queue.get())
                item = None
                try:
                    await asyncio.wait(
                        (pending, producer),
                        timeout=15,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if pending.done():
                        item = pending.result()
                    elif producer.done():
                        pending.cancel()
                        await asyncio.gather(pending, return_exceptions=True)
                        item = (
                            queue.get_nowait()
                            if not queue.empty()
                            else ("error", None, None, None)
                        )
                finally:
                    if not pending.done():
                        pending.cancel()
                    await asyncio.gather(pending, return_exceptions=True)
                    if not pending.cancelled():
                        item = pending.result()
                if item is None:
                    # No queue getter may remain live while the client pauses
                    # on a heartbeat; it could consume an unacked delta.
                    yield ": keep-alive\n\n"
                    continue
                kind, text, guard, acknowledgement = item
                if kind == "error":
                    from app.services.proxy.service import format_anila_stream_error
                    from app.services.quick_execution import mask

                    code = "FLOW_STREAM_FAILED"
                    message = "助手回答未能完成，請重新提問或通知管理員（FLOW_STREAM_FAILED）"
                    if isinstance(text, dict) and text.get("completion") is not None:
                        from app.services.quick_execution import completion_envelope

                        yield "event: anila.meta\ndata: " + json.dumps(
                            completion_envelope(text["completion"]),
                            ensure_ascii=False, separators=(",", ":"),
                        ) + "\n\n"
                    if (
                        isinstance(text, dict)
                        and isinstance(text.get("type"), str)
                        and re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", text["type"])
                        and isinstance(text.get("message"), str)
                    ):
                        code, message = text["type"], mask(text["message"])[:500]
                    yield format_anila_stream_error(message, code=code)
                    yield "data: [DONE]\n\n"
                    break
                if kind == "finish":
                    if text is not None:
                        yield "event: anila.meta\ndata: " + json.dumps(
                            text, ensure_ascii=False, separators=(",", ":")
                        ) + "\n\n"
                    yield frame(
                        finish=True,
                        finish_reason=(text["finish_reason"] or "stop")
                        if text is not None else "stop",
                        usage=text["usage"] if text is not None else None,
                    )
                    yield "data: [DONE]\n\n"
                    break
                try:
                    if guard is None:
                        raise ValueError("missing_delivery_authorization")
                    guard()
                    yield frame(text)
                except Exception as exc:
                    if not acknowledgement.done():
                        acknowledgement.set_exception(exc)
                    continue
                finally:
                    if not acknowledgement.done():
                        acknowledgement.set_result(None)
        finally:
            if not producer.done():
                producer.cancel()
            await asyncio.gather(producer, return_exceptions=True)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-store",
            "X-Accel-Buffering": "no",
        },
    )
