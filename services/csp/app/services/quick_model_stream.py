"""終端模型節點的純內文串流，沿用平台代理與用量結算。"""
from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.services.internal_llm import InternalCompletionError, _quota_fields, _snapshot
from app.services.proxy.sse import _parse_sse_block
from app.services.quick_flow import MAX_TEXT_CHARS
from app.services.reasoning_gate import ContentRedactor, ReasoningTagLimitError

MAX_FRAME_CHARS = 256 * 1024


async def stream_chat(
    db: Session,
    model,
    body: dict,
    *,
    user_id: int,
    department_id: int | None = None,
    caller_agent_id: int | None = None,
    api_key_id: int | None = None,
    on_delta: Callable[[str], Awaitable[None]],
    before_chunk: Callable[[], object],
) -> str:
    """僅供已確認直接通往輸出的模型；回呼須同步重驗當下執行授權。"""
    from app.services.proxy.headers import resolve_model_gateway_key
    from app.services.proxy.service import STREAM_FINISH_REASONS, proxy_stream, resolve_proxy_tuning
    from app.services.proxy.urls import join_upstream_path
    from app.services.quota_service import enforce_call_quota

    if type(user_id) is not int or user_id <= 0 or not callable(before_chunk):
        raise InternalCompletionError(403)
    before_chunk()
    snapshot = _snapshot(model)
    if (
        not snapshot.is_active
        or snapshot.model_type != "llm"
        or snapshot.name == "anila-router"
    ):
        raise InternalCompletionError(403)
    tuning = resolve_proxy_tuning(db)
    started = datetime.now(timezone.utc)
    try:
        enforce_call_quota(
            db,
            user_id=user_id,
            department_id=department_id,
            api_key_id=api_key_id,
            usage_kind="inference",
            now=started,
        )
        gateway_key = resolve_model_gateway_key(snapshot)
    except HTTPException as exc:
        db.commit()
        code, message = _quota_fields(exc)
        raise InternalCompletionError(
            exc.status_code, code=code, message=message
        ) from exc
    db.commit()
    payload = {**body, "model": snapshot.name, "stream": True, "n": 1}
    upstream = proxy_stream(
        target_url=join_upstream_path(
            snapshot.endpoint_url, f"/{snapshot.api_version}/chat/completions"
        ),
        api_key_id=api_key_id,
        user_id=user_id,
        department_id=department_id,
        usage_model_id=snapshot.id,
        request_body=payload,
        model_name=snapshot.name,
        caller_agent_id=caller_agent_id,
        gateway_api_key=gateway_key,
        model_name_snapshot=snapshot.name,
        tuning=tuning,
        model=snapshot,
        request_type="chat",
        usage_kind="inference",
        record_usage=True,
        request_timestamp=started,
        require_stream_terminal=True,
    )
    redactor = ContentRedactor(reveal=False)
    parts: list[str] = []
    content_chars = 0
    done = False
    finish_seen = False
    buf = ""

    async def emit(text: str):
        if text:
            before_chunk()
            await on_delta(text)
            parts.append(text)

    async def process(block: str):
        nonlocal content_chars, done, finish_seen
        event, data = _parse_sse_block(block)
        if event in ("anila.error", "error"):
            raise InternalCompletionError(502)
        if not data or event not in (None, "message"):
            return
        if data == "[DONE]":
            done = True
            return
        if done:
            return
        try:
            item = json.loads(data)
        except (ValueError, RecursionError) as exc:
            raise InternalCompletionError(502) from exc
        if not isinstance(item, dict) or "error" in item:
            raise InternalCompletionError(502)
        choices = item.get("choices", [])
        if not choices:
            return
        if not isinstance(choices, list) or not isinstance(choices[0], dict):
            raise InternalCompletionError(502)
        choice = choices[0]
        if choice.get("index", 0) != 0:
            raise InternalCompletionError(502)
        reason = choice.get("finish_reason")
        if reason is not None:
            if not isinstance(reason, str) or reason not in STREAM_FINISH_REASONS:
                raise InternalCompletionError(502)
            finish_seen = True
        delta = choice.get("delta", {})
        if not isinstance(delta, dict):
            raise InternalCompletionError(502)
        text = delta.get("content")
        if text is None:
            return
        if not isinstance(text, str):
            raise InternalCompletionError(502)
        content_chars += len(text)
        if content_chars > MAX_TEXT_CHARS:
            raise InternalCompletionError(502)
        try:
            visible, _ = redactor.push(text)
        except ReasoningTagLimitError as exc:
            raise InternalCompletionError(502) from exc
        await emit(visible)

    failed = False
    try:
        async for chunk in upstream:
            if not isinstance(chunk, str):
                raise InternalCompletionError(502)
            buf += chunk
            # 保留尾端 CR，讓跨片段的 CRLF 只正規化一次。
            buf = buf.replace("\r\n", "\n")
            while "\n\n" in buf:
                block, buf = buf.split("\n\n", 1)
                if len(block) > MAX_FRAME_CHARS:
                    raise InternalCompletionError(502)
                await process(block)
            if len(buf) > MAX_FRAME_CHARS:
                raise InternalCompletionError(502)
        if buf.strip():
            await process(buf)
        if not (done or finish_seen):
            raise InternalCompletionError(502)
        try:
            tail, _ = redactor.finish()
        except ReasoningTagLimitError as exc:
            raise InternalCompletionError(502) from exc
        await emit(tail)
    except BaseException:
        failed = True
        raise
    finally:
        try:
            await upstream.aclose()
        except Exception as exc:
            if not failed:
                raise InternalCompletionError(502) from exc
    text = "".join(parts)
    if not text.strip():
        raise InternalCompletionError(None)
    return text
