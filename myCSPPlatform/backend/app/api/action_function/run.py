"""Run endpoint — SSE relay from worker to browser.

Two authz paths (spec §4.5):

* test_mode=true → ``authorize_test_console_run`` (author or admin
  only; function status irrelevant; conversation_id may be None)
* test_mode=false → ``authorize_chat_message_run`` (full owner check
  via ``conversation_service.get_conversation``; message must belong
  to the conversation and be assistant role)

The CSP side wraps each worker SSE chunk in audit redaction before
forwarding to the browser. Sprint 1 ships the relay with a stub
worker_client that won't actually connect; Sprint 2 wires real httpx.
"""

from __future__ import annotations

import json
import logging
import traceback
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session


logger = logging.getLogger("anila.functions.run")

from app.api.auth import get_current_user
from app.database import get_db
from app.models import (
    ActionFunctionRun,
    ActionFunctionRunContext,
    ActionFunctionRunStatus,
    User,
)
from app.schemas.action_function import RunRequest
from app.services.action_function import crud as fn_crud
from app.services.action_function.ownership import (
    AuthzError,
    authorize_chat_message_run,
    authorize_test_console_run,
)
from app.services.action_function.worker_client import WorkerClient


router = APIRouter(prefix="/api/functions", tags=["action-functions"])


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@router.post("/{slug}/run")
async def run_function(
    slug: str,
    payload: RunRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    fn = fn_crud.get_function_by_slug(db, slug)
    if fn is None:
        raise HTTPException(status_code=404)
    try:
        if payload.test_mode:
            authorize_test_console_run(db, caller=user, fn=fn)
        else:
            authorize_chat_message_run(
                db,
                caller=user,
                fn=fn,
                conv_id=payload.context.conversation_id,
                msg_id=payload.context.message_id,
            )
    except AuthzError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    latest = fn_crud.get_latest_version(db, fn.id)
    if latest is None:
        raise HTTPException(status_code=400, detail="function has no version")

    run = ActionFunctionRun(
        function_id=fn.id,
        version_no=latest.version_no,
        action_id=payload.action_id,
        triggered_by_user_id=user.id,
        context_type=(
            ActionFunctionRunContext.TEST_CONSOLE
            if payload.test_mode
            else ActionFunctionRunContext.CHAT_MESSAGE
        ),
        conversation_id=payload.context.conversation_id,
        message_id=payload.context.message_id,
        request_payload_json=payload.model_dump(),
        status=ActionFunctionRunStatus.RUNNING,
    )
    db.add(run)
    db.commit()
    db.refresh(run)

    client = WorkerClient()

    async def stream():
        # Sprint 2 will wrap each worker chunk with redact_events. Sprint
        # 1 just relays and finalizes the run row on completion / error.
        worker_payload = {
            "code": latest.code,
            "body": {
                **payload.context.model_dump(),
                "action_id": payload.action_id,
            },
            "valves": {},  # Sprint 2 wires decrypted valves
            "user": {
                "id": user.id,
                "username": user.username,
                "email": getattr(user, "email", None),
                "role": user.role,
            },
            "metadata": {"started_at": _utcnow().isoformat()},
        }
        final_status = ActionFunctionRunStatus.ERROR
        final_error = None
        try:
            logger.info("[run %s] fwd to worker, slug=%s", run.id, slug)
            async for chunk in client.stream_run(worker_payload):
                yield chunk
            final_status = ActionFunctionRunStatus.SUCCESS
            logger.info("[run %s] stream complete", run.id)
        except Exception as exc:
            tb = traceback.format_exc()
            logger.exception("[run %s] stream_run failed", run.id)
            final_error = f"{type(exc).__name__}: {exc}"
            err_event = {"type": "error", "message": final_error}
            try:
                yield (
                    f"event: function_event\n"
                    f"data: {json.dumps(err_event)}\n\n"
                ).encode()
            except Exception:
                logger.exception("[run %s] failed yielding error event", run.id)
        finally:
            # DB writes wrapped so a closed session / commit failure can't
            # tear the stream down in a way nginx HTTP/2 can't represent.
            try:
                run.status = final_status
                if final_error:
                    run.error_message = final_error
                run.ended_at = _utcnow()
                if run.started_at is not None:
                    started = run.started_at
                    if started.tzinfo is None:
                        started = started.replace(tzinfo=timezone.utc)
                    run.duration_ms = int(
                        (run.ended_at - started).total_seconds() * 1000
                    )
                db.commit()
                logger.info(
                    "[run %s] finalized status=%s duration_ms=%s",
                    run.id, final_status.value, run.duration_ms,
                )
            except Exception:
                logger.exception("[run %s] finalize failed", run.id)
            done = {
                "run_id": run.id,
                "status": (
                    final_status.value if hasattr(final_status, "value")
                    else str(final_status)
                ),
            }
            try:
                yield (
                    f"event: function_done\n"
                    f"data: {json.dumps(done)}\n\n"
                ).encode()
            except Exception:
                logger.exception("[run %s] failed yielding done sentinel", run.id)

    return StreamingResponse(stream(), media_type="text/event-stream")
