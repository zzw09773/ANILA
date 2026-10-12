"""回答完成後，另外產生「建議下一個問題」。

不進主回答的 prompt，也不往主串流插事件。呼叫端在訊息已經存好之後
打 POST。這裡只讀最後一則使用者訊息和這則回答，寫回該訊息的 meta。

失敗、逾時、額度、上游 5xx、並行額滿都回空清單，不把錯誤丟給畫面。
只有還沒產生過的成功結果（含模型說 NONE）會寫進 meta。
"""
from __future__ import annotations

import asyncio
import os
import re
import threading
import time

from fastapi import HTTPException
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from app.models.conversation import Conversation
from app.models.message import Message
from app.models.platform_setting import get_setting
from app.models.user import User

FOLLOW_UP_PROMPT = (
    "以繁體中文（台灣用語）列出使用者最可能接著問的 3 個問題，每行一個，每個 30 字內，"
    "用使用者的口吻，不要重複已回答的內容，不要編造文件名稱；如果不適合追問就輸出 NONE。"
)

INPUT_CHAR_LIMIT = 2000
MIN_SUGGESTION_CHARS = 4
MAX_SUGGESTION_CHARS = 40
MIN_ANSWER_CHARS = 40
NEAR_IDENTICAL_RATIO = 0.85
GENERATED_FLAG = "follow_ups_generated"
_ROUTER_AGENT_ID = "anila-router"
_DEFAULT_TIMEOUT_S = 6.0
_DEFAULT_CONCURRENCY = 4
_DEFAULT_MAX = 3

_INCOMPLETE_STATES = frozenset({"stopped", "failed", "interrupted", "unanswered"})
_NOT_READY_DETAIL = "只能為已完成的助理訊息產生建議"

_NUMBER_RE = re.compile(r"^(?:[（(]\d+[）)]|\d+[.)）、])\s*")
_BULLET_RE = re.compile(r"^[-*•·‧]\s*")
_NONE_RE = re.compile(r"^NONE[。．.！!？?]*$", re.IGNORECASE)
_QUOTE_CHARS = set("\"“”'‘’「」『』")
_TRAILING_PUNCT = "？?！!。．.，,、；;：:"
_ARTIFACT_FENCE_RE = re.compile(
    r"(?m)^[ \t]*```(?:html|htm|svg|jsx|tsx|react)\b",
    re.IGNORECASE,
)
_DISPATCH_RE = re.compile(r"^\s*dispatch to\s+(\S.*?)\s*$", re.IGNORECASE)

_gate = threading.Lock()
_inflight = 0
_s2t = None


class FollowUpParseError(Exception):
    """簡繁檢查無法執行。這次當成產生失敗，不寫快取。"""


def followups_timeout_seconds() -> float:
    """每次呼叫重讀。非法或非正數回到 6 秒。"""
    raw = os.environ.get("FOLLOWUPS_TIMEOUT_S")
    if raw is None or not str(raw).strip():
        return _DEFAULT_TIMEOUT_S
    try:
        value = float(str(raw).strip())
    except ValueError:
        return _DEFAULT_TIMEOUT_S
    if value <= 0:
        return _DEFAULT_TIMEOUT_S
    return value


def followups_concurrency_limit() -> int:
    """每次取得名額時重讀。非法或負數回到 4；0 表示一律不再接新的。"""
    raw = os.environ.get("FOLLOWUPS_MAX_CONCURRENCY")
    if raw is None or not str(raw).strip():
        return _DEFAULT_CONCURRENCY
    try:
        value = int(str(raw).strip())
    except ValueError:
        return _DEFAULT_CONCURRENCY
    if value < 0:
        return _DEFAULT_CONCURRENCY
    return value


def try_acquire() -> bool:
    """額滿立刻失敗，不排隊。"""
    global _inflight
    with _gate:
        if _inflight >= followups_concurrency_limit():
            return False
        _inflight += 1
        return True


def release() -> None:
    global _inflight
    with _gate:
        if _inflight > 0:
            _inflight -= 1


def reset_concurrency() -> None:
    """測試用。正式路徑不呼叫。"""
    global _inflight
    with _gate:
        _inflight = 0


def _peel_quotes(text: str) -> str:
    for _ in range(2):
        if len(text) < 2 or text[0] not in _QUOTE_CHARS or text[-1] not in _QUOTE_CHARS:
            break
        text = text[1:-1].strip()
    return text


def _clean_line(line: str) -> str:
    text = line.strip()
    for _ in range(3):
        peeled = _peel_quotes(text)
        unnumbered = _NUMBER_RE.sub("", peeled, count=1).strip()
        unbulleted = _BULLET_RE.sub("", unnumbered, count=1).strip()
        cleaned = _peel_quotes(unbulleted).strip()
        if cleaned == text:
            break
        text = cleaned
    return text


def _normalize_for_compare(text: str) -> str:
    compact = "".join((text or "").split()).lower()
    while compact and compact[-1] in _TRAILING_PUNCT:
        compact = compact[:-1]
    return compact


def _near_identical(suggestion: str, question: str) -> bool:
    left = _normalize_for_compare(suggestion)
    right = _normalize_for_compare(question)
    if not left or not right:
        return False
    if left == right:
        return True
    shorter, longer = (left, right) if len(left) <= len(right) else (right, left)
    return shorter in longer and (len(shorter) / len(longer)) >= NEAR_IDENTICAL_RATIO


def _converter():
    global _s2t
    if _s2t is None:
        from opencc import OpenCC

        _s2t = OpenCC("s2t")
    return _s2t


def _contains_simplified(text: str) -> bool:
    """整句轉成繁體後有變，就當裡面有簡體。轉換器壞掉則整次產生作廢。"""
    try:
        converted = _converter().convert(text)
    except Exception as exc:
        raise FollowUpParseError("簡繁檢查失敗") from exc
    return converted != text


def parse_follow_ups(raw: str, user_question: str, *, limit: int) -> list[str]:
    """清編號與引號、丢掉 NONE、過短過長、簡體、與原問題幾乎一樣的，再去重。"""
    if limit < 1:
        return []
    kept: list[str] = []
    for line in (raw or "").splitlines():
        text = _clean_line(line)
        if not text or _NONE_RE.match(text):
            continue
        if len(text) < MIN_SUGGESTION_CHARS or len(text) > MAX_SUGGESTION_CHARS:
            continue
        if _contains_simplified(text):
            continue
        if _near_identical(text, user_question or ""):
            continue
        if text in kept:
            continue
        kept.append(text)
        if len(kept) >= limit:
            break
    return kept


def _stream_state(msg: Message) -> str | None:
    meta = msg.metadata_ if isinstance(msg.metadata_, dict) else None
    if not meta:
        return None
    envelope = meta.get("anila_stream")
    if not isinstance(envelope, dict):
        return None
    state = envelope.get("state")
    return state if isinstance(state, str) else None


def _require_suggestable(msg: Message) -> None:
    """使用者訊息、還在跑、或看不懂的狀態是 422。中斷與失敗留給空清單。"""
    if msg.role != "assistant":
        raise HTTPException(status_code=422, detail=_NOT_READY_DETAIL)
    state = _stream_state(msg)
    if state is None or state == "complete" or state in _INCOMPLETE_STATES:
        return
    raise HTTPException(status_code=422, detail=_NOT_READY_DETAIL)


def _cached(msg: Message) -> list[str] | None:
    meta = msg.metadata_ if isinstance(msg.metadata_, dict) else None
    if not meta or meta.get(GENERATED_FLAG) is not True:
        return None
    raw = meta.get("follow_ups")
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, str)]


def _user_enabled(user: User) -> bool:
    settings = user.ui_settings if isinstance(user.ui_settings, dict) else {}
    return settings.get("followUpsEnabled", True) is not False


def _meta(msg: Message) -> dict:
    return msg.metadata_ if isinstance(msg.metadata_, dict) else {}


def _nonempty_collection(value) -> bool:
    return isinstance(value, (list, dict)) and bool(value)


def _is_artifact(msg: Message) -> bool:
    meta = _meta(msg)
    document = meta.get("document")
    if isinstance(document, dict) and document:
        return True
    for key in ("artifact", "artifact_revision", "artifactRevision"):
        if meta.get(key):
            return True
    return _ARTIFACT_FENCE_RE.search(msg.content or "") is not None


def _is_compare(msg: Message) -> bool:
    meta = _meta(msg)
    if meta.get("compare") is True:
        return True
    return meta.get("source") == "compare" or meta.get("turn") == "compare"


def _add_agent(names: set[str], value) -> None:
    if not isinstance(value, str):
        return
    name = value.strip()
    if name and name != _ROUTER_AGENT_ID:
        names.add(name)


def _agent_names(chain) -> set[str]:
    names: set[str] = set()
    if not isinstance(chain, list):
        return names
    for hop in chain:
        if isinstance(hop, str):
            _add_agent(names, hop)
            continue
        if not isinstance(hop, dict):
            continue
        _add_agent(names, hop.get("agent_id"))
        summary = hop.get("output_summary")
        if isinstance(summary, str):
            match = _DISPATCH_RE.match(summary)
            if match:
                _add_agent(names, match.group(1))
    return names


def _is_agentic(msg: Message) -> bool:
    """多代理、待辦、跨度或中斷才跳過。單次派工與工具呼叫仍可建議。"""
    meta = _meta(msg)
    if _nonempty_collection(meta.get("todos")) or _nonempty_collection(meta.get("spans")):
        return True
    interrupt = meta.get("interrupt")
    if isinstance(interrupt, dict) and interrupt:
        return True
    return len(_agent_names(meta.get("handoff_chain"))) >= 2


def _should_skip(db: Session, user: User, conv: Conversation, msg: Message) -> bool:
    if _stream_state(msg) in _INCOMPLETE_STATES:
        return True
    if len((msg.content or "").strip()) < MIN_ANSWER_CHARS:
        return True
    if get_setting(db, "follow_ups.enabled") is False:
        return True
    if not _user_enabled(user):
        return True
    if conv.classified and get_setting(db, "follow_ups.allow_classified") is not True:
        return True
    return _is_artifact(msg) or _is_compare(msg) or _is_agentic(msg)


def resolve_followup_model(db: Session, *, local_only: bool):
    """摘要角色優先，沒有就主路由。機密只留下本地模型。"""
    from app.services.model_roles import resolve_role

    for role in ("summary", "router_primary"):
        resolved = resolve_role(db, role)
        model = resolved.model if resolved.status == "ok" else None
        if model is None:
            continue
        if local_only and not bool(getattr(model, "is_internal", False)):
            continue
        return model
    return None


def _last_user_message(db: Session, msg: Message) -> Message | None:
    current = msg
    seen = {msg.id}
    while current.parent_id and current.parent_id not in seen:
        parent = db.get(Message, current.parent_id)
        if parent is None or parent.conversation_id != msg.conversation_id:
            break
        if parent.role == "user":
            return parent
        seen.add(parent.id)
        current = parent
    return (
        db.query(Message)
        .filter(
            Message.conversation_id == msg.conversation_id,
            Message.role == "user",
            Message.id < msg.id,
        )
        .order_by(Message.id.desc())
        .first()
    )


def _follow_ups_max(db: Session) -> int:
    value = get_setting(db, "follow_ups.max")
    if isinstance(value, bool) or not isinstance(value, int):
        return _DEFAULT_MAX
    return value


def _model_input(question: str, answer: str) -> str:
    return (
        "使用者的問題：\n"
        f"{question[:INPUT_CHAR_LIMIT]}\n\n"
        "回答：\n"
        f"{answer[:INPUT_CHAR_LIMIT]}"
    )


def _persist(db: Session, message_id: int, suggestions: list[str]) -> list[str]:
    try:
        msg = db.get(Message, message_id)
        if msg is None:
            return suggestions
        db.refresh(msg)
        cached = _cached(msg)
        if cached is not None:
            return cached
        meta = dict(msg.metadata_) if isinstance(msg.metadata_, dict) else {}
        meta["follow_ups"] = list(suggestions)
        meta[GENERATED_FLAG] = True
        msg.metadata_ = meta
        flag_modified(msg, "metadata_")
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
    return suggestions


async def _generate(
    db: Session,
    user: User,
    conv: Conversation,
    msg: Message,
    question: str,
    model,
) -> list[str]:
    from app.services.internal_llm import complete_chat

    limit = _follow_ups_max(db)
    classified = bool(conv.classified)
    timeout = followups_timeout_seconds()
    payload = {
        "messages": [
            {"role": "system", "content": FOLLOW_UP_PROMPT},
            {"role": "user", "content": _model_input(question, msg.content or "")},
        ],
        "temperature": 0.3,
        "max_tokens": 120,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    try:
        raw = await asyncio.wait_for(
            complete_chat(
                db,
                model,
                payload,
                user_id=user.id,
                department_id=user.department_id,
                on_behalf_of_user=True,
                request_type_override="follow_ups",
                deadline=time.monotonic() + timeout,
                record_usage=not classified,
                conversation_id=None if classified else str(conv.id),
            ),
            timeout=timeout,
        )
    except Exception:
        return []
    try:
        suggestions = parse_follow_ups(raw, question, limit=limit)
    except Exception:
        return []
    return _persist(db, msg.id, suggestions)


async def suggest_follow_ups(
    db: Session,
    user: User,
    conv: Conversation,
    msg: Message,
) -> list[str]:
    """產生或回放這則助理訊息的建議問題。不合資格的訊息丟 422。"""
    _require_suggestable(msg)
    cached = _cached(msg)
    if cached is not None:
        return cached
    if _should_skip(db, user, conv, msg):
        return []
    user_msg = _last_user_message(db, msg)
    question = (user_msg.content or "").strip() if user_msg is not None else ""
    if not question:
        return []
    model = resolve_followup_model(db, local_only=bool(conv.classified))
    if model is None:
        return []
    if not try_acquire():
        return []
    try:
        try:
            db.refresh(msg)
            cached = _cached(msg)
            if cached is not None:
                return cached
            return await _generate(db, user, conv, msg, user_msg.content or "", model)
        except Exception:
            return []
    finally:
        release()
