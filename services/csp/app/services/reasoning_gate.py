"""原始思考只留在伺服器。

擁有者、管理員、開發者看得到模型原文。其他人（含單位管理員、副手、
API 金鑰所屬的一般使用者）在串流、非串流、重新載入都收不到原文。
中文摘要失敗就略過，回答照常；原文不確定時也不送出。

摘要與回答並行：每一條串流最多兩個摘要在飛，落後就留下尾巴、丟掉更早的
批次。內容增量不會等摘要。這個模組不開資料庫連線；摘要函式自己開短連線，
還回池子之後才打模型。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import AsyncIterator, Awaitable, Callable, Optional

from anila_core.text.leaked_thought import (
    LEAKED_THOUGHT_PLACEHOLDER,
    THOUGHT_PREFIX_RE,
    sanitize_leaked_thought,
)

logger = logging.getLogger(__name__)

REVEAL_HEADER = "X-ANILA-Reveal-Reasoning"
_REVEAL_ROLES = frozenset({"owner", "admin", "developer"})
_RAW_KEYS = frozenset({"reasoning", "reasoning_content"})

# 串流最多同時兩筆摘要。殼層的節奏是：滿 400 字、或 80 字且過了 2.5 秒、
# 收尾時滿 40 字。落後時只留最後一段，不讓摘要追不上就擋住回答。
_MAX_IN_FLIGHT = 2
_FORCE_CHARS = 40
_BURST_CHARS = 400
_SLOW_CHARS = 80
_SLOW_MS = 2500
# 非串流回答只肯等這麼一下。摘要沒跟上就先回正文，不把模型延遲算進回答。
_NONSTREAM_SUMMARY_DEADLINE_S = 0.15
# 串流在 [DONE] 前給已排進去的摘要一個收尾期限。超過才取消。
_FINISH_DRAIN_S = 0.6
_UPSTREAM_ERROR_DETAIL = "上游暫時無法回應"

_OPEN_RE = re.compile(r"<think(?:ing)?\b[^>]*>", re.IGNORECASE)
_CLOSE_RE = re.compile(r"</think(?:ing)?>", re.IGNORECASE)
_DIRECTIVE_LINE_RE = re.compile(
    r"^[ \t]*(?:[`*>]{1,3}[ \t]*)?(?:DISPATCH:[^\n\r]+|ASK\*?:[^\n\r]+|RECALL:[^\n\r]+)",
    re.MULTILINE,
)
_OPEN_PREFIXES = ("<thinking>", "<think>")
_CLOSE_PREFIXES = ("</thinking>", "</think>")

Summarizer = Callable[..., Awaitable[Optional[str]]]


def may_see_raw_reasoning(user_or_role) -> bool:
    """伺服器依登入者角色決定。不看客戶端旗標，也不把單位管理員算進去。"""
    if user_or_role is None:
        return False
    role = user_or_role if isinstance(user_or_role, str) else getattr(user_or_role, "role", None)
    return role in _REVEAL_ROLES


def reveal_headers(reveal: bool) -> dict[str, str]:
    return {REVEAL_HEADER: "1" if reveal else "0"}


def redact_visible_text(text, *, reveal: bool):
    """可見文字走跟串流同一套剝法。可以看原文、或本來就沒有字，就原樣回去。"""
    if text is None or reveal or not isinstance(text, str) or text == "":
        return text
    redactor = ContentRedactor(reveal=False)
    emit, _parts = redactor.push(text)
    tail, _more = redactor.finish()
    return emit + tail


def _without_raw_keys(value):
    """拿掉每一層的 reasoning／reasoning_content。沒有就沿用原本的物件。"""
    if isinstance(value, dict):
        changed = False
        cleaned = {}
        for key, item in value.items():
            if key in _RAW_KEYS:
                changed = True
                continue
            new_item, child_changed = _without_raw_keys(item)
            if child_changed:
                changed = True
            cleaned[key] = new_item
        if not changed:
            return value, False
        return cleaned, True
    if isinstance(value, list):
        changed = False
        cleaned = []
        for item in value:
            new_item, child_changed = _without_raw_keys(item)
            if child_changed:
                changed = True
            cleaned.append(new_item)
        if not changed:
            return value, False
        return cleaned, True
    return value, False


def redact_message_metadata(metadata, *, reveal: bool):
    """讀取與寫入都走這裡。沒有檢視者時當不可看原文。不改到呼叫端原本的 dict。"""
    if not isinstance(metadata, dict) or reveal:
        return metadata
    cleaned, changed = _without_raw_keys(metadata)
    return cleaned if changed else metadata


def _directive_block(analysis: str) -> str:
    lines: list[str] = []
    seen: set[str] = set()
    for match in _DIRECTIVE_LINE_RE.finditer(analysis or ""):
        line = match.group(0).strip()
        if line and line not in seen:
            seen.add(line)
            lines.append(line)
    return "\n".join(lines)


def _with_directives(analysis: str, answer: str) -> str:
    """協定行要留在可見內容裡，而且排在中文回答前面，Router 才分派得到。"""
    prefix = _directive_block(analysis)
    if not prefix:
        return answer
    if not answer or answer.startswith(prefix):
        return prefix if not answer else answer
    return prefix + "\n" + answer


def _thought_decision(text: str) -> str:
    """開頭是否還可能是 thought／thinking。回傳 match、hold 或 no。"""
    if not text:
        return "hold"
    if THOUGHT_PREFIX_RE.match(text):
        return "match"
    if "\n" in text or "\r" in text or len(text) > 80:
        return "no"
    body = text.lstrip(" \t")
    if not body:
        return "hold" if len(text) <= 32 else "no"
    rest = body[2:] if body.startswith("**") else body[1:] if body[0] in "*`" else body
    low = rest.lower()
    if not low:
        return "hold"
    for word in ("thought", "thinking"):
        if word.startswith(low):
            return "hold"
        if low.startswith(word):
            tail = rest[len(word):]
            if re.fullmatch(r"(?:[*`]{0,2})?\s*[:：]?\s*", tail):
                return "hold"
            return "no"
    return "no"


def _partial_suffix(text: str, *, closing: bool) -> int:
    """尾端有幾字可能是還沒寫完的 think 標籤。完整標籤不在這裡擋。"""
    if not text or "<" not in text:
        return 0
    lower = text.lower()
    prefixes = _CLOSE_PREFIXES if closing else _OPEN_PREFIXES
    hold = 0
    start = max(0, len(lower) - 24)
    for index in range(start, len(lower)):
        if lower[index] != "<":
            continue
        frag = lower[index:]
        if any(prefix.startswith(frag) and frag != prefix for prefix in prefixes):
            hold = max(hold, len(frag))
        if (
            not closing
            and frag.startswith("<think")
            and ">" not in frag
        ):
            hold = max(hold, len(frag))
    return hold


def _collect_text(value, bucket: list[str]) -> None:
    if isinstance(value, str):
        if value:
            bucket.append(value)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if key in _RAW_KEYS or key in ("text", "content", "delta"):
                _collect_text(item, bucket)
        return
    if isinstance(value, list):
        for item in value:
            _collect_text(item, bucket)


def _walk_raw(value, bucket: list[str], *, delete: bool) -> bool:
    changed = False
    if isinstance(value, dict):
        for key in list(value.keys()):
            if key in _RAW_KEYS:
                _collect_text(value[key], bucket)
                if delete:
                    value.pop(key, None)
                    changed = True
                continue
            if _walk_raw(value[key], bucket, delete=delete):
                changed = True
    elif isinstance(value, list):
        for item in value:
            if _walk_raw(item, bucket, delete=delete):
                changed = True
    return changed


class ContentRedactor:
    """把 `<think>` 與 thought 開頭從可見內容拿掉。

    標籤或標題被切在兩個片段中間時先握住，不把半截標籤送出去。
    可以看原文的人內容原樣送出，只另外抄一份給摘要。
    """

    def __init__(self, *, reveal: bool):
        self.reveal = reveal
        self._mode = "start"
        self._hold = ""
        self._buf = ""
        self._thought = ""
        self._pushed = 0
        self._obs = ""
        self._seen: set[str] = set()
        self._thought_noted = False
        self._closed = False

    def push(self, text: str) -> tuple[str, list[str]]:
        if not text or self._closed:
            return "", []
        if self.reveal:
            return text, self._observe(text)
        if self._mode == "think":
            self._buf += text
            return self._scan_think()
        if self._mode == "thought":
            return self._in_thought(text)
        if self._mode == "normal":
            self._buf += text
            return self._scan_normal()
        self._hold += text
        decision = _thought_decision(self._hold)
        if decision == "hold":
            return "", []
        if decision == "match":
            self._thought = self._hold
            self._hold = ""
            self._pushed = 0
            self._mode = "thought"
            return self._in_thought("")
        pending = self._hold
        self._hold = ""
        self._mode = "normal"
        self._buf += pending
        return self._scan_normal()

    def finish(self) -> tuple[str, list[str]]:
        if self.reveal or self._closed:
            self._closed = True
            return "", []
        self._closed = True
        if self._mode == "start":
            pending = self._hold
            self._hold = ""
            if _thought_decision(pending) == "match":
                self._thought = pending
                self._mode = "thought"
                return self._finish_thought()
            self._mode = "normal"
            self._buf += pending
            emit, parts = self._scan_normal()
            tail, more = self._finish_normal()
            return emit + tail, parts + more
        if self._mode == "thought":
            return self._finish_thought()
        if self._mode == "think":
            extra = [self._buf] if self._buf else []
            self._buf = ""
            self._mode = "normal"
            return "", extra
        return self._finish_normal()

    def _observe(self, text: str) -> list[str]:
        self._obs = (self._obs + text)[-8000:]
        found: list[str] = []
        for match in _OPEN_RE.finditer(self._obs):
            close = _CLOSE_RE.search(self._obs, match.end())
            if close is None:
                continue
            inner = self._obs[match.end():close.start()]
            if inner and inner not in self._seen:
                self._seen.add(inner)
                found.append(inner)
        if not self._thought_noted and THOUGHT_PREFIX_RE.match(self._obs):
            answer, analysis = sanitize_leaked_thought(self._obs, "")
            if analysis and answer != LEAKED_THOUGHT_PLACEHOLDER and analysis not in self._seen:
                self._thought_noted = True
                self._seen.add(analysis)
                found.append(analysis)
        return found

    def _fresh(self, analysis: str) -> list[str]:
        if len(analysis) <= self._pushed:
            return []
        piece = analysis[self._pushed:]
        self._pushed = len(analysis)
        return [piece] if piece else []

    def _in_thought(self, text: str) -> tuple[str, list[str]]:
        if text:
            self._thought += text
        answer, analysis = sanitize_leaked_thought(self._thought, "")
        if answer and answer != LEAKED_THOUGHT_PLACEHOLDER:
            safe = _with_directives(analysis, answer)
            fresh = self._fresh(analysis)
            self._mode = "normal"
            self._thought = ""
            self._pushed = 0
            return safe, fresh
        return "", self._fresh(self._thought)

    def _finish_thought(self) -> tuple[str, list[str]]:
        answer, analysis = sanitize_leaked_thought(self._thought, "")
        if answer and answer != LEAKED_THOUGHT_PLACEHOLDER:
            safe = _with_directives(analysis, answer)
            fresh = self._fresh(analysis)
        else:
            safe = _with_directives(self._thought, "") or LEAKED_THOUGHT_PLACEHOLDER
            fresh = self._fresh(self._thought)
        self._mode = "normal"
        self._thought = ""
        self._pushed = 0
        return safe, fresh

    def _scan_normal(self) -> tuple[str, list[str]]:
        safe: list[str] = []
        extracted: list[str] = []
        while self._mode == "normal" and self._buf:
            match = _OPEN_RE.search(self._buf)
            if match:
                safe.append(self._buf[:match.start()])
                self._buf = self._buf[match.end():]
                self._mode = "think"
                more_safe, more_ex = self._scan_think()
                safe.append(more_safe)
                extracted.extend(more_ex)
                continue
            hold = _partial_suffix(self._buf, closing=False)
            if hold:
                safe.append(self._buf[:-hold])
                self._buf = self._buf[-hold:]
                break
            safe.append(self._buf)
            self._buf = ""
        return "".join(safe), extracted

    def _scan_think(self) -> tuple[str, list[str]]:
        extracted: list[str] = []
        while self._mode == "think" and self._buf:
            match = _CLOSE_RE.search(self._buf)
            if match:
                inner = self._buf[:match.start()]
                if inner:
                    extracted.append(inner)
                self._buf = self._buf[match.end():]
                self._mode = "normal"
                break
            hold = _partial_suffix(self._buf, closing=True)
            if hold < len(self._buf):
                inner = self._buf[:-hold] if hold else self._buf
                if inner:
                    extracted.append(inner)
                self._buf = self._buf[-hold:] if hold else ""
            break
        if self._mode == "normal":
            more_safe, more_ex = self._scan_normal()
            return more_safe, extracted + more_ex
        return "", extracted

    def _finish_normal(self) -> tuple[str, list[str]]:
        hold = self._buf
        self._buf = ""
        if not hold:
            return "", []
        if hold.lower().startswith("<think") and _partial_suffix(hold, closing=False) == len(hold):
            return "", [hold]
        return hold, []


def _summary_frame(text: str) -> str:
    payload = {"choices": [{"index": 0, "delta": {"anila_thinking_summary": text}}]}
    return "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"


def _content_frame(text: str) -> str:
    payload = {"choices": [{"index": 0, "delta": {"content": text}}]}
    return "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"


class StreamSummaryPump:
    """有界並行的摘要。滿載時只留尾巴，不新增工作，也不等待。"""

    def __init__(
        self,
        *,
        user_id: int | None,
        department_id: int | None,
        summarizer: Summarizer | None = None,
    ):
        self.user_id = user_id
        self.department_id = department_id
        self._summarizer = summarizer or _default_summarizer
        self.pending = ""
        self.previous: list[dict] = []
        self.inflight: list[asyncio.Task] = []
        self.ready: list[str] = []
        self._closed = False
        self._finished = False
        self._last_at = 0.0

    def push(self, text: str) -> None:
        if self._closed or not text:
            return
        from app.services.thinking_summary import MAX_ADDED_CHARS

        self.pending = (self.pending + text)[-MAX_ADDED_CHARS:]
        self._maybe_schedule(force=False)

    def drain(self) -> list[str]:
        ready = self.ready
        self.ready = []
        return ready

    async def finish(self) -> list[str]:
        if self._finished:
            return []
        self._maybe_schedule(force=True)
        pending = [task for task in self.inflight if not task.done()]
        if pending:
            await asyncio.wait(pending, timeout=_FINISH_DRAIN_S)
        ready = self.drain()
        self._closed = True
        self._finished = True
        for task in self.inflight:
            if not task.done():
                task.cancel()
        self.inflight = []
        return ready

    def cancel(self) -> None:
        self._closed = True
        self._finished = True
        for task in self.inflight:
            if not task.done():
                task.cancel()
        self.inflight = []

    def _live(self) -> int:
        self.inflight = [task for task in self.inflight if not task.done()]
        return len(self.inflight)

    def _maybe_schedule(self, *, force: bool) -> None:
        if self._closed or self._live() >= _MAX_IN_FLIGHT:
            return
        added = len(self.pending)
        elapsed_ms = 99_999.0 if self._last_at == 0 else (time.monotonic() - self._last_at) * 1000
        if force:
            if added < _FORCE_CHARS:
                return
        elif added >= _BURST_CHARS:
            pass
        elif added >= _SLOW_CHARS and elapsed_ms >= _SLOW_MS:
            pass
        else:
            return
        chunk = self.pending
        self.pending = ""
        self._last_at = time.monotonic()
        task = asyncio.create_task(self._run(chunk, list(self.previous)))
        self.inflight.append(task)

    async def _run(self, chunk: str, previous: list[dict]) -> None:
        text: str | None = None
        try:
            text = await self._summarizer(
                added=chunk,
                previous=previous,
                user_id=self.user_id,
                department_id=self.department_id,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("thinking_summary: batch failed")
            text = None
        if text:
            from app.services.thinking_summary import sanitize_summary

            text = sanitize_summary(text)
        if text and not self._closed:
            if not self.previous or self.previous[-1].get("text") != text:
                self.previous.append({"text": text, "at": 0})
                from app.services.thinking_summary import MAX_HISTORY

                self.previous = self.previous[-MAX_HISTORY:]
                self.ready.append(text)
        if not self._closed:
            self._maybe_schedule(force=False)


async def _default_summarizer(**kwargs) -> str | None:
    from app.services.thinking_summary import summarize_reasoning_detached

    return await summarize_reasoning_detached(**kwargs)


def _parse_block(block: str) -> tuple[str | None, str | None, bool]:
    event_name = None
    data_parts: list[str] = []
    for line in block.split("\n"):
        if line.startswith("event:"):
            event_name = line[6:].strip()
        elif line.startswith("data:"):
            data_parts.append(line[5:].lstrip())
    data = "\n".join(data_parts) if data_parts else None
    is_done = data is not None and data.strip() == "[DONE]"
    return event_name, data, is_done


def _render(event_name: str | None, obj) -> str:
    body = "data: " + json.dumps(obj, ensure_ascii=False)
    if event_name:
        return f"event: {event_name}\n{body}\n\n"
    return body + "\n\n"


def _event_text(obj) -> str:
    if isinstance(obj, str):
        return obj
    if not isinstance(obj, dict):
        return ""
    for key in ("delta", "content", "text", "reasoning"):
        value = obj.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _error_status(data: str | None) -> int:
    if not data:
        return 502
    try:
        obj = json.loads(data)
    except json.JSONDecodeError:
        obj = None
    if isinstance(obj, dict):
        for key in ("status", "status_code"):
            raw = obj.get(key)
            if isinstance(raw, bool):
                continue
            if isinstance(raw, int) and raw > 0:
                return raw
            if isinstance(raw, str) and raw.isdigit():
                return int(raw)
    match = re.search(r"""["']status["']\s*:\s*(\d{3})""", data)
    if match:
        return int(match.group(1))
    return 502


def _safe_error_block(block: str, event_name: str | None, data: str | None, *, reveal: bool) -> str:
    if reveal:
        return block + "\n\n"
    status = _error_status(data)
    payload = {
        "status": status,
        "detail": f"{_UPSTREAM_ERROR_DETAIL}（HTTP {status}）",
    }
    return _render(event_name or "error", payload)


def _rewrite_tool_arguments(choice: dict, redactor: ContentRedactor) -> tuple[bool, list[str]]:
    """工具參數用另一個 redactor，避免跟正文的暫存標籤混在一起。"""
    extracted: list[str] = []
    changed = False
    for field in ("delta", "message"):
        container = choice.get(field)
        if not isinstance(container, dict):
            continue
        calls = container.get("tool_calls")
        if not isinstance(calls, list):
            continue
        for index, call in enumerate(calls):
            if not isinstance(call, dict):
                continue
            fn = call.get("function")
            if not isinstance(fn, dict) or not isinstance(fn.get("arguments"), str):
                continue
            call_index = call.get("index", index)
            if isinstance(call_index, int):
                redactor.last_tool_index = call_index
            emit, parts = redactor.push(fn["arguments"])
            extracted.extend(parts)
            if not redactor.reveal and emit != fn["arguments"]:
                changed = True
                if emit:
                    fn["arguments"] = emit
                else:
                    fn.pop("arguments", None)
    return changed, extracted


def _tool_arg_frame(index: int, text: str) -> str:
    payload = {
        "choices": [{
            "index": 0,
            "delta": {"tool_calls": [{"index": index, "function": {"arguments": text}}]},
        }],
    }
    return "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"


def _rewrite_choice_content(choice: dict, redactor: ContentRedactor) -> tuple[bool, list[str]]:
    extracted: list[str] = []
    changed = False
    for field in ("delta", "message"):
        container = choice.get(field)
        if not isinstance(container, dict) or "content" not in container:
            continue
        content = container["content"]
        if isinstance(content, str):
            emit, parts = redactor.push(content)
            extracted.extend(parts)
            if not redactor.reveal and emit != content:
                changed = True
                if emit:
                    container["content"] = emit
                else:
                    container.pop("content", None)
        elif isinstance(content, list):
            for part in content:
                if not isinstance(part, dict) or not isinstance(part.get("text"), str):
                    continue
                emit, parts = redactor.push(part["text"])
                extracted.extend(parts)
                if not redactor.reveal and emit != part["text"]:
                    changed = True
                    part["text"] = emit
    return changed, extracted


def _apply_json_block(
    block: str,
    obj,
    event_name: str | None,
    redactor: ContentRedactor,
    tool_redactor: ContentRedactor,
    *,
    reveal: bool,
):
    """回傳 (要送出的框架或 None, 抽出的原文)。None 表示整段丟掉。"""
    bucket: list[str] = []
    changed = _walk_raw(obj, bucket, delete=not reveal)
    if event_name == "anila.reasoning":
        text = _event_text(obj)
        if text:
            bucket.append(text)
        if not reveal:
            return None, bucket
        return block + "\n\n", bucket
    if isinstance(obj, dict):
        choices = obj.get("choices")
        if isinstance(choices, list):
            for choice in choices:
                if isinstance(choice, dict):
                    choice_changed, extra = _rewrite_choice_content(choice, redactor)
                    tool_changed, tool_extra = _rewrite_tool_arguments(choice, tool_redactor)
                    bucket.extend(extra)
                    bucket.extend(tool_extra)
                    changed = changed or choice_changed or tool_changed
    if not changed:
        return block + "\n\n", bucket
    return _render(event_name, obj), bucket


def _process_block(
    block: str,
    redactor: ContentRedactor,
    pump: StreamSummaryPump,
    *,
    reveal: bool,
    tool_redactor: ContentRedactor,
):
    event_name, data, is_done = _parse_block(block)
    if is_done:
        return [], True
    if event_name == "error":
        return [_safe_error_block(block, event_name, data, reveal=reveal)], False
    if data is None:
        return [block + "\n\n"], False
    try:
        obj = json.loads(data)
    except json.JSONDecodeError:
        if not reveal:
            return [], False
        return [block + "\n\n"], False
    frame, bucket = _apply_json_block(
        block, obj, event_name, redactor, tool_redactor, reveal=reveal,
    )
    for piece in bucket:
        pump.push(piece)
    frames = [frame] if frame else []
    return frames, False


async def _closing_frames(
    redactor: ContentRedactor,
    tool_redactor: ContentRedactor,
    pump: StreamSummaryPump,
) -> list[str]:
    emit, parts = redactor.finish()
    tool_emit, tool_parts = tool_redactor.finish()
    frames: list[str] = []
    if emit:
        frames.append(_content_frame(emit))
    if tool_emit:
        frames.append(_tool_arg_frame(getattr(tool_redactor, "last_tool_index", 0), tool_emit))
    for part in parts + tool_parts:
        pump.push(part)
    for text in await pump.finish():
        frames.append(_summary_frame(text))
    return frames


async def gate_sse_stream(
    upstream: AsyncIterator[str],
    *,
    reveal: bool,
    user_id: int | None,
    department_id: int | None,
    summarizer: Summarizer | None = None,
) -> AsyncIterator[str]:
    """使用者看得到的 SSE。原文先拿掉，摘要晚一點跟著走，不擋這一段回答。"""
    redactor = ContentRedactor(reveal=reveal)
    tool_redactor = ContentRedactor(reveal=reveal)
    pump = StreamSummaryPump(
        user_id=user_id,
        department_id=department_id,
        summarizer=summarizer,
    )
    buf = ""
    done = False
    try:
        async for chunk in upstream:
            # [DONE] 之後仍要把上游讀完。在這裡停掉會被當成客戶端斷線：
            # TaskRun 變成 failed，[DONE] 後面的用量也不會入列。剩下的位元組不再送出。
            if done:
                continue
            if isinstance(chunk, bytes):
                chunk = chunk.decode("utf-8", errors="replace")
            buf += chunk
            while "\n\n" in buf:
                block, buf = buf.split("\n\n", 1)
                frames, is_done = _process_block(
                    block, redactor, pump, reveal=reveal, tool_redactor=tool_redactor,
                )
                for frame in frames:
                    yield frame
                for text in pump.drain():
                    yield _summary_frame(text)
                if is_done:
                    for frame in await _closing_frames(redactor, tool_redactor, pump):
                        yield frame
                    yield block + "\n\n"
                    done = True
                    break
        if not done and buf.strip():
            frames, is_done = _process_block(
                buf, redactor, pump, reveal=reveal, tool_redactor=tool_redactor,
            )
            for frame in frames:
                yield frame
            for text in pump.drain():
                yield _summary_frame(text)
            if is_done:
                for frame in await _closing_frames(redactor, tool_redactor, pump):
                    yield frame
                yield buf if buf.endswith("\n\n") else buf + "\n\n"
                done = True
        if not done:
            for frame in await _closing_frames(redactor, tool_redactor, pump):
                yield frame
    finally:
        pump.cancel()


async def _one_summary(
    parts: list[str],
    *,
    user_id: int | None,
    department_id: int | None,
    summarizer: Summarizer | None,
) -> str | None:
    from app.services.thinking_summary import MAX_ADDED_CHARS, MIN_ADDED_CHARS, sanitize_summary

    text = "".join(parts).strip()
    if len(text) < MIN_ADDED_CHARS:
        return None
    fn = summarizer or _default_summarizer
    try:
        raw = await fn(
            added=text[-MAX_ADDED_CHARS:],
            previous=[],
            user_id=user_id,
            department_id=department_id,
        )
    except Exception:
        logger.warning("thinking_summary: non-stream batch failed")
        return None
    return sanitize_summary(raw) if raw else None


def _redact_tool_message(message: dict, *, reveal: bool, bucket: list[str]) -> None:
    calls = message.get("tool_calls")
    if not isinstance(calls, list):
        return
    redactor = ContentRedactor(reveal=reveal)
    last_fn = None
    for index, call in enumerate(calls):
        if not isinstance(call, dict):
            continue
        fn = call.get("function")
        if not isinstance(fn, dict) or not isinstance(fn.get("arguments"), str):
            continue
        emit, extra = redactor.push(fn["arguments"])
        bucket.extend(extra)
        last_fn = fn
        if not reveal and emit != fn["arguments"]:
            if emit:
                fn["arguments"] = emit
            else:
                fn.pop("arguments", None)
        elif not reveal:
            fn["arguments"] = emit
    tail, more = redactor.finish()
    bucket.extend(more)
    if tail and last_fn is not None and not reveal:
        last_fn["arguments"] = (last_fn.get("arguments") or "") + tail


async def gate_completion_payload(
    payload,
    *,
    reveal: bool,
    user_id: int | None,
    department_id: int | None,
    summarizer: Summarizer | None = None,
):
    """非串流出口。先剝原文，再等最多一則摘要；摘要失敗仍回傳已剝過的正文。"""
    if not isinstance(payload, dict):
        return payload
    bucket: list[str] = []
    _walk_raw(payload, bucket, delete=not reveal)
    choices = payload.get("choices")
    if isinstance(choices, list):
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            message = choice.get("message")
            if not isinstance(message, dict):
                continue
            content = message.get("content")
            if isinstance(content, str):
                redactor = ContentRedactor(reveal=reveal)
                emit, extra = redactor.push(content)
                tail, more = redactor.finish()
                bucket.extend(extra)
                bucket.extend(more)
                if not reveal:
                    message["content"] = emit + tail
            elif isinstance(content, list):
                redactor = ContentRedactor(reveal=reveal)
                for part in content:
                    if isinstance(part, dict) and isinstance(part.get("text"), str):
                        emit, extra = redactor.push(part["text"])
                        bucket.extend(extra)
                        if not reveal:
                            part["text"] = emit
                _tail, more = redactor.finish()
                bucket.extend(more)
            _redact_tool_message(message, reveal=reveal, bucket=bucket)
    try:
        summary = await asyncio.wait_for(
            _one_summary(
                bucket,
                user_id=user_id,
                department_id=department_id,
                summarizer=summarizer,
            ),
            timeout=_NONSTREAM_SUMMARY_DEADLINE_S,
        )
    except asyncio.TimeoutError:
        summary = None
    if not summary or not isinstance(choices, list):
        return payload
    for choice in choices:
        message = choice.get("message") if isinstance(choice, dict) else None
        if isinstance(message, dict):
            message["anila_thinking_summary"] = summary
            break
    meta = payload.get("anila_meta")
    if not isinstance(meta, dict):
        meta = {}
        payload["anila_meta"] = meta
    rows = [row for row in (meta.get("thinking_summaries") or []) if isinstance(row, dict)]
    if not rows or rows[-1].get("text") != summary:
        rows.append({"text": summary, "at": 0})
    meta["thinking_summaries"] = rows[-24:]
    return payload
