# -*- coding: utf-8 -*-
"""完整回答在背景跑完，再用既有通知告訴送出的人。

模型只走對話上已經選定、而且這個人有權使用的登錄模型，經現有的
``proxy_request`` 送出。不另外接模型供應商。
"""
from __future__ import annotations

import logging
import secrets
from typing import Optional

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.conversation import Conversation
from app.models.handoff import Notification
from app.models.message import Message
from app.models.model_registry import ModelRegistry
from app.models.user import User
from app.services.answer_delivery import (
    ANSWER_FAILED,
    ANSWER_READY,
    BACKGROUND_FAILURE,
    BACKGROUND_PLACEHOLDER,
    FULL_ANSWER_SYSTEM,
)
from app.services.conversation_service import (
    STREAM_TERMINAL_STATES,
    load_active_path,
    start_turn,
    stream_envelope,
    update_message_content,
)
from app.services.handoff_service import notify_user

logger = logging.getLogger(__name__)

_NOTIFY_LOOKBACK = 100


def session_factory() -> Session:
    from app.database import SessionLocal

    return SessionLocal()


def _user_detail(exc: HTTPException) -> str:
    detail = exc.detail
    if isinstance(detail, str) and detail.strip():
        return detail.strip()
    return "這個對話的模型現在無法使用，無法在背景完成完整回答。"


def resolve_background_model(db: Session, user: User, conv: Conversation) -> ModelRegistry:
    """沿用這串對話已經選定的 Router 模型。"""
    from app.services.router_model_policy import (
        PLATFORM_ROUTER_NAME,
        RouterModelPolicyError,
        resolve_router_model,
    )

    try:
        selection = resolve_router_model(
            db,
            user,
            conversation_id=conv.id,
            conversation_model_id=conv.router_model_id,
            selection_version=int(conv.router_selection_version or 0),
        )
    except RouterModelPolicyError as exc:
        detail = exc.detail
        if isinstance(detail, dict):
            label = (detail.get("display_name") or "這個模型").strip() or "這個模型"
            reason = detail.get("reason")
            if reason == "not_granted":
                text = f"你目前沒有使用「{label}」的權限，無法在背景完成完整回答。"
            else:
                text = f"「{label}」現在無法使用，無法在背景完成完整回答。"
            status = exc.status_code if exc.status_code < 500 else 409
            raise HTTPException(status_code=status, detail=text) from exc
        raise HTTPException(
            status_code=exc.status_code if exc.status_code < 500 else 409,
            detail=_user_detail(
                HTTPException(status_code=exc.status_code, detail=detail)
            ),
        ) from exc

    model = db.get(ModelRegistry, selection.model_id)
    if model is None or not model.is_active or model.name == PLATFORM_ROUTER_NAME:
        raise HTTPException(
            status_code=409,
            detail="這個對話的模型現在無法使用，無法在背景完成完整回答。",
        )
    return model


def _clean_reference_ids(raw: object) -> list[str]:
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for item in raw[:20]:
        if not isinstance(item, str):
            continue
        ref = item.strip()
        if ref and len(ref) <= 80:
            out.append(ref)
    return out


def enqueue_background_answer(
    db: Session,
    conv_id: int,
    user: User,
    *,
    content: str,
    agent_name: Optional[str] = None,
    attachment_reference_ids: Optional[list[str]] = None,
    user_metadata: Optional[dict] = None,
) -> tuple[Message, Message, Optional[Message]]:
    """先把問題與預留回答落庫，呼叫端可以立刻離開。"""
    text = (content or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="請先寫下問題")
    from app.services.conversation_service import get_conversation

    conv = get_conversation(db, conv_id, user, for_write=True)
    model = resolve_background_model(db, user, conv)
    refs = _clean_reference_ids(attachment_reference_ids)
    extra = {
        "answer_delivery": "background",
    }
    if refs:
        extra["attachment_reference_ids"] = refs
    writer = secrets.token_urlsafe(24)
    agent = (agent_name or "").strip() or None
    if agent is not None:
        agent = agent[:100]
    return start_turn(
        db,
        conv_id,
        user,
        content=text,
        writer=writer,
        model_name=(model.name or "")[:100] or None,
        agent_name=agent,
        user_metadata=user_metadata,
        assistant_content=BACKGROUND_PLACEHOLDER,
        assistant_extra_metadata=extra,
    )


def history_for_completion(path: list[Message], *, skip_message_id: int) -> list[dict]:
    messages: list[dict] = [{"role": "system", "content": FULL_ANSWER_SYSTEM}]
    for msg in path:
        if msg.id == skip_message_id:
            continue
        if msg.role not in ("user", "assistant"):
            continue
        text = msg.content or ""
        if msg.role == "assistant" and (
            not text.strip() or text.strip() == BACKGROUND_PLACEHOLDER
        ):
            continue
        messages.append({"role": msg.role, "content": text})
    return messages


def assistant_text_from_completion(payload: object) -> str:
    if not isinstance(payload, dict):
        raise RuntimeError("empty")
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise RuntimeError("empty")
    message = choices[0].get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                parts.append(part["text"])
        content = "".join(parts)
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError("empty")
    return content.strip()


async def complete_chat(
    db: Session,
    user: User,
    model: ModelRegistry,
    messages: list[dict],
    *,
    conversation_id: int,
) -> str:
    """非串流完成。走現有 proxy，不另開模型客戶端。"""
    from app.middleware.caller import Caller
    from app.services.api_key_service import check_model_permission
    from app.services.proxy.ceiling import enforce_model_ceiling
    from app.services.proxy.headers import downstream_identity
    from app.services.proxy.service import proxy_request, resolve_proxy_tuning

    if not check_model_permission(db, user=user, api_key_id=None, model_id=model.id):
        raise HTTPException(status_code=403, detail="你目前沒有使用這個模型的權限")
    enforce_model_ceiling(
        db,
        model=model,
        caller=Caller(user=user, api_key_id=None),
        task_ctx=None,
        conv_id_int=conversation_id,
    )
    tuning = resolve_proxy_tuning(db)
    result = await proxy_request(
        model=model,
        api_key_id=None,
        user_id=user.id,
        department_id=user.department_id,
        request_body={
            "model": model.name,
            "messages": messages,
            "stream": False,
        },
        endpoint_path="/v1/chat/completions",
        user_identity=downstream_identity(user),
        conversation_id=str(conversation_id),
        legacy_runtime_call=True,
        tuning=tuning,
        record_usage=True,
    )
    return assistant_text_from_completion(result)


def _already_notified(db: Session, user_id: int, message_id: int, kind: str) -> bool:
    rows = (
        db.query(Notification)
        .filter(Notification.user_id == user_id, Notification.type == kind)
        .order_by(Notification.id.desc())
        .limit(_NOTIFY_LOOKBACK)
        .all()
    )
    for row in rows:
        payload = row.payload if isinstance(row.payload, dict) else {}
        if payload.get("message_id") == message_id:
            return True
    return False


def _notify(
    db: Session,
    *,
    user_id: int,
    message_id: int,
    conversation_id: int,
    title: str,
    kind: str,
    conv_title: str,
) -> None:
    if _already_notified(db, user_id, message_id, kind):
        return
    label = (conv_title or "對話").strip() or "對話"
    if len(label) > 80:
        label = label[:80] + "…"
    if kind == ANSWER_READY:
        body = f"「{label}」的完整回答已經好了。"
    else:
        body = f"「{label}」的完整回答沒有完成。請回到對話再試一次。"
    notify_user(
        db,
        user_id=user_id,
        type=kind,
        title=title,
        body=body,
        payload={
            "conversation_id": conversation_id,
            "message_id": message_id,
            "answer_delivery": "background",
        },
    )
    db.commit()


def _load_job(db: Session, message_id: int) -> tuple[Message, Conversation, User] | None:
    msg = db.get(Message, message_id)
    if msg is None or msg.role != "assistant":
        return None
    conv = db.get(Conversation, msg.conversation_id)
    if conv is None or conv.user_id is None:
        return None
    user = db.get(User, conv.user_id)
    if user is None:
        return None
    return msg, conv, user


def _finish(
    db: Session,
    msg: Message,
    user: User,
    *,
    content: str,
    state: str,
    kind: str,
    title: str,
    conv_title: str,
) -> None:
    envelope = stream_envelope(msg.metadata_) or {}
    writer = envelope.get("writer")
    current = envelope.get("state")
    if current not in STREAM_TERMINAL_STATES:
        meta = dict(msg.metadata_ or {})
        meta["anila_stream"] = {"state": state}
        meta["answer_delivery"] = "background"
        meta.pop("attachment_reference_ids", None)
        update_message_content(
            db,
            msg.conversation_id,
            msg.id,
            user,
            content=content,
            stream_writer=writer if isinstance(writer, str) else None,
            metadata=meta,
        )
    _notify(
        db,
        user_id=user.id,
        message_id=msg.id,
        conversation_id=msg.conversation_id,
        title=title,
        kind=kind,
        conv_title=conv_title,
    )


async def _inject_attachments(
    db: Session,
    user: User,
    conv_id: int,
    model_name: str,
    messages: list[dict],
    reference_ids: list[str],
) -> list[dict]:
    if not reference_ids:
        return messages
    from app.api.proxy import _inject_attachments_async

    body = {"model": model_name, "messages": list(messages)}
    try:
        await _inject_attachments_async(
            db,
            conv_id,
            body,
            model_name,
            explicit_reference_ids=set(reference_ids),
            user=user,
        )
    except Exception:
        logger.exception(
            "background answer attachment inject failed conv_id=%s", conv_id
        )
        db.rollback()
        return messages
    merged = body.get("messages")
    return merged if isinstance(merged, list) else messages


async def run_background_answer(db: Session, message_id: int) -> None:
    loaded = _load_job(db, message_id)
    if loaded is None:
        logger.info("background answer missing message_id=%s", message_id)
        return
    msg, conv, user = loaded
    envelope = stream_envelope(msg.metadata_) or {}
    if envelope.get("state") in STREAM_TERMINAL_STATES:
        kind = ANSWER_FAILED if envelope.get("state") == "failed" else ANSWER_READY
        title = "完整回答沒有完成" if kind == ANSWER_FAILED else "完整回答已完成"
        _notify(
            db,
            user_id=user.id,
            message_id=msg.id,
            conversation_id=conv.id,
            title=title,
            kind=kind,
            conv_title=conv.title or "",
        )
        return

    meta = msg.metadata_ if isinstance(msg.metadata_, dict) else {}
    refs = _clean_reference_ids(meta.get("attachment_reference_ids"))
    model = (
        db.query(ModelRegistry)
        .filter(ModelRegistry.name == msg.model_name)
        .first()
        if msg.model_name
        else None
    )
    try:
        if model is None or not model.is_active:
            raise HTTPException(status_code=409, detail="模型現在無法使用")
        path = load_active_path(db, conv)
        messages = history_for_completion(path, skip_message_id=msg.id)
        messages = await _inject_attachments(
            db, user, conv.id, model.name, messages, refs
        )
        loaded_again = _load_job(db, message_id)
        if loaded_again is None:
            return
        msg, conv, user = loaded_again
        text = await complete_chat(
            db,
            user,
            model,
            messages,
            conversation_id=conv.id,
        )
    except Exception:
        logger.exception("background answer failed message_id=%s", message_id)
        failed = _load_job(db, message_id)
        if failed is None:
            return
        msg, conv, user = failed
        db.rollback()
        msg = db.get(Message, message_id) or msg
        _finish(
            db,
            msg,
            user,
            content=BACKGROUND_FAILURE,
            state="failed",
            kind=ANSWER_FAILED,
            title="完整回答沒有完成",
            conv_title=conv.title or "",
        )
        return

    _finish(
        db,
        msg,
        user,
        content=text,
        state="complete",
        kind=ANSWER_READY,
        title="完整回答已完成",
        conv_title=conv.title or "",
    )


async def run_detached(message_id: int) -> None:
    db = session_factory()
    try:
        await run_background_answer(db, message_id)
    except Exception:
        logger.exception("background answer crashed message_id=%s", message_id)
        try:
            db.rollback()
        except Exception:
            logger.exception("background answer rollback failed message_id=%s", message_id)
    finally:
        db.close()
