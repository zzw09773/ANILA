"""OpenAI-compatible API proxy endpoints."""
import asyncio
import json
import logging
import re
import time
from types import SimpleNamespace
from typing import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy.orm import Session
from anila_core.security.external_content import (
    AUDIT_ACTION,
    PRIORITY_RULE_EN,
    PRIORITY_RULE_ZH,
    SOURCES,
    Finding,
    StreamTextGuard,
    TurnSidechannel,
    compose_external_message,
    insert_external_message,
    sanitize_model_output,
    wrap_external,
)
from app.database import SessionLocal, get_db
from app.services.audit_service import log_audit_event
from app.middleware.caller import Caller, get_caller
from app.services.agent_availability import AGENT_TEMPORARILY_UNAVAILABLE
from app.services.proxy.dispatch_chat import (
    DispatchModelCall,
    enforce_dispatched_model_ceiling,
    resolve_chat_caller,
)
from app.models.agent import Agent, UserAgentPermission
from app.models.attachment import Attachment
from app.models.conversation import Conversation
from app.models.model_registry import ModelRegistry
from app.models.platform_setting import get_kb_threshold, get_setting
from app.schemas.contracts.classification import ClassificationLevel
from app.services import memory_service
from app.services.agent_reply_signal import attach_agent_reply_observation
from app.services.institutional_kb import (
    KbResult,
    KbState,
    retrieve_institutional,
)
from app.services.api_key_service import check_model_permission, check_agent_permission
from app.services.health_checker import HEALTH_HEALTHY, normalize_health_status
from app.services.model_roles import resolve_role
from app.services.auth_service import is_admin_tier
from app.services.proxy.ceiling import enforce_agent_ceiling, enforce_model_ceiling
from app.services.proxy.headers import resolve_model_gateway_key
from app.services.proxy.service import resolve_proxy_tuning
from app.services.proxy.task_link import begin_task_run, finalize_task_run
from app.services.proxy.urls import join_upstream_path
from app.services.proxy_service import (
    _estimate_token_count,
    _extract_response_text,
    build_default_anila_meta,
    downstream_identity,
    proxy_request,
    proxy_stream,
)
from app.services.endpoint_author_service import visible_endpoint_url

logger = logging.getLogger(__name__)


def _endpoint_display_for(
    db: Session,
    caller_user,
    endpoint_url: str,
    *,
    is_internal: bool = False,
) -> str:
    """Visibility-gated address for proxy trace / failure faces."""
    return visible_endpoint_url(
        endpoint_url,
        is_internal=is_internal,
        db=db,
        caller=caller_user,
    )

def _coerce_conversation_id(raw: str | None) -> int | None:
    """Convert the X-ANILA-Conversation-Id header to int for FK use.

    The header is free-form per the proxy contract — clients send the
    int row PK as a string today, but legacy / external callers may
    send non-numeric ids (e.g. UUIDs). Memory write paths need a real
    FK, so non-coercible values disable the writer for this turn but
    still allow the reader (which only depends on user_id).
    """
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _require_conversation_access(
    db: Session, caller: Caller, conversation_id: int
) -> None:
    """Ensure caller-supplied conversation id belongs to this caller.

    ``X-ANILA-Conversation-Id`` drives memory writes and classification
    latching, so accepting an arbitrary numeric id would let one user mutate
    another user's conversation metadata. Admin-tier callers retain the
    existing operational bypass.

    Unauthorised and missing collapse to the same 404 (models pattern) so
    existence is not an oracle.
    """
    conv = db.get(Conversation, conversation_id)
    if conv is None or (
        conv.user_id != caller.user.id and not is_admin_tier(caller.user)
    ):
        raise HTTPException(status_code=404, detail="Conversation not found")


def _agent_policy_level(agent) -> ClassificationLevel:
    """The agent's own four-level classification floor (SYSTEM-MAP §8).

    ``default_classification_level`` (set by the 3a migration bridge / manual
    inventory) is the source of truth; a ``requires_encryption=true`` agent
    without a manual level still floors at RESTRICTED(密) — preserves the old
    rank-2 floor that previously was 機密 in the five-level set
    (SYSTEM-MAP §8), so the legacy boolean stays byte-compatible.
    """
    level = ClassificationLevel.from_storage(
        getattr(agent, "default_classification_level", None) or "無機密"
    )
    if bool(getattr(agent, "requires_encryption", False)):
        level = ClassificationLevel.max_of(
            [level, ClassificationLevel.RESTRICTED]
        )
    return level


def _latch_agent_classification(
    db: Session, conversation_id: int, level: str = ClassificationLevel.RESTRICTED.to_storage()
) -> None:
    """Latch the routed agent's classification onto the conversation row.

    Slice 3b: routes through the four-level one-way core
    (``apply_classification`` reason=``agent_policy``) instead of the old raw
    ``UPDATE ... SET classified=TRUE``. The core mirrors the legacy boolean
    (``classified = level >= 密`` / RESTRICTED; old rank-2 floor,
    SYSTEM-MAP §8) so a hard refresh still latches the UI back into encrypted
    mode; it never lowers (single-direction), and leaves
    ``classification_inherited`` untouched (the source is agent policy, not
    memory inheritance — that path is handled separately below).
    """
    from app.modules.policy import apply_classification
    apply_classification(
        db,
        resource_type="conversation",
        resource_id=str(conversation_id),
        new_level=level,
        actor_type="service",
        actor_id="agent-policy",
        reason="agent_policy",
        source="agent_policy",
    )


def _latch_inherited_classification(db: Session, conversation_id: int) -> None:
    """Mark the conversation as classified-via-inheritance (memory recall).

    Slice 3b: routes through the four-level one-way core
    (``apply_classification`` reason=``memory_inherited``), which floors the
    row at RESTRICTED(密) — preserves the old rank-2 floor (SYSTEM-MAP §8),
    mirrors the legacy boolean AND flips ``classification_inherited=TRUE``
    on the raising event. One-way — never lowers a row already at 密 or higher.
    """
    from app.modules.policy import apply_classification
    apply_classification(
        db,
        resource_type="conversation",
        resource_id=str(conversation_id),
        new_level=ClassificationLevel.RESTRICTED.to_storage(),
        actor_type="service",
        actor_id="memory",
        reason="memory_inherited",
        source="memory_inherited",
    )


def _propagate_conversation_level_to_task(
    db: Session, task_id: int, conversation_id: int
) -> None:
    """Slice 3b: carry the conversation's effective level onto the linked
    task so later ceiling checks (doc 08 §4/§10) see it.

    reason=``source_selected`` — the runtime conversation is the selected
    source context feeding the task (doc 08 §4 task.level = max(...,
    source_snapshot.level, ...)). One-way core → never lowers the task.
    No-op when the conversation is unclassified (nothing to raise to).
    """
    from app.modules.policy import apply_classification, effective_level
    conv_level = effective_level(
        db, resource_type="conversation", resource_id=str(conversation_id)
    )
    # Not an OE-4 outbound gate: one-way latch/propagation skip when there
    # is nothing above 無機密 to raise the task to (SYSTEM-MAP §8 latch).
    if conv_level <= ClassificationLevel.UNCLASSIFIED:
        return
    apply_classification(
        db,
        resource_type="task",
        resource_id=str(task_id),
        new_level=conv_level.to_storage(),
        actor_type="service",
        actor_id="task-link",
        reason="source_selected",
        task_id=task_id,
        source="conversation_propagation",
    )


def _extract_assistant_text(payload: dict | None) -> str | None:
    """Pull the assistant message text out of an OpenAI chat response."""
    if not isinstance(payload, dict):
        return None
    choices = payload.get("choices") or []
    for c in choices:
        if not isinstance(c, dict):
            continue
        msg = c.get("message") or {}
        content = msg.get("content")
        if isinstance(content, str) and content:
            return content
    return None


def _annotate_agent_reply_payload(payload: dict, model_name: str) -> dict:
    """Attach the platform-only completion-length observation to agent meta."""
    try:
        usage = payload.get("usage") if isinstance(payload, dict) else None
        reported = usage.get("completion_tokens") if isinstance(usage, dict) else None
        if (
            isinstance(reported, int)
            and not isinstance(reported, bool)
            and reported >= 0
        ):
            completion_tokens = reported
            usage_source = "reported"
        else:
            completion_tokens = _estimate_token_count(
                model_name,
                _extract_response_text(payload),
            )
            usage_source = "estimated"

        meta = payload.get("anila_meta")
        if not isinstance(meta, dict):
            meta = {}
            payload["anila_meta"] = meta
        attach_agent_reply_observation(
            meta,
            completion_tokens=completion_tokens,
            usage_source=usage_source,
        )
    except Exception:  # pragma: no cover - defensive serving boundary
        logger.exception("agent reply observation annotation failed")
    return payload


def _extract_latest_user_message(body: dict) -> str | None:
    """Pull the most recent user-role message text out of an OpenAI body."""
    messages = body.get("messages") or []
    for msg in reversed(messages):
        if not isinstance(msg, dict):
            continue
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            return content
        # Multimodal content: concatenate text parts only — image / audio
        # parts are dropped because the embedder is text-only.
        if isinstance(content, list):
            parts = [p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"]
            joined = " ".join(p for p in parts if p)
            return joined or None
    return None


def _memory_confined_to_conversation(
    db: Session, conversation_id: int | None
) -> int | None:
    """P4.5 — return the conversation memory recall may not leave, else None.

    Owner rule (PLAN.md §4.4/4.5, 2026-07-30):「ANILALM 的「同一 session」=
    **同一個對話框**;不是關分頁,也不是登出。」So an ANILALM conversation
    recalls from itself and from nowhere else, while ANILA keeps the
    cross-conversation long-term memory SYSTEM-MAP §5 grants it (L51:
    長期記憶 ANILA ✓ / ANILALM 只在同一 session 內).

    ``origin`` on the conversation row is the only signal that says which
    front end a completion came from; without a conversation id there is
    no origin to read, so this returns None. That is not a hole in
    practice — ANILALM cannot create a conversation without going through
    ``POST /conversations`` with ``origin='anilalm'`` (and a required
    ``collection_id``), and it sends the id back on every completion.
    """
    if conversation_id is None:
        return None
    origin = (
        db.query(Conversation.origin)
        .filter(Conversation.id == conversation_id)
        .scalar()
    )
    return conversation_id if origin == "anilalm" else None


def _quoted_memory_message(block: str):
    """已存的事實是外來內容，放在 user，不進 system。"""
    return wrap_external("memory", "memory", block)


async def _inject_memory(
    db: Session,
    user_id: int,
    body: dict,
    *,
    exclude_conversation_id: int | None,
    side: TurnSidechannel | None = None,
) -> memory_service.MemoryReadResult | None:
    """把記憶區塊插在系統訊息之後、第一則非系統訊息之前。

    2026-09-02 (harness §6-1): 系統提示的前綴要保持逐字相同，前綴快取才打得中。
    記憶是使用者衍生的文字，另外放進標成不可遵循的 user 訊息，不寫進 system。

    Returns the read result (so the caller can inspect
    ``encryption_inherited``) or None when there's no user message to
    embed against. Failures are swallowed and logged — memory must
    not break chat.
    """
    user_text = _extract_latest_user_message(body)
    if not user_text:
        return None
    only_conversation_id = _memory_confined_to_conversation(
        db, exclude_conversation_id
    )
    try:
        result = await memory_service.build_memory_block(
            db,
            user_id=user_id,
            latest_user_message=user_text,
            exclude_conversation_id=exclude_conversation_id,
            only_conversation_id=only_conversation_id,
        )
    except Exception:
        logger.exception("memory_service: build_memory_block failed user_id=%s", user_id)
        return None

    if not result.block:
        return result

    messages = list(body.get("messages") or [])
    wrapped = _quoted_memory_message(result.block)
    if side is not None:
        side.add_wrap(wrapped)
    insert_external_message(messages, wrapped.message)
    body["messages"] = messages
    return result


def _inject_attachments(
    db: Session,
    conversation_id: int | None,
    body: dict,
    model_name: str | None,
    side: TurnSidechannel | None = None,
) -> "attachment_context.AttachmentInjectResult | None":
    """附件狀態留在系統訊息；抽出的本文改走外來內容包裝，不進 system。

    Unlike memory injection, failures are recorded (not swallowed) so the
    chat handler can put a trace entry on ``anila_meta``. Chat still proceeds.
    Returns None when there is no conversation id (nothing to do).

    Admission is derived via ``admit()`` against this turn's model window —
    nothing is written back to extract_status. Loads metadata first, then
    ``extracted_text`` only for admitted ids.
    """
    from app.services import attachment_context

    if conversation_id is None:
        return None

    try:
        # Metadata only — do not pull unbounded extracted_text for every row.
        meta_rows = (
            db.query(
                Attachment.id,
                Attachment.filename,
                Attachment.page_count,
                Attachment.token_count,
                Attachment.extract_status,
                Attachment.extract_error,
                Attachment.created_at,
            )
            .filter(Attachment.conversation_id == conversation_id)
            .order_by(Attachment.created_at.asc(), Attachment.id.asc())
            .all()
        )
        if not meta_rows:
            return None

        # model_name None → explicit default-window fallback.
        context_window = attachment_context.get_context_window(db, model_name)
        budget = attachment_context.attachment_budget_tokens(db, context_window)
        admitted_list, excluded_list = attachment_context.admit(db, meta_rows, budget)
        admitted_set = set(admitted_list)

        text_by_id: dict[int, str | None] = {}
        if admitted_list:
            text_by_id = dict(
                db.query(Attachment.id, Attachment.extracted_text)
                .filter(Attachment.id.in_(admitted_list))
                .all()
            )

        class _PromptRow:
            __slots__ = (
                "id", "filename", "page_count", "token_count",
                "extract_status", "extract_error", "extracted_text",
            )

            def __init__(self, row, text: str | None):
                self.id = row.id
                self.filename = row.filename
                self.page_count = row.page_count
                self.token_count = row.token_count
                self.extract_status = row.extract_status
                self.extract_error = row.extract_error
                self.extracted_text = text

        views = [
            _PromptRow(
                r,
                text_by_id.get(r.id) if r.id in admitted_set else None,
            )
            for r in meta_rows
        ]
        block = attachment_context.build_attachment_prompt_block(
            views, admitted_ids=admitted_set, include_bodies=False,
        )
        if block is None:
            return None

        messages = list(body.get("messages") or [])
        if messages and isinstance(messages[0], dict) and messages[0].get("role") == "system":
            existing = messages[0].get("content") or ""
            if isinstance(existing, str):
                messages[0] = {
                    **messages[0],
                    "content": f"{existing}\n\n{block}" if existing else block,
                }
            else:
                messages[0] = {
                    **messages[0],
                    "content": [
                        *list(existing),
                        {"type": "text", "text": block},
                    ],
                }
        else:
            messages.insert(0, {"role": "system", "content": block})
        wraps = []
        for view in views:
            status = view.extract_status or "pending"
            if status == "ok" and view.id in admitted_set and view.extracted_text:
                wraps.append(
                    wrap_external(
                        "attachment",
                        str(view.id),
                        f"檔名：{view.filename}\n{view.extracted_text}",
                    )
                )
        if wraps:
            if side is not None:
                for wrapped in wraps:
                    side.add_wrap(wrapped)
            insert_external_message(messages, compose_external_message(wraps))
        body["messages"] = messages

        ok_n = len(admitted_list)
        pending_n = sum(
            1 for a in views if (a.extract_status or "") == "pending"
        )
        omitted_n = (
            len(excluded_list)
            + sum(
                1
                for a in views
                if (a.extract_status or "") in (
                    "failed", "unsupported", "too_large",
                )
            )
        )
        detail_parts = [f"納入 {ok_n} 份"]
        if pending_n:
            detail_parts.append(f"處理中 {pending_n} 份")
        if omitted_n:
            detail_parts.append(f"未納入 {omitted_n} 份")
        return attachment_context.AttachmentInjectResult(
            status="ok" if omitted_n == 0 and pending_n == 0 else "partial",
            label="附件注入",
            detail="；".join(detail_parts),
            injected_count=ok_n,
        )
    except Exception as exc:
        logger.exception(
            "attachment inject failed conv_id=%s", conversation_id,
        )
        return attachment_context.AttachmentInjectResult(
            status="error",
            label="附件注入",
            detail=f"注入失敗：{type(exc).__name__}",
            skipped=True,
        )


def _merge_attachment_trace(payload: dict, inject_result) -> dict:
    """Append an attachment trace entry onto ``payload['anila_meta']``."""
    if inject_result is None or not isinstance(payload, dict):
        return payload
    meta = payload.get("anila_meta")
    if not isinstance(meta, dict):
        meta = build_default_anila_meta(
            "attachments",
            detail="attachment inject",
        )
        payload["anila_meta"] = meta
    trace = meta.get("trace")
    if not isinstance(trace, list):
        trace = []
        meta["trace"] = trace
    trace.append(inject_result.to_trace_entry())
    return payload


async def _sse_with_attachment_trace(
    upstream: AsyncIterator[str],
    inject_result,
) -> AsyncIterator[str]:
    """Inject the attachment trace entry into streaming ``anila.meta`` frames.

    Mirrors ``_merge_attachment_trace`` for the SSE path: the terminal
    metadata frame (synthesised by proxy_stream when the downstream omits
    one, or forwarded when present) carries the same attachment entry as
    the non-streaming ``anila_meta.trace``. Stays in proxy.py so the
    P1.5 change set does not touch ``proxy/service.py``.
    """
    import json

    if inject_result is None:
        async for chunk in upstream:
            yield chunk
        return

    entry = inject_result.to_trace_entry()
    buf = ""
    async for chunk in upstream:
        buf += chunk
        while "\n\n" in buf:
            block, buf = buf.split("\n\n", 1)
            block_out = block + "\n\n"
            event_name = None
            data_line = None
            for line in block.split("\n"):
                if line.startswith("event:"):
                    event_name = line[6:].strip()
                elif line.startswith("data:"):
                    data_line = line[5:].strip()
            if event_name == "anila.meta" and data_line and data_line != "[DONE]":
                try:
                    meta = json.loads(data_line)
                except (json.JSONDecodeError, TypeError):
                    yield block_out
                    continue
                if isinstance(meta, dict):
                    trace = meta.get("trace")
                    if not isinstance(trace, list):
                        trace = []
                        meta["trace"] = trace
                    trace.append(entry)
                    yield (
                        "event: anila.meta\n"
                        + "data: "
                        + json.dumps(meta, ensure_ascii=False)
                        + "\n\n"
                    )
                    continue
            yield block_out
    if buf:
        yield buf


# ── 院內規章檢索與注入（SYSTEM-MAP §3 的「不需要 agent」那條路，Q39）─────────
#
# 觸發條件是 **``X-ANILA-Route`` 這個 header 在**，不是「router 已經決定直答」
# ——那個判定在時序上晚於這通呼叫（task-5-report.md §1：判定就是從這通呼叫的
# 回覆解析出來的）。header 的語意是「這是 router 的答案通道」。代價講明白：
# 派工收尾的回合會白做一次檢索，離題的問題靠分數門檻擋掉。
#
# ⚠ 反過來那一半才是本包真正要消滅的形狀：**沒有這個 header 的呼叫（agent
# 派工、ANILALM）一律不檢索**。派工回合那通的命中要是漏進使用者看得到的
# payload，就會出現「以為有依據、其實那不是給他看的東西」。
#
# ⚠ 注入**只騎系統訊息**（照 ``_inject_memory`` 的樣板），永遠不與 user /
# assistant 回合交錯。被標記的多輪合成呼叫裡對話已經含有 agent 的輸出，靠這個
# 結構性的分界，規章段落與 agent 文字才分得開——不必靠讀字面去猜哪一段是規章。

_ROUTE_HEADER = "X-ANILA-Route"

_KB_BLOCK_TITLE = "【院內規章檢索結果】"
# ⚠ 這一句是硬規則，不是文案：使用者信的是正文，不是標記。沒有依據卻用條號
# 說話，看起來就跟有依據一模一樣。
_KB_NO_CITATION_RULE = "不得以條號格式引用"
# miss 與 error 的第一句必須不同——「查過，沒有」和「查不了」是兩件事，混成
# 同一句話，兩個狀態就從使用者那邊消失了。
_KB_MISS_NOTICE = "已查詢院內規章知識庫，沒有找到相關條文"
_KB_ERROR_NOTICE = "本次無法查詢院內規章知識庫（檢索失敗）"
_KB_NO_BASIS_INSTRUCTION = (
    "請以一般知識的口吻作答，並明白告訴使用者這個回答沒有院內規章作為依據；"
    f"{_KB_NO_CITATION_RULE}（例如「依第三條」「依 XX 要點第五點」），"
    "也不得杜撰任何條號、函頒日期或文號。"
)
_KB_HIT_INSTRUCTION = (
    "後面的參考資料是從平台已標記可搜的知識庫查到的段落，依相關度排序。回答時以這些段落"
    "為依據，並在用到某一段時於句末標出該段的編號（例如 [1]）。段落之外的內容"
    "請說明是一般知識，不要寫成知識庫裡的規定。"
    "若段落與使用者問題無關，直接忽略，不要引用也不要提及。"
)
_KB_PARTIAL_NOTICE = (
    "⚠ 有 {n} 個規章庫這次查詢失敗，以下**不是**全部的依據；回答時請一併告訴"
    "使用者這次的檢索並不完整。"
)
# citation 的 snippet 只是抽屜裡的預覽；完整內容在 ``kb_hits``。
_KB_SNIPPET_CHARS = 200
_KB_TRACE_LABEL = "院內規章檢索"


def _route_marked(headers) -> bool:
    """這通呼叫是不是 router 的答案通道。

    值是 ``direct`` 還是 ``forced`` 在這裡不重要（router 已正規化，前端造得出
    的只有 ``forced``）——**存在即檢索**。空白值當作沒送：router 永遠送得出
    正規化過的值，只有別的來源會送出空的。
    """
    raw = headers.get(_ROUTE_HEADER)
    return bool(raw and raw.strip())


async def _retrieve_institutional_kb(
    db: Session,
    user,
    *,
    marked: bool,
    query: str | None,
) -> KbResult:
    """跑檢索，回一個**一定有狀態**的結果。

    ⚠ 狀態一律取自 ``KbResult.state``，呼叫端不得自行重推：``SEARCH_ERROR``
    優先於 ``PARTIAL_ERROR`` 的次序是模組內釘死的不變式
    （institutional_kb.py:225-227），在這裡重推等於把修好的缺陷蓋回來。
    唯一由本函式決定狀態的情形是**根本沒有 KbResult**（沒標記、沒有問題文字、
    或模組整個拋例外），三種都不是在重推分界。
    """
    if not marked or not query:
        # 沒標記 = 沒搜過（不是「搜了沒有」）；沒有使用者訊息也沒得搜。
        return KbResult(state=KbState.NOT_SEARCHED)
    try:
        # ⚠ 每個請求重讀門檻。這裡加任何快取，設定頁上那個數字就變成假控制項
        # （platform_setting.py 的模組 docstring）。
        threshold = get_kb_threshold(db)
        return await retrieve_institutional(db, user, query, threshold=threshold)
    except Exception:
        # 設計 §5：檢索失敗**絕不擋回答**。查不了與沒命中是兩件事，所以這裡是
        # SEARCH_ERROR 而不是靜靜地當作沒命中。
        logger.exception("institutional_kb: 檢索失敗 user_id=%s", getattr(user, "id", None))
        return KbResult(state=KbState.SEARCH_ERROR)


def _build_kb_block(result: KbResult) -> str | None:
    """把檢索結果變成要注入系統訊息的那一段字（None ＝ 什麼都不注入）。"""
    if result.state is KbState.NOT_SEARCHED:
        # 一個庫都沒標記的院所，聊天內容不該因為這個功能而改變。
        return None
    parts = [_KB_BLOCK_TITLE]
    if result.hits:
        if result.failed_collections:
            parts.append(_KB_PARTIAL_NOTICE.format(n=len(result.failed_collections)))
        parts.append(_KB_HIT_INSTRUCTION)
    elif result.state is KbState.SEARCHED_MISS:
        parts.append(f"{_KB_MISS_NOTICE}。{_KB_NO_BASIS_INSTRUCTION}")
    else:
        # SEARCH_ERROR，以及「有狀態卻沒有段落」的退化情形：兩者共通的事實是
        # 手上沒有任何可以引用的院規，所以走同一條「不准用條號說話」的指示。
        parts.append(f"{_KB_ERROR_NOTICE}。{_KB_NO_BASIS_INSTRUCTION}")
    return "\n\n".join(parts)


# 規章段落之後再提醒一次語言（harness §6-4）：長 context 下小模型會忘記前導
# 開頭的語言規則，這一行是最便宜的修法。它永遠是 system 訊息的最後一行。
KB_LANGUAGE_REMINDER = "預設使用繁體中文（台灣用語）；使用者明確指定語言時依其指定。"


def _inject_kb_block(body: dict, block: str) -> None:
    """Mutate ``body`` in-place to append the regulation block to the system msg.

    照 ``_inject_memory`` 的樣板：有 system 訊息就 **append**，沒有就在 index 0
    插一則；區塊之後補一行語言提醒。**只動 messages[0]**。
    2026-09-02 之前是 prepend，把 Router 的靜態前導推到中段（見 harness §6-1）。
    """
    tail = f"{block}\n\n{KB_LANGUAGE_REMINDER}"
    messages = list(body.get("messages") or [])
    if messages and isinstance(messages[0], dict) and messages[0].get("role") == "system":
        existing = messages[0].get("content") or ""
        if isinstance(existing, str):
            messages[0] = {
                **messages[0],
                "content": f"{existing}\n\n{tail}" if existing else tail,
            }
        else:
            # Multimodal system content — 規章段落當成一個並列的 text part，
            # 接在既有 parts 之後，不去動它們。
            messages[0] = {
                **messages[0],
                "content": [*list(existing), {"type": "text", "text": tail}],
            }
    else:
        messages.insert(0, {"role": "system", "content": tail})
    body["messages"] = messages


def _inject_kb_passages(body: dict, result: KbResult, side: TurnSidechannel | None) -> None:
    """規章段落是外來內容，只放 user 訊息。系統訊息只留平台自己的引用規則。"""
    if not result.hits:
        return
    wraps = [
        wrap_external(
            "kb",
            str(hit.document_id),
            f"[{idx}]（來源：{hit.filename}）\n{hit.content}",
        )
        for idx, hit in enumerate(result.hits, start=1)
    ]
    if side is not None:
        for wrapped in wraps:
            side.add_wrap(wrapped)
    messages = list(body.get("messages") or [])
    insert_external_message(messages, compose_external_message(wraps))
    body["messages"] = messages


def _inject_client_passages(body: dict, raw, side: TurnSidechannel | None) -> None:
    """ANILA LM 把檢索段落交上來，由這裡組進同一種包裝。"""
    if not isinstance(raw, list):
        return
    wraps = []
    for item in raw[:20]:
        if not isinstance(item, dict):
            continue
        source = item.get("source") or "kb"
        if source not in SOURCES:
            source = "kb"
        text = item.get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        document_id = str(item.get("id") or "passage")[:100]
        wraps.append(wrap_external(source, document_id, text[:8000]))
    if not wraps:
        return
    if side is not None:
        for wrapped in wraps:
            side.add_wrap(wrapped)
    messages = list(body.get("messages") or [])
    insert_external_message(messages, compose_external_message(wraps))
    body["messages"] = messages


def _has_cjk(text: str) -> bool:
    return any("\u4e00" <= char <= "\u9fff" for char in text or "")


def _ensure_priority_rule(body: dict) -> None:
    """拿掉呼叫端貼的優先順序，再把可信規則附在最後一則系統訊息尾端。"""
    from anila_core.security.external_content import strip_priority_copies

    messages = list(body.get("messages") or [])

    def _text(message: dict) -> str:
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "\n".join(
                part.get("text", "")
                for part in content
                if isinstance(part, dict) and isinstance(part.get("text"), str)
            )
        return ""

    blob = "\n".join(
        _text(message)
        for message in messages
        if isinstance(message, dict) and message.get("role") == "system"
    )
    sample = blob or (_extract_latest_user_message({"messages": messages}) or "")
    rule = PRIORITY_RULE_EN if sample and not _has_cjk(sample) else PRIORITY_RULE_ZH
    last_system: int | None = None
    for index, message in enumerate(messages):
        if not isinstance(message, dict) or message.get("role") != "system":
            continue
        content = message.get("content")
        if isinstance(content, str):
            messages[index] = {**message, "content": strip_priority_copies(content)}
        elif isinstance(content, list):
            cleaned_parts = []
            for part in content:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    cleaned_parts.append({**part, "text": strip_priority_copies(part["text"])})
                else:
                    cleaned_parts.append(part)
            messages[index] = {**message, "content": cleaned_parts}
        last_system = index
    if last_system is None:
        messages.insert(0, {"role": "system", "content": rule})
        body["messages"] = messages
        return
    message = messages[last_system]
    content = message.get("content")
    if isinstance(content, str):
        messages[last_system] = {
            **message,
            "content": f"{content.rstrip()}\n\n{rule}" if content.strip() else rule,
        }
    elif isinstance(content, list):
        messages[last_system] = {
            **message,
            "content": [*list(content), {"type": "text", "text": rule}],
        }
    else:
        messages[last_system] = {"role": "system", "content": rule}
    body["messages"] = messages


def _audit_injection(db: Session, actor, findings: list[Finding]) -> None:
    """寫 prompt_injection_suspected。metadata 只有來源、id、規則，沒有全文。"""
    if not findings or db is None:
        return
    for item in findings:
        log_audit_event(
            db,
            action=AUDIT_ACTION,
            resource_type=(item.source or "external")[:50],
            resource_id=(item.document_id or "")[:100],
            actor=actor,
            metadata={
                "source": item.source,
                "document_id": item.document_id,
                "rule_id": item.rule_id,
            },
            detail=None,
            commit=False,
        )
    try:
        db.commit()
    except Exception:
        logger.exception("prompt_injection_suspected 稽核寫入失敗")
        try:
            db.rollback()
        except Exception:
            pass


def _audit_injection_later(actor, findings: list[Finding]) -> None:
    """串流收尾時請求的 session 可能已經關了，另開一筆。"""
    if not findings:
        return
    snap = None
    if actor is not None:
        snap = SimpleNamespace(id=getattr(actor, "id", None), username=getattr(actor, "username", None))
    db = SessionLocal()
    try:
        _audit_injection(db, snap, findings)
    finally:
        db.close()


def _take_external_sidechannel(body: dict) -> tuple[TurnSidechannel, object]:
    """把呼叫端附帶的段落與協定行取出。這些欄位不能送到模型。"""
    side = TurnSidechannel()
    raw_lines = body.pop("anila_protocol_corpus", None)
    raw_findings = body.pop("anila_injection_findings", None)
    raw_passages = body.pop("anila_external_passages", None)
    if isinstance(raw_lines, list):
        side.add_protocol_lines(
            line for line in raw_lines[:50] if isinstance(line, str)
        )
    if isinstance(raw_findings, list):
        side.add_findings(raw_findings[:50])
    return side, raw_passages


def _apply_output_guard(payload, side: TurnSidechannel):
    """非串流回答：回聲不當指令、外連改純文字、尾段固定句遮掉。"""
    if not isinstance(payload, dict):
        return payload
    choices = payload.get("choices")
    if isinstance(choices, list):
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            message = choice.get("message")
            if not isinstance(message, dict):
                continue
            content = message.get("content")
            if not isinstance(content, str) or not content:
                continue
            result = sanitize_model_output(
                content,
                originals=side.originals,
                protocol_lines=side.protocol_lines,
            )
            message["content"] = result.text
            if result.echoed and not any(item.rule_id == "protocol_echo" for item in side.findings):
                side.findings.append(Finding("output", "output", "protocol_echo"))
    if side.suspicious and isinstance(payload.get("anila_meta"), dict):
        payload["anila_meta"]["prompt_injection_suspected"] = True
    elif side.suspicious:
        payload["anila_meta"] = {"prompt_injection_suspected": True}
    return payload


def _content_sse(text: str) -> str:
    payload = {"choices": [{"index": 0, "delta": {"content": text}}]}
    return "data: " + json.dumps(payload, ensure_ascii=False)


def _format_sse(event_name: str | None, payload: dict) -> str:
    data = "data: " + json.dumps(payload, ensure_ascii=False)
    if event_name:
        return f"event: {event_name}\n{data}"
    return data


def _rewrite_sse_block(block: str, guard: StreamTextGuard, side: TurnSidechannel) -> str:
    """清理一個 SSE 事件。沒有需要改的內容時，原樣送出。"""
    event_name = None
    data_lines: list[str] = []
    other = False
    for line in block.split("\n"):
        if line.startswith("event:"):
            event_name = line[6:].strip()
        elif line.startswith("data:"):
            data_lines.append(line[5:].strip())
        elif line.strip():
            other = True
    if other or len(data_lines) != 1:
        return block
    data = data_lines[0]
    if data == "[DONE]":
        tail = guard.flush()
        if guard.echoed and not any(item.rule_id == "protocol_echo" for item in side.findings):
            side.findings.append(Finding("output", "output", "protocol_echo"))
        prefix = (_content_sse(tail) + "\n\n") if tail else ""
        return prefix + block
    try:
        payload = json.loads(data)
    except json.JSONDecodeError:
        return block
    if not isinstance(payload, dict):
        return block
    if event_name == "anila.meta" or (
        "choices" not in payload and event_name and event_name.startswith("anila.")
    ):
        if side.suspicious:
            payload["prompt_injection_suspected"] = True
            return _format_sse(event_name, payload)
        return block
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return block
    choice = choices[0]
    container = None
    for key in ("delta", "message"):
        candidate = choice.get(key)
        if isinstance(candidate, dict) and isinstance(candidate.get("content"), str) and candidate.get("content"):
            container = candidate
            break
    if container is None:
        return block
    original = container["content"]
    emitted = guard.push(original)
    if emitted == original and not guard.pending:
        return block
    container["content"] = emitted
    return _format_sse(event_name, payload)


async def _guard_sse_stream(
    upstream: AsyncIterator[str],
    side: TurnSidechannel,
    actor,
) -> AsyncIterator[str]:
    """串流出口套上輸出檢查。可疑標記由後面的 meta 框補上。"""
    guard = StreamTextGuard(side.originals, side.protocol_lines)
    buf = ""
    try:
        async for chunk in upstream:
            buf += chunk
            while "\n\n" in buf:
                block, buf = buf.split("\n\n", 1)
                yield _rewrite_sse_block(block, guard, side) + "\n\n"
        if buf.strip():
            yield _rewrite_sse_block(buf, guard, side) + "\n\n"
    finally:
        tail = guard.flush()
        if guard.echoed and not any(item.rule_id == "protocol_echo" for item in side.findings):
            side.findings.append(Finding("output", "output", "protocol_echo"))
        if tail:
            yield _content_sse(tail) + "\n\n"
        echoes = [item for item in side.findings if item.rule_id == "protocol_echo"]
        if echoes:
            _audit_injection_later(actor, echoes)


async def _sse_with_injection_notice(
    upstream: AsyncIterator[str],
    side: TurnSidechannel,
) -> AsyncIterator[str]:
    """回覆詳情要看得到「已忽略」。只在有可疑輸入時蓋旗標。"""
    if not side.suspicious:
        async for chunk in upstream:
            yield chunk
        return
    buf = ""
    async for chunk in upstream:
        buf += chunk
        while "\n\n" in buf:
            block, buf = buf.split("\n\n", 1)
            yield _stamp_notice_block(block) + "\n\n"
    if buf:
        yield _stamp_notice_block(buf) if buf.strip() else buf


def _stamp_notice_block(block: str) -> str:
    event_name = None
    data_lines: list[str] = []
    for line in block.split("\n"):
        if line.startswith("event:"):
            event_name = line[6:].strip()
        elif line.startswith("data:"):
            data_lines.append(line[5:].strip())
    if event_name != "anila.meta" or len(data_lines) != 1:
        return block
    try:
        payload = json.loads(data_lines[0])
    except json.JSONDecodeError:
        return block
    if not isinstance(payload, dict):
        return block
    payload["prompt_injection_suspected"] = True
    return _format_sse(event_name, payload)


def _kb_meta_fragment(result: KbResult) -> dict:
    """要蓋到每一個 payload 出口上的 ``anila_meta`` 片段。

    ``kb_state`` **一定**在（硬規則 1）。``citations`` 沿用既有的 drawer 契約
    ``{id, title, score?, snippet?}``，``id`` 必填且必須逐筆唯一——命中是 chunk
    級的，同一份文件可以中兩段，id 撞號會讓抽屜對到錯的訊息（app.jsx 用 id
    跨訊息找來源）。第 N 段對應 ``citations[N-1]``，router 提示詞裡既有的
    ``[N]`` 指令因此活過來。
    """
    fragment: dict = {
        "kb_state": result.state.value,
        "kb_hits": [
            {
                "collection_id": hit.collection_id,
                "document_id": hit.document_id,
                "filename": hit.filename,
                "content": hit.content,
                "score": hit.score,
            }
            for hit in result.hits
        ],
    }
    if result.failed_collections:
        fragment["kb_failed_collections"] = list(result.failed_collections)
    if result.hits:
        fragment["citations"] = [
            {
                "id": f"kb:{hit.collection_id}:{hit.document_id}:{idx}",
                "title": hit.filename,
                "score": hit.score,
                "snippet": hit.content[:_KB_SNIPPET_CHARS],
                **({"image_pks": list(hit.image_pks)} if hit.image_pks else {}),
            }
            for idx, hit in enumerate(result.hits, start=1)
        ]
    return fragment


def _kb_trace_entry(fragment: dict) -> dict | None:
    """檢索這件事在 trace 上的樣子（沒搜過就不留痕，避免每一通聊天都多一行）。"""
    state = fragment.get("kb_state")
    if state in (None, KbState.NOT_SEARCHED.value):
        return None
    hit_n = len(fragment.get("kb_hits") or [])
    failed_n = len(fragment.get("kb_failed_collections") or [])
    if state == KbState.SEARCHED_HIT.value:
        detail, status = f"命中 {hit_n} 段", "ok"
    elif state == KbState.SEARCHED_MISS.value:
        detail, status = "查過，沒有找到相關條文", "ok"
    elif state == KbState.PARTIAL_ERROR.value:
        detail = f"命中 {hit_n} 段，但有 {failed_n} 個庫查詢失敗"
        status = "partial"
    else:
        detail, status = "檢索失敗，本次回答沒有院規依據", "error"
    return {
        "kind": "institutional_kb",
        "label": _KB_TRACE_LABEL,
        "detail": detail,
        "status": status,
    }


def _apply_kb_fragment(meta: dict, fragment: dict) -> None:
    """把片段蓋到一份 ``anila_meta`` 上（payload 與 SSE 共用同一段邏輯）。"""
    for key, value in fragment.items():
        if key == "citations":
            # 不覆蓋下游自己的 citations：我們的排在前面，[N] 才對得上，
            # 而下游（例如 agent）給的來源也不會被吃掉。
            existing = meta.get("citations")
            existing = list(existing) if isinstance(existing, list) else []
            meta["citations"] = [*value, *existing]
        else:
            meta[key] = value
    entry = _kb_trace_entry(fragment)
    if entry is not None:
        trace = meta.get("trace")
        if not isinstance(trace, list):
            trace = []
            meta["trace"] = trace
        trace.append(entry)


def _merge_kb_meta(payload, fragment: dict):
    """非串流出口：狀態一定要騎上去，連下游沒給 meta 的情形也要。"""
    if not isinstance(payload, dict):
        return payload
    meta = payload.get("anila_meta")
    if not isinstance(meta, dict):
        meta = build_default_anila_meta(
            "institutional_kb", detail="institutional kb state",
        )
        payload["anila_meta"] = meta
    _apply_kb_fragment(meta, fragment)
    return payload


async def _sse_with_kb_meta(
    upstream: AsyncIterator[str],
    fragment: dict,
) -> AsyncIterator[str]:
    """串流出口：把狀態蓋進 ``anila.meta`` frame，沒有 frame 就補一個。

    ``proxy_stream`` 今天保證會有一個終端 meta frame（下游沒給就自己合成），
    但**本層不依賴那個保證**：硬規則 1 說的是「狀態明帶在出口上」，那條保證
    哪天被改掉，狀態就會靜默消失。所以這裡自己也留一條合成路徑，並且照
    ``proxy_stream`` 的做法把 ``[DONE]`` 壓到最後才吐——補上去的 meta 必須落在
    ``[DONE]`` 之前，否則客戶端早就收工了。
    """
    import json

    buf = ""
    stamped = False
    pending_done: str | None = None
    async for chunk in upstream:
        buf += chunk
        while "\n\n" in buf:
            block, buf = buf.split("\n\n", 1)
            block_out = block + "\n\n"
            event_name = None
            data_line = None
            for line in block.split("\n"):
                if line.startswith("event:"):
                    event_name = line[6:].strip()
                elif line.startswith("data:"):
                    data_line = line[5:].strip()
            if data_line == "[DONE]":
                pending_done = block_out
                continue
            if event_name == "anila.meta" and data_line:
                try:
                    meta = json.loads(data_line)
                except (json.JSONDecodeError, TypeError):
                    yield block_out
                    continue
                if isinstance(meta, dict):
                    _apply_kb_fragment(meta, fragment)
                    stamped = True
                    yield (
                        "event: anila.meta\n"
                        + "data: "
                        + json.dumps(meta, ensure_ascii=False)
                        + "\n\n"
                    )
                    continue
            yield block_out
    if buf:
        yield buf
    if not stamped:
        meta: dict = {}
        _apply_kb_fragment(meta, fragment)
        yield (
            "event: anila.meta\n"
            + "data: "
            + json.dumps(meta, ensure_ascii=False)
            + "\n\n"
        )
    if pending_done:
        yield pending_done


def _schedule_memory_write(
    *,
    user_id: int,
    conversation_id: int | None,
    user_message: str | None,
    assistant_message: str | None,
    is_encrypted: bool,
) -> None:
    """Fire-and-forget the post-turn memory writer.

    Skips silently if the conversation FK is missing (legacy header
    formats) or either side of the turn is empty.
    """
    if conversation_id is None or not user_message or not assistant_message:
        return
    try:
        asyncio.create_task(
            memory_service.persist_turn(
                user_id=user_id,
                conversation_id=conversation_id,
                user_message=user_message,
                assistant_message=assistant_message,
                is_encrypted=is_encrypted,
            )
        )
    except RuntimeError:
        # No running event loop (shouldn't happen inside FastAPI but
        # be defensive — proxy.py is also imported in test contexts).
        logger.warning("memory_service: no event loop, skipping persist_turn")


async def _tee_stream_capture_assistant(
    upstream: AsyncIterator[str],
    *,
    on_complete: callable,
) -> AsyncIterator[str]:
    """Pass SSE chunks through while collecting assistant text.

    The upstream generator (``proxy_stream``) emits server-sent-event
    blocks; we forward them verbatim and inspect ``data:`` lines to
    pull out the assistant delta text. After the stream finishes,
    ``on_complete`` is called with the assembled assistant string so
    the memory writer can persist the turn.
    """
    import json

    def _choices(chunk: dict) -> list:
        choices = chunk.get("choices")
        if isinstance(choices, list):
            return choices
        choice = chunk.get("choice")
        if isinstance(choice, list):
            return choice
        if isinstance(choice, dict):
            return [choice]
        return []

    def _content(value) -> str:
        if isinstance(value, str):
            return value
        if not isinstance(value, list):
            return ""
        out: list[str] = []
        for item in value:
            if isinstance(item, str):
                out.append(item)
            elif isinstance(item, dict):
                text = item.get("text") or item.get("content")
                if isinstance(text, str):
                    out.append(text)
        return "".join(out)

    parts: list[str] = []
    saw_terminal_error = False
    try:
        async for block in upstream:
            # SSE block format: "event: foo\ndata: {...}\n\n" — extract
            # the data payload and pull assistant-visible text if present.
            for line in block.split("\n"):
                if line.startswith("event:") and line[6:].strip() == "anila.error":
                    # Failed turns must not land in memory; keep streamed
                    # text on the wire for the UI but skip persist.
                    saw_terminal_error = True
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if not payload or payload == "[DONE]":
                    continue
                try:
                    chunk = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                # OpenAI streaming format plus registered agents that send
                # complete assistant messages inside SSE frames.
                for c in _choices(chunk):
                    if not isinstance(c, dict):
                        continue
                    delta = c.get("delta") if isinstance(c.get("delta"), dict) else {}
                    message = (
                        c.get("message") if isinstance(c.get("message"), dict) else {}
                    )
                    txt = _content(delta.get("content")) or _content(
                        message.get("content")
                    )
                    if txt:
                        parts.append(txt)
            yield block
    finally:
        if not saw_terminal_error:
            try:
                on_complete("".join(parts))
            except Exception:
                logger.exception("memory_service: on_complete callback failed")

router = APIRouter(tags=["API 代理"])



def _caller_authorization(request: Request) -> str:
    auth = (request.headers.get("Authorization") or "").strip()
    if auth:
        return auth
    from app.middleware.cookies import ACCESS_COOKIE_NAME
    token = request.cookies.get(ACCESS_COOKIE_NAME)
    if token:
        return f"Bearer {token}"
    raise HTTPException(status_code=401, detail="缺少認證資訊，無法轉送 Router")


def _assert_fixed_router_endpoint(model: ModelRegistry) -> None:
    from urllib.parse import urlparse
    from app.services.auto_seed import PLATFORM_ROUTER_NAME, _platform_router_endpoint
    if model.name != PLATFORM_ROUTER_NAME:
        return
    expected = _platform_router_endpoint().rstrip("/")
    actual = (model.endpoint_url or "").rstrip("/")
    if urlparse(actual).netloc != urlparse(expected).netloc:
        raise HTTPException(
            status_code=403,
            detail="平台入口位址不符合部署設定，拒絕轉送呼叫者憑證",
        )


def _prepare_platform_router_forward(
    db: Session,
    request: Request,
    caller: Caller,
    body: dict,
    model: ModelRegistry,
    conv_id_int: int | None,
) -> tuple[str | None, dict, str, str]:
    """Return (caller_authorization, extra_headers, usage_kind, gateway_api_key)."""
    from app.services.auto_seed import PLATFORM_ROUTER_NAME
    from app.services.router_model_policy import RouterModelPolicyError, resolve_router_model

    if model.name != PLATFORM_ROUTER_NAME:
        return None, {}, "inference", resolve_model_gateway_key(model)
    _assert_fixed_router_endpoint(model)
    requested = body.pop("router_model", None)
    if isinstance(requested, str):
        requested = requested.strip() or None
    conv_model_id = None
    conv_version = 0
    if conv_id_int is not None:
        conv = db.get(Conversation, conv_id_int)
        if conv is not None:
            conv_model_id = getattr(conv, "router_model_id", None)
            conv_version = int(getattr(conv, "router_selection_version", 0) or 0)
    try:
        selection = resolve_router_model(
            db,
            caller.user,
            requested_name=requested,
            conversation_model_id=conv_model_id,
            selection_version=conv_version,
            api_key_id=caller.api_key_id,
        )
    except RouterModelPolicyError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    if conv_id_int is not None:
        conv = db.get(Conversation, conv_id_int)
        if conv is not None and getattr(conv, "router_model_id", None) is None:
            conv.router_model_id = selection.model_id
            conv.router_selection_version = max(conv_version, 1)
            db.commit()
    return (
        _caller_authorization(request),
        {"X-ANILA-Router-Model": selection.model_name},
        "router_transport",
        "",
    )


def _resolve_model(
    db: Session,
    caller: Caller,
    model_name: str,
    request: Request | None = None,
) -> ModelRegistry:
    """Resolve model name to registry entry and check caller permissions."""
    model = db.query(ModelRegistry).filter(ModelRegistry.name == model_name).first()
    if not model:
        raise HTTPException(status_code=404, detail=f"模型 '{model_name}' 未註冊")
    if not model.is_active:
        raise HTTPException(status_code=400, detail=f"模型 '{model_name}' 已停用")
    # Route marks a Router-originated *base LLM* call. The platform entry
    # anila-router also carries Route for KB forced-research; that is not
    # a base-LLM selection and must not be eligibility-checked as one.
    from app.services.router_model_policy import PLATFORM_ROUTER_NAME, user_can_use_router_model
    router_base_call = bool(
        request is not None
        and request.headers.get(_ROUTE_HEADER)
        and model.name != PLATFORM_ROUTER_NAME
    )
    if router_base_call and not user_can_use_router_model(db, caller.user, model):
        raise HTTPException(
            status_code=403,
            detail=f"無權使用 Router 基礎模型 '{model_name}'",
        )
    if not check_model_permission(
        db, user=caller.user, api_key_id=caller.api_key_id, model_id=model.id
    ):
        raise HTTPException(
            status_code=403,
            detail=f"無權使用模型 '{model_name}'",
        )
    return model


def _resolve_agent(db: Session, caller: Caller, agent_name: str) -> Agent | None:
    """Return the Agent if agent_name matches an approved agent, else None."""
    agent = db.query(Agent).filter(Agent.name == agent_name).first()
    if agent is None:
        return None
    if agent.approval_status != "approved":
        if agent.unavailable_reason:
            raise HTTPException(status_code=403, detail=AGENT_TEMPORARILY_UNAVAILABLE)
        return None
    if not check_agent_permission(
        db, user=caller.user, api_key_id=caller.api_key_id, agent_id=agent.id
    ):
        raise HTTPException(
            status_code=403,
            detail=f"無權呼叫 agent '{agent_name}'",
        )
    if agent.unavailable_reason:
        raise HTTPException(status_code=403, detail=AGENT_TEMPORARILY_UNAVAILABLE)
    return agent


def _target_allows_memory(agent: Agent | None) -> bool:
    """Allow memory unless the outbound target is a registered agent.

    A resolved ``Agent`` is an outbound registered-agent target and must never
    receive user memory. Agent-ness is structural; mutable model registry
    properties are deliberately not part of this privacy decision.
    """
    return agent is None


@router.get("/v1/agents")
def list_available_agents(
    caller: Caller = Depends(get_caller),
    db: Session = Depends(get_db),
):
    """Data-plane endpoint: return approved agents available to the caller.

    Used by RemoteAgentRegistry in the Router to discover agents.
    Response mirrors OpenAI /v1/models shape.
    """
    user = caller.user

    # admin + owner 都看得到所有 approved agent;一般 user 必須有
    # UserAgentPermission 顯式授權才看得到。先前漏掉 owner,讓 owner
    # 在 ANILA UI 看到的 agent 清單可能跟 CSP UI (受同樣 bug 影響) 對不上。
    # 底層模型下線的 agent 不進 Router 清單，與核准、健康狀態無關。
    available = (
        Agent.approval_status == "approved",
        Agent.unavailable_reason.is_(None),
    )
    if is_admin_tier(user):
        agents = db.query(Agent).filter(*available).all()
    else:
        agents = (
            db.query(Agent)
            .join(UserAgentPermission, UserAgentPermission.agent_id == Agent.id)
            .filter(
                UserAgentPermission.user_id == user.id,
                *available,
            )
            .all()
        )

    # Encryption is an agent-level policy only. Base models (LLMs) do NOT carry
    # a requires_encryption flag — classification is decided per-agent so the
    # same LLM can serve both classified and non-classified agents.
    data = [
        {
            "id": a.name,
            "object": "agent",
            "name": a.name,
            "description_for_router": a.description_for_router,
            # Same visibility predicate as /api/models. Router dispatch does
            # not call this URL (it proxies through CSP); the SPA must not
            # receive a docker-internal agent address just because the user
            # has UserAgentPermission.
            "endpoint_url": _endpoint_display_for(db, user, a.endpoint_url),
            "capabilities": a.capabilities or {},
            "input_schema": a.input_schema,
            "requires_encryption": bool(getattr(a, "requires_encryption", False)),
        }
        for a in agents
    ]
    return JSONResponse({"object": "list", "data": data})


@router.get("/v1/models")
async def list_models_openai(
    caller: Caller = Depends(get_caller),
    db: Session = Depends(get_db),
):
    """OpenAI-compatible model discovery.

    Mirrors ``GET https://api.openai.com/v1/models`` so off-the-shelf clients
    (OpenWebUI, LangChain, official openai-python, LlamaIndex) can point at
    ``https://<host>/v1`` with an API key and auto-discover usable models —
    without our custom ``/api/models`` shape. Returns only models the caller
    is permitted to use via ``check_model_permission`` (same gate as
    ``/v1/chat/completions``), so the discovery list cannot be used to widen
    a key's effective scope.
    """
    rows = (
        db.query(ModelRegistry)
        .filter(ModelRegistry.is_active.is_(True))
        .order_by(ModelRegistry.id)
        .all()
    )
    visible = [
        m for m in rows
        if check_model_permission(
            db, user=caller.user, api_key_id=caller.api_key_id, model_id=m.id
        )
    ]
    return JSONResponse({
        "object": "list",
        "data": [
            {
                "id": m.name,
                "object": "model",
                "created": int(m.created_at.timestamp()) if m.created_at else 0,
                "owned_by": "anila",
            }
            for m in visible
        ],
    })


async def _complete_dispatched_model(
    request: Request,
    call: DispatchModelCall,
    db: Session,
):
    """Agent 帶派工 JWT 呼叫自己核准的底層模型。不查提問者的模型授權。"""
    body = await request.json()
    model_name = body.get("model") if isinstance(body, dict) else None
    if not model_name:
        raise HTTPException(status_code=400, detail="缺少 model 參數")
    base = call.agent.base_model
    if base is None and call.agent.base_model_id is not None:
        base = db.get(ModelRegistry, call.agent.base_model_id)
    approved_name = base.name if base is not None else "（未指定）"
    if base is None or model_name != base.name or not base.is_active:
        if base is not None and model_name == base.name and not base.is_active:
            raise HTTPException(status_code=403, detail=AGENT_TEMPORARILY_UNAVAILABLE)
        raise HTTPException(
            status_code=403,
            detail=f"此 agent 只核准使用 {approved_name}",
        )
    # 分類來自派工 JWT 簽過的任務或對話，不看 X-ANILA-Task-Id 或 body。
    enforce_dispatched_model_ceiling(db, call, base)
    stream = bool(body.get("stream", False))
    user = call.user
    tuning = resolve_proxy_tuning(db)
    identity = downstream_identity(user)
    chat_path = (
        "/v2/chat/completions" if base.api_version == "v2" else "/v1/chat/completions"
    )
    if stream:
        upstream = proxy_stream(
            target_url=join_upstream_path(base.endpoint_url, chat_path),
            api_key_id=None,
            user_id=user.id,
            department_id=call.department_id,
            usage_model_id=base.id,
            request_body=body,
            user_identity=identity,
            model_name=base.name,
            caller_agent_id=call.agent.id,
            legacy_runtime_call=True,
            gateway_api_key=resolve_model_gateway_key(base),
            model_name_snapshot=base.name,
            tuning=tuning,
            model=base,
        )
        return StreamingResponse(
            upstream,
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    return await proxy_request(
        model=base,
        api_key_id=None,
        user_id=user.id,
        department_id=call.department_id,
        request_body=body,
        endpoint_path=chat_path,
        user_identity=identity,
        caller_agent_id=call.agent.id,
        legacy_runtime_call=True,
        tuning=tuning,
        model_name_snapshot=base.name,
    )


@router.post("/v1/chat/completions")
async def chat_completions(
    request: Request,
    caller: Caller | DispatchModelCall = Depends(resolve_chat_caller),
    db: Session = Depends(get_db),
):
    if isinstance(caller, DispatchModelCall):
        return await _complete_dispatched_model(request, caller, db)
    body = await request.json()
    side, client_passages = _take_external_sidechannel(body)
    model_name = body.get("model")
    if not model_name:
        raise HTTPException(status_code=400, detail="缺少 model 參數")

    stream: bool = body.get("stream", False)
    user = caller.user
    department_id = user.department_id
    user_email = user.email
    # 員編 forwarded as the downstream wire identity (None for non-card
    # accounts → identity header omitted, never forged; the request still
    # proceeds). user.id (PK) is still used for usage rows.
    user_identity = downstream_identity(user)

    # Audit fields from optional client headers
    conversation_id: str | None = request.headers.get("X-ANILA-Conversation-Id")
    trace_id: str | None = request.headers.get("X-ANILA-Trace-Id")

    # Resolve the outbound target before any memory retrieval. Memory is a
    # destination policy, not a caller-attribution policy.
    agent = _resolve_agent(db, caller, model_name)
    resolved_model: ModelRegistry | None = None
    if agent is None:
        resolved_model = _resolve_model(db, caller, model_name, request)

    # ── Memory: read path (sync, ~150ms) ─────────────────────────────────────
    # The conv_id (if numeric) is excluded from RAG because the active
    # conversation's history is already in the messages array — re-injecting
    # would just waste prompt tokens.
    conv_id_int = _coerce_conversation_id(conversation_id)
    if conv_id_int is not None:
        _require_conversation_access(db, caller, conv_id_int)
    memory_read = None
    if _target_allows_memory(agent):
        memory_read = await _inject_memory(
            db,
            user.id,
            body,
            exclude_conversation_id=conv_id_int,
            side=side,
        )
    # P1.5: whole-document attachment injection (after memory). Failures are
    # recorded on attach_inject for anila_meta.trace; chat still proceeds.
    attach_inject = _inject_attachments(
        db, conv_id_int, body, model_name, side,
    )
    # Capture the user message text NOW (after memory / attachment injection
    # but before any downstream mutation) so the post-turn writer has the
    # exact string the user sent.
    # ⚠ 這一次抽取同時是**檢索的 query**（硬規則 6：兩者是同一個定義）。共用
    # 一次呼叫而不是各抽一次，是為了讓「送去檢索的字」與「記進記憶的字」不可能
    # 漂開——漂開時兩邊都不會報錯。
    captured_user_text = _extract_latest_user_message(
        # _inject_memory may have altered the messages list; use the
        # last user message which is unchanged across that path.
        body
    )
    # 院內規章檢索（Q39）：header 在就檢索並注入；不在就一次都不查。狀態在下面
    # 四個出口上明帶。詳見 ``_route_marked`` 上方那一段。
    kb_result = await _retrieve_institutional_kb(
        db,
        user,
        marked=_route_marked(request.headers),
        query=captured_user_text,
    )
    # 優先順序先附上，規章區塊（含語言提醒）才會留在系統訊息最後一行。
    _ensure_priority_rule(body)
    kb_block = _build_kb_block(kb_result)
    if kb_block:
        _inject_kb_block(body, kb_block)
    _inject_kb_passages(body, kb_result, side)
    _inject_client_passages(body, client_passages, side)
    kb_meta = _kb_meta_fragment(kb_result)
    # P3: latch the consuming conversation into classified state when
    # memory recall pulled at least one encrypted chunk. One-shot — once
    # set, never cleared by a later non-encrypted turn (would otherwise
    # let a single clean turn launder the classification). Only writes
    # when we actually have a conversation FK and the row exists.
    if (
        conv_id_int is not None
        and memory_read
        and memory_read.encryption_inherited
    ):
        try:
            _latch_inherited_classification(db, conv_id_int)
        except Exception:
            db.rollback()
            logger.exception(
                "memory_service: classification latch failed conv_id=%s",
                conv_id_int,
            )
    # 輸入當下就能寫的可疑紀錄。回聲要等模型輸出，稍後再補。
    _audit_injection(
        db,
        user,
        [item for item in side.findings if item.rule_id != "protocol_echo"],
    )
    # Try agent first, fallback to the model registry row already resolved
    # above, before the memory policy was evaluated.
    if agent:
        agent_requires_encryption = bool(getattr(agent, "requires_encryption", False))
        # P3 hook: if any retrieved memory chunk was encrypted at write
        # time, inherit that classification onto this turn even if the
        # current agent isn't itself encrypted (Bell-LaPadula no-write-
        # down). For P1 we just OR them — UI / latch wiring lands in P3.
        if memory_read and memory_read.encryption_inherited:
            agent_requires_encryption = True
        # Persist classified state to the conversation row so it survives
        # hard refresh. ROUTER routing to an encrypted downstream agent
        # is the canonical case: conversation.agent_id stays NULL (router)
        # but the row's classified flag must record the encrypted turn so
        # the next GET /api/conversations latches the UI back into
        # encrypted mode.
        # Slice 3b: latch the agent's OWN four-level classification onto the
        # conversation (reason=agent_policy) through the one-way core. Uses
        # the agent's default level, floored at RESTRICTED(密) when
        # requires_encryption — old rank-2 floor preserved (SYSTEM-MAP §8).
        # The OR'd ``agent_requires_encryption`` still drives the wire meta
        # below; the memory-inheritance contribution is latched separately.
        if conv_id_int is not None:
            agent_level = _agent_policy_level(agent)
            # Not an OE-4 outbound gate: classification latch onto the
            # conversation row when the agent policy level is above floor.
            if agent_level > ClassificationLevel.UNCLASSIFIED:
                try:
                    _latch_agent_classification(
                        db, conv_id_int, agent_level.to_storage()
                    )
                except Exception:
                    db.rollback()
                    logger.exception(
                        "agent classification latch failed conv_id=%s",
                        conv_id_int,
                    )
        # Slice 2b-C: optional X-ANILA-Task-Id — validate access, record
        # the task.run PolicyDecision and open a TaskRun BEFORE dispatch.
        # None → legacy traffic (usage row marked legacy_runtime_call).
        task_ctx = begin_task_run(
            db,
            caller=caller,
            request_headers=request.headers,
            dispatch_target="agent",
            resource_type="agent",
            resource_id=str(agent.id),
        )
        # Slice 3b: propagate the conversation's effective level onto the
        # linked task (reason=source_selected) so later ceiling checks see it.
        if task_ctx is not None and conv_id_int is not None:
            try:
                _propagate_conversation_level_to_task(
                    db, task_ctx.task_id, conv_id_int
                )
            except Exception:
                db.rollback()
                logger.exception(
                    "task classification propagation failed task_id=%s",
                    task_ctx.task_id,
                )
        enforce_agent_ceiling(
            db,
            agent=agent,
            caller=caller,
            task_ctx=task_ctx,
            conv_id_int=conv_id_int,
        )
        # Bind session ownership only after permission + ceiling gates so a
        # refused caller cannot claim anila_session_id (P2.4 H3).
        raw_sid = body.get("anila_session_id") or body.get("session_id")
        if isinstance(raw_sid, str) and raw_sid.strip():
            from app.services.agent_session_owner_service import (
                ensure_agent_session_owner,
            )
            ensure_agent_session_owner(
                db, session_id=raw_sid, owner_user_id=user.id,
            )
        # Usage attribution: inbound X-ANILA-Trace-Id wins (legacy
        # contract); a task-linked call without one falls back to the
        # task row's trace id (doc 04 AC10 歸戶).
        usage_trace_id = trace_id or (task_ctx.trace_id if task_ctx else None)
        # 逾時／重試在 handler 期解一次。⚠ 串流那條路是 async generator，
        # **在 handler 回傳之後才被抽乾**，那時 request scope 的 session 可能
        # 已經關了 —— 所以值要在這裡凍結，不能讓 proxy 那一層自己去查。
        tuning = resolve_proxy_tuning(db)
        if stream:
            upstream = proxy_stream(
                target_url=join_upstream_path(
                    agent.endpoint_url, "/v1/chat/completions"
                ),
                api_key_id=caller.api_key_id,
                user_id=user.id,
                department_id=department_id,
                usage_model_id=agent.id,
                request_body=body,
                user_email=user_email,
                user_identity=user_identity,
                model_name=agent.name,
                conversation_id=conversation_id,
                trace_id=usage_trace_id,
                requires_encryption=agent_requires_encryption,
                # target_agent_id 決定簽派工 JWT，不把模型閘道金鑰送去 agent。
                # 這一跳不記 token；用量在 agent 回來打底層模型時入帳。
                target_agent_id=agent.id,
                caller_agent_id=agent.id,
                record_usage=False,
                # Slice 2b-C — task linkage (headers + usage + run finish).
                task_id=task_ctx.task_id if task_ctx else None,
                task_trace_id=task_ctx.trace_id if task_ctx else None,
                task_run_id=task_ctx.task_run_id if task_ctx else None,
                legacy_runtime_call=task_ctx is None,
                endpoint_display=_endpoint_display_for(
                    db, user, agent.endpoint_url
                ),
                tuning=tuning,
            )
            # Tee the SSE so we can capture the final assistant text and
            # schedule the memory writer once the stream drains.
            guarded = _guard_sse_stream(upstream, side, user)
            teed = _tee_stream_capture_assistant(
                guarded,
                on_complete=lambda assistant_text: _schedule_memory_write(
                    user_id=user.id,
                    conversation_id=conv_id_int,
                    user_message=captured_user_text,
                    assistant_message=assistant_text,
                    is_encrypted=agent_requires_encryption,
                ),
            )
            # Same attachment trace entry as non-streaming anila_meta.
            traced = _sse_with_attachment_trace(teed, attach_inject)
            # 出口 1/4（agent SSE）：kb 包在最外層，狀態是最後一個寫入者。
            traced = _sse_with_kb_meta(traced, kb_meta)
            traced = _sse_with_injection_notice(traced, side)
            return StreamingResponse(
                traced,
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )
        # Non-streaming agent call — use proxy_request with a synthetic ModelRegistry-like obj
        # by forwarding to the agent endpoint directly
        import httpx
        from fastapi import HTTPException as _HTTPException
        target = join_upstream_path(agent.endpoint_url, "/v1/chat/completions")
        from anila_core.security import ENDPOINT_KIND_AGENT
        from app.services.proxy_service import (
            _aggregate_sse_to_chat_completion,
            build_agent_headers,
            _guard_outbound,
        )
        _guard_outbound(
            target, endpoint_kind=ENDPOINT_KIND_AGENT
        )  # call-time SSRF re-validation (TOCTOU defense) — FINAL url
        # P2.1: mint per-dispatch signed identity JWT (no csk- / plaintext
        # user headers).
        headers = build_agent_headers(
            user_id=user.id,
            department=department_id,
            agent_id=agent.id,
            # Slice 2b-C (doc 05 §4): task/trace ids ride on agent dispatch.
            task_id=task_ctx.task_id if task_ctx else None,
            trace_id=task_ctx.trace_id if task_ctx else None,
            conversation_id=conversation_id,
        )
        started_at = time.time()
        try:
            async with httpx.AsyncClient(timeout=tuning.llm_timeout) as client:
                resp = await client.post(target, json=body, headers=headers)
                resp.raise_for_status()
                # SSE-only agents (e.g. asrd) ignore ``stream: false`` and
                # respond with event-stream regardless. Aggregate in that
                # case so the caller still gets JSON.
                ct = resp.headers.get("content-type", "")
                preview = resp.text[:8].lstrip()
                if "text/event-stream" in ct or preview.startswith("data:"):
                    payload = _aggregate_sse_to_chat_completion(resp.text, agent.name)
                else:
                    payload = resp.json()
                existing_meta = payload.get("anila_meta")
                if not existing_meta:
                    # Caller-facing detail names the agent, never the
                    # upstream address (twin of the model-proxy fix).
                    _ep = _endpoint_display_for(
                        db, user, agent.endpoint_url
                    )
                    payload["anila_meta"] = build_default_anila_meta(
                        agent.name,
                        detail=f"CSP proxy -> {agent.name}（{_ep}）",
                        latency_ms=int((time.time() - started_at) * 1000),
                        classified=agent_requires_encryption,
                    )
                elif agent_requires_encryption and isinstance(existing_meta, dict):
                    existing_meta["classified"] = True
                _annotate_agent_reply_payload(payload, agent.name)
                # 派工這一跳不打模型，不寫 token_usage。
                before_echo = len(side.findings)
                _apply_output_guard(payload, side)
                _audit_injection(db, user, side.findings[before_echo:])
                # Memory write (non-streaming agent path)
                assistant_text = _extract_assistant_text(payload)
                _schedule_memory_write(
                    user_id=user.id,
                    conversation_id=conv_id_int,
                    user_message=captured_user_text,
                    assistant_message=assistant_text,
                    is_encrypted=agent_requires_encryption,
                )
                # Slice 2b-C: run finished.
                if task_ctx is not None:
                    finalize_task_run(task_ctx.task_run_id, "completed")
                # 出口 2/4（agent 非串流）。
                return _merge_kb_meta(
                    _merge_attachment_trace(payload, attach_inject), kb_meta,
                )
        except httpx.HTTPStatusError as e:
            logger.error(
                "Agent %s 上游 HTTP 錯誤 url=%s: %s",
                agent.name,
                target,
                e,
            )
            if task_ctx is not None:
                finalize_task_run(
                    task_ctx.task_run_id,
                    "failed",
                    error={
                        "code": f"http_{e.response.status_code}",
                        "message": f"Agent「{agent.name}」上游回應錯誤",
                    },
                )
            raise _HTTPException(
                status_code=e.response.status_code,
                detail=(
                    f"Agent「{agent.name}」上游回應錯誤"
                    f"（HTTP {e.response.status_code}）"
                ),
            )
        except Exception as e:
            # Fixed caller-facing text; exception may embed the URL.
            logger.error(
                "Agent %s 呼叫失敗 url=%s: %s",
                agent.name,
                target,
                e,
                exc_info=True,
            )
            if task_ctx is not None:
                finalize_task_run(
                    task_ctx.task_run_id,
                    "failed",
                    error={
                        "code": "agent_call_failed",
                        "message": f"Agent「{agent.name}」呼叫失敗",
                    },
                )
            raise _HTTPException(
                status_code=502,
                detail=f"Agent「{agent.name}」呼叫失敗",
            )

    if resolved_model is None:
        # This is unreachable after the positive target resolution above, but
        # keep the outbound path fail-closed if that invariant changes.
        raise HTTPException(status_code=404, detail="模型目標無法解析")
    model = resolved_model
    # Direct LLM calls (not through an agent) do NOT trigger CSP-side classified
    # latch. Encryption is agent-level policy; the same LLM can back both
    # classified and non-classified agents. Downstream-reported classified=True
    # still latches via proxy_service's normal meta merge.
    # Inheritance: if memory injected encrypted material, latch this
    # direct-LLM call as encrypted too (matches agent path semantics).
    inherited_encryption = bool(memory_read and memory_read.encryption_inherited)
    # Slice 2b-C: optional X-ANILA-Task-Id — same wiring as the agent
    # branch, dispatch_target/resource_type = "model". Outbound headers to
    # the model gateway stay minimal (doc 04 §3/AC5) — the task ids below
    # only reach the usage row + run lifecycle, never the gateway headers.
    task_ctx = begin_task_run(
        db,
        caller=caller,
        request_headers=request.headers,
        dispatch_target="model",
        resource_type="model",
        resource_id=str(model.id),
    )
    # Slice 3b: propagate the conversation's effective level onto the linked
    # task (reason=source_selected). On the direct-model path the conversation
    # may still be classified via memory inheritance (latched above).
    if task_ctx is not None and conv_id_int is not None:
        try:
            _propagate_conversation_level_to_task(
                db, task_ctx.task_id, conv_id_int
            )
        except Exception:
            db.rollback()
            logger.exception(
                "task classification propagation failed task_id=%s",
                task_ctx.task_id,
            )
    # Slice 6a (doc 04 §5/§8) + OE-4/G4: classification ceiling check BEFORE
    # the outbound model call. Covers task-linked AND legacy traffic. A
    # violation raises 403 + records a model.invoke deny row and never
    # dispatches upstream; a pass records an allow row when task-linked OR
    # level ≥ 營業秘密.
    enforce_model_ceiling(
        db,
        model=model,
        caller=caller,
        task_ctx=task_ctx,
        conv_id_int=conv_id_int,
    )
    usage_trace_id = trace_id or (task_ctx.trace_id if task_ctx else None)
    # 同上：串流在 handler 回傳之後才抽乾，值必須在這裡解。
    tuning = resolve_proxy_tuning(db)
    caller_authorization, router_extra_headers, usage_kind, gateway_api_key = (
        _prepare_platform_router_forward(
            db, request, caller, body, model, conv_id_int
        )
    )
    # 路由器才會執行協定行。原文語料跟著這一次轉送走，模型本體看不到這個欄位。
    from app.services.auto_seed import PLATFORM_ROUTER_NAME
    if model.name == PLATFORM_ROUTER_NAME and side.protocol_lines:
        body["anila_protocol_corpus"] = list(side.protocol_lines)[:50]
    if stream:
        chat_path = (
            "/v2/chat/completions"
            if model.api_version == "v2"
            else "/v1/chat/completions"
        )
        target_url = join_upstream_path(model.endpoint_url, chat_path)
        upstream = proxy_stream(
            target_url=target_url,
            api_key_id=caller.api_key_id,
            user_id=user.id,
            department_id=department_id,
            usage_model_id=model.id,
            request_body=body,
            user_email=user_email,
            user_identity=user_identity,
            model_name=model.name,
            conversation_id=conversation_id,
            trace_id=usage_trace_id,
            requires_encryption=inherited_encryption,
            task_id=task_ctx.task_id if task_ctx else None,
            task_trace_id=task_ctx.trace_id if task_ctx else None,
            task_run_id=task_ctx.task_run_id if task_ctx else None,
            legacy_runtime_call=task_ctx is None,
            gateway_api_key=gateway_api_key,
            caller_authorization=caller_authorization,
            extra_headers=router_extra_headers,
            usage_kind=usage_kind,
            model_name_snapshot=model.name,
            endpoint_display=_endpoint_display_for(
                db,
                user,
                model.endpoint_url,
                is_internal=bool(getattr(model, "is_internal", False)),
            ),
            tuning=tuning,
            model=model,
        )
        guarded = _guard_sse_stream(upstream, side, user)
        teed = _tee_stream_capture_assistant(
            guarded,
            on_complete=lambda assistant_text: _schedule_memory_write(
                user_id=user.id,
                conversation_id=conv_id_int,
                user_message=captured_user_text,
                assistant_message=assistant_text,
                is_encrypted=inherited_encryption,
            ),
        )
        # Same attachment trace entry as non-streaming anila_meta.
        traced = _sse_with_attachment_trace(teed, attach_inject)
        # 出口 3/4（model SSE）——使用者實際踩到的那一條。
        traced = _sse_with_kb_meta(traced, kb_meta)
        traced = _sse_with_injection_notice(traced, side)
        return StreamingResponse(
            traced,
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    payload = await proxy_request(
        model=model,
        api_key_id=caller.api_key_id,
        user_id=user.id,
        user_identity=user_identity,
        department_id=department_id,
        request_body=body,
        endpoint_path="/v1/chat/completions",
        conversation_id=conversation_id,
        trace_id=usage_trace_id,
        requires_encryption=inherited_encryption,
        task_id=task_ctx.task_id if task_ctx else None,
        task_trace_id=task_ctx.trace_id if task_ctx else None,
        task_run_id=task_ctx.task_run_id if task_ctx else None,
        legacy_runtime_call=task_ctx is None,
        endpoint_display=_endpoint_display_for(
            db,
            user,
            model.endpoint_url,
            is_internal=bool(getattr(model, "is_internal", False)),
        ),
        tuning=tuning,
        usage_source=request.headers.get("X-ANILA-Request-Source"),
        caller_authorization=caller_authorization,
        extra_headers=router_extra_headers,
        usage_kind=usage_kind,
        model_name_snapshot=model.name,
    )
    before_echo = len(side.findings)
    _apply_output_guard(payload, side)
    _audit_injection(db, user, side.findings[before_echo:])
    assistant_text = _extract_assistant_text(payload)
    _schedule_memory_write(
        user_id=user.id,
        conversation_id=conv_id_int,
        user_message=captured_user_text,
        assistant_message=assistant_text,
        is_encrypted=inherited_encryption,
    )
    # 出口 4/4（model 非串流）。
    return _merge_kb_meta(
        _merge_attachment_trace(payload, attach_inject), kb_meta,
    )


@router.post("/v1/agents/{agent_name}/sessions/{session_id}/answer")
async def resume_agent_session(
    agent_name: str,
    session_id: str,
    request: Request,
    caller: Caller = Depends(get_caller),
    db: Session = Depends(get_db),
):
    """Sprint 13 PR A2 — Router-driven resume proxy for paused agent runs.

    The Router persists ``session_id → agent_id`` per dispatch and uses
    this endpoint to forward the user's answer to the agent that owns
    the paused run. Identity injection + per-agent service-token swap
    use the same machinery as agent ``chat_completions`` so audit and
    token attribution are consistent.

    Body shape mirrors the agent's ``/sessions/{id}/answer``::

        { "interrupt_id": str,
          "answer": str | dict,
          "max_turns": int (optional),
          "model": str (optional),
          "system_prompt": str (optional) }

    Response: SSE stream of the resumed turn, passed through verbatim.
    """
    body = await request.json()
    agent = _resolve_agent(db, caller, agent_name)
    if agent is None:
        raise HTTPException(
            status_code=404, detail=f"Agent '{agent_name}' 未註冊或未審核",
        )

    # Same ownership predicate as agent chat with anila_session_id —
    # resume without a prior bind (or under another caller) → 404.
    from app.services.agent_session_owner_service import ensure_agent_session_owner
    ensure_agent_session_owner(
        db,
        session_id=session_id,
        owner_user_id=caller.user.id,
        missing_is_error=True,
    )

    user = caller.user
    target = (
        f"{agent.endpoint_url.rstrip('/')}/sessions/{session_id}/answer"
    )
    from anila_core.security import ENDPOINT_KIND_AGENT
    from app.services.proxy_service import build_agent_headers, _guard_outbound
    _guard_outbound(
        target, endpoint_kind=ENDPOINT_KIND_AGENT
    )  # call-time SSRF re-validation (TOCTOU defense)
    headers = build_agent_headers(
        user_id=user.id,
        department=user.department_id,
        agent_id=agent.id,
    )

    import httpx

    # ⚠ 在 closure **外面**解析：``_passthrough_stream`` 是 StreamingResponse 的
    # generator，執行時 handler 已經回傳，session 不保證還活著。
    llm_timeout = float(get_setting(db, "proxy.llm_timeout"))

    async def _passthrough_stream():
        try:
            async with httpx.AsyncClient(timeout=llm_timeout) as client:
                async with client.stream(
                    "POST", target, json=body, headers=headers,
                ) as resp:
                    if resp.status_code >= 400:
                        err = await resp.aread()
                        # Surface the upstream error inline so the
                        # caller's SSE framing stays valid.
                        msg = err[:300].decode("utf-8", errors="replace")
                        yield (
                            f"event: error\n"
                            f"data: {{\"status\": {resp.status_code}, "
                            f"\"detail\": {msg!r}}}\n\n"
                        )
                        return
                    async for raw_line in resp.aiter_lines():
                        if raw_line == "":
                            yield "\n"
                        else:
                            yield raw_line + "\n"
        except httpx.RequestError as exc:
            yield (
                f"event: error\n"
                f"data: {{\"status\": 502, "
                f"\"detail\": \"agent connection error: "
                f"{type(exc).__name__}\"}}\n\n"
            )

    return StreamingResponse(
        _passthrough_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _pop_input_type(body: dict) -> str:
    """Read and remove the ``input_type`` extension from an embeddings body.

    Triton embedders put queries and documents in **different input tensors**;
    an out-of-process caller (agent SDK, ingestion worker, anything holding an
    API key) has no other way to say which side it is on. Without this the
    public ``/v1`` and ``/v2`` surfaces were hardcoded to ``document`` and
    every agent RAG query was embedded as a document — no error, just worse
    ranking. Naming follows the Cohere/Voyage/Jina convention so it reads as
    an ordinary vendor extension rather than an ANILA-only word.

    Default stays ``document``: that is what every existing caller means, so
    silence keeps today's behaviour. Popped rather than read so the key never
    rides on to an OpenAI-compatible upstream that would reject an unknown
    field.
    """
    raw = body.pop("input_type", None)
    if raw is None or raw == "":
        return "document"
    if raw in ("query", "document"):
        return raw
    raise HTTPException(
        status_code=400,
        detail="input_type 僅接受 query 或 document",
    )


@router.post("/v1/embeddings")
async def embeddings_v1(
    request: Request,
    caller: Caller = Depends(get_caller),
    db: Session = Depends(get_db),
):
    body = await request.json()
    model_name = body.get("model")
    if not model_name:
        raise HTTPException(status_code=400, detail="缺少 model 參數")

    input_type = _pop_input_type(body)

    model = _resolve_model(db, caller, model_name, request)
    return await proxy_request(
        model=model,
        api_key_id=caller.api_key_id,
        user_id=caller.user.id,
        user_identity=downstream_identity(caller.user),
        department_id=caller.user.department_id,
        request_body=body,
        endpoint_path="/v1/embeddings",
        endpoint_display=_endpoint_display_for(
            db,
            caller.user,
            model.endpoint_url,
            is_internal=bool(getattr(model, "is_internal", False)),
        ),
        # Public OpenAI-compat surface (incl. ingestion-worker) defaults to
        # documents; ``input_type: "query"`` opts a caller onto the query side.
        embedding_input_role=input_type,
        tuning=resolve_proxy_tuning(db),
    )


@router.post("/v2/embeddings")
async def embeddings_v2(
    request: Request,
    caller: Caller = Depends(get_caller),
    db: Session = Depends(get_db),
):
    body = await request.json()
    model_name = body.get("model")
    if not model_name:
        raise HTTPException(status_code=400, detail="缺少 model 參數")

    input_type = _pop_input_type(body)

    model = _resolve_model(db, caller, model_name, request)
    return await proxy_request(
        model=model,
        api_key_id=caller.api_key_id,
        user_id=caller.user.id,
        user_identity=downstream_identity(caller.user),
        department_id=caller.user.department_id,
        request_body=body,
        endpoint_path="/v2/embeddings",
        endpoint_display=_endpoint_display_for(
            db,
            caller.user,
            model.endpoint_url,
            is_internal=bool(getattr(model, "is_internal", False)),
        ),
        embedding_input_role=input_type,
        tuning=resolve_proxy_tuning(db),
    )


_IMAGE_UNREADY = "生圖模型尚未就緒，簡報將不配生成圖片"
_IMAGE_PROMPT_MAX = 240
IMAGE_UPSTREAM_MAX_BYTES = 8 * 1024 * 1024
_IMAGE_PIXEL_CAP = 1792 * 1024
_IMAGE_SIZES = frozenset({
    "512x512",
    "768x768",
    "1024x1024",
    "1024x768",
    "768x1024",
    "1280x720",
    "1792x1024",
})
_KB_LEAK_RE = re.compile(
    r"(來源\s*[:：]|第\s*\d+\s*條|\(參\s*\[|（參\s*\[|chunk\b|leaf-\d+)",
    re.IGNORECASE,
)


def image_size_allowed(size: str) -> bool:
    """只允許白名單尺寸，而且寬乘高不得超過像素上限。"""
    if size not in _IMAGE_SIZES:
        return False
    width_text, height_text = size.split("x", 1)
    return int(width_text) * int(height_text) <= _IMAGE_PIXEL_CAP


def prepare_image_prompt(prompt: str) -> str:
    """短的抽象視覺描述。知識庫原文直接拒絕；內部細節先濾掉再送。"""
    from anila_core.api.router_prompts import redact_internal_details

    text = " ".join(prompt.split())
    if not text or len(text) > _IMAGE_PROMPT_MAX or _KB_LEAK_RE.search(text):
        raise HTTPException(status_code=400, detail="生圖提示必須是短的抽象視覺描述")
    cleaned = " ".join(redact_internal_details(text).split())
    if not cleaned or len(cleaned) > _IMAGE_PROMPT_MAX or _KB_LEAK_RE.search(cleaned):
        raise HTTPException(status_code=400, detail="生圖提示必須是短的抽象視覺描述")
    return cleaned


@router.post("/v1/images/generations")
async def images_generations(
    request: Request,
    caller: Caller = Depends(get_caller),
    db: Session = Depends(get_db),
):
    """把生圖請求轉給「生圖模型」角色。位址與金鑰留在 CSP。

    沒設回 404、已停用或健康狀態不是 healthy 回 409，都不打上游。
    呼叫端帶來的 model／網址一律不用，只轉角色目前那一顆。
    """
    try:
        body = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="請求必須是 JSON") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="請求必須是 JSON 物件")
    prompt = body.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise HTTPException(status_code=400, detail="缺少 prompt")

    resolved = resolve_role(db, "image_generation")
    if resolved.status == "unset":
        raise HTTPException(status_code=404, detail=resolved.message)
    if resolved.status != "ok" or resolved.model is None:
        raise HTTPException(status_code=409, detail=resolved.message)
    model = resolved.model
    health = normalize_health_status(
        model.health_status, is_active=bool(model.is_active)
    )
    if health != HEALTH_HEALTHY:
        raise HTTPException(status_code=409, detail=_IMAGE_UNREADY)
    if not check_model_permission(
        db,
        user=caller.user,
        api_key_id=caller.api_key_id,
        model_id=model.id,
    ):
        raise HTTPException(status_code=403, detail=f"無權使用模型 '{model.name}'")

    prompt = prepare_image_prompt(prompt.strip())
    size = body.get("size") or "1024x1024"
    if not isinstance(size, str) or not image_size_allowed(size):
        raise HTTPException(status_code=400, detail="不支援的生圖尺寸")
    task_ctx = begin_task_run(
        db,
        caller=caller,
        request_headers=request.headers,
        dispatch_target="model",
        resource_type="model",
        resource_id=str(model.id),
    )
    enforce_model_ceiling(
        db,
        model=model,
        caller=caller,
        task_ctx=task_ctx,
        conv_id_int=None,
    )
    forwarded = {
        "model": model.name,
        "prompt": prompt,
        "n": 1,
        "size": size,
        "response_format": "b64_json",
    }
    return await proxy_request(
        model=model,
        api_key_id=caller.api_key_id,
        user_id=caller.user.id,
        user_identity=downstream_identity(caller.user),
        department_id=caller.user.department_id,
        request_body=forwarded,
        endpoint_path="/v1/images/generations",
        endpoint_display=_endpoint_display_for(
            db,
            caller.user,
            model.endpoint_url,
            is_internal=bool(getattr(model, "is_internal", False)),
        ),
        tuning=resolve_proxy_tuning(db),
        usage_source=request.headers.get("X-ANILA-Request-Source"),
        model_name_snapshot=model.name,
        task_id=task_ctx.task_id if task_ctx else None,
        task_trace_id=task_ctx.trace_id if task_ctx else None,
        task_run_id=task_ctx.task_run_id if task_ctx else None,
        legacy_runtime_call=task_ctx is None,
        max_response_bytes=IMAGE_UPSTREAM_MAX_BYTES,
    )
