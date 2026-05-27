"""P2-6 AutoDreamer — 把一個 session 的對話抽成 :class:`SessionMemory`。

對齊 claude-code-src ``services/autoDream/autoDream.ts`` + deep-dive §4.16:

- 接收 session 結束時的 message list,呼 LLM 跑「dream prompt」,抽出
  ``summary`` / ``topics`` / ``lessons``。
- 解析回 JSON,組 :class:`SessionMemory`,存進 :class:`SessionMemoryStore`。
- 在背景跑(``async`` + 可選 ``asyncio.create_task``),不阻塞主 agent flow。

設計取捨
~~~~~~~~

- ``llm_caller`` 是 injectable ``Callable[[str], Awaitable[str]]`` — 拿 prompt
  字串,回 LLM 完整 response text。讓測試 mock 一行帶過,production 端可以
  wrap openai-agents Runner / LiteLLM。沒綁特定 SDK。
- prompt template 可 override(預設用中文 + 英文 hybrid,對齊既有
  ``memory/summarizer.py`` 的 prompt 風格)。
- dedup:若 ``store.load(session_id)`` 已有同 ``conversation_hash`` 條目,
  直接回原 memory,不浪費 LLM call(避免重複 dream)。
- 背景模式:``dream_in_background`` 用 ``asyncio.create_task``,給 caller 一個
  ``Task`` handle,可選 ``await`` 或丟著 fire-and-forget。失敗 log warning,
  不冒泡到主 flow。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone

from anila_agent.memory.compaction import Message
from anila_agent.memory.session_memory import (
    SessionMemory,
    SessionMemoryStore,
    compute_conversation_hash,
)

logger = logging.getLogger(__name__)

__all__ = [
    "DREAM_PROMPT_TEMPLATE",
    "AutoDreamer",
    "LlmCaller",
]


# ---------------------------------------------------------------------------
# 型別 / 預設 prompt
# ---------------------------------------------------------------------------


LlmCaller = Callable[[str], Awaitable[str]]
"""LLM 呼叫器型別 — 吃 prompt 字串,回 raw response text。

讓 ``AutoDreamer`` 不綁特定 SDK。production 端可:

>>> async def llm_caller(prompt: str) -> str:
...     result = await Runner.run(starting_agent=agent, input=prompt, max_turns=1)
...     return result.final_output or ""
"""


DREAM_PROMPT_TEMPLATE = """You are an autoDream summarizer extracting cross-session
context from a finished conversation. Output STRICT JSON (no prose around it):

{{
  "summary": "<繁體中文 1-3 句,描述這個 session 在處理什麼問題與最終結果>",
  "topics": ["<關鍵主題 1>", "<關鍵主題 2>", ...],
  "lessons": ["<lesson learned / 可重用經驗 1>", ...]
}}

Rules:
- topics 為英文 / 程式術語 keyword(供 keyword overlap retrieval),3-8 個。
- lessons 為自然語句,可中可英;描述「下次遇到類似情境可重用的判斷」。
- 若 session 沒明確 lesson,lessons 可為空 list。
- 不要 fabricate — 沒在 transcript 裡的東西就不要寫。
- 嚴格 JSON,不能有 markdown code fence。

Session id: {session_id}
Message count: {message_count}

Transcript:
{transcript}
"""


# ---------------------------------------------------------------------------
# AutoDreamer
# ---------------------------------------------------------------------------


class AutoDreamer:
    """把 session messages dream 成 :class:`SessionMemory`,存進 store。

    Args:
        llm_caller: 注入式 LLM 呼叫器(吃 prompt,回 raw text)。test 用 mock。
        store: SessionMemory 落地 store(P1-8 protocol)。
        prompt_template: dream prompt(可 override);預設用 :data:`DREAM_PROMPT_TEMPLATE`。
        model_name: metadata,只寫進 :class:`SessionMemory.model`,不影響呼叫。
    """

    def __init__(
        self,
        llm_caller: LlmCaller,
        store: SessionMemoryStore,
        *,
        prompt_template: str = DREAM_PROMPT_TEMPLATE,
        model_name: str | None = None,
    ) -> None:
        self._llm_caller = llm_caller
        self._store = store
        self._prompt_template = prompt_template
        self._model_name = model_name

    async def dream(
        self,
        session_messages: list[Message],
        *,
        session_id: str,
        started_at: str | None = None,
    ) -> SessionMemory:
        """把一段 session messages 抽成 :class:`SessionMemory` 並存檔。

        Args:
            session_messages: P1-6 ``Message`` dict 列表。
            session_id: 對齊 P1-3 ``SessionContext.session_id``。
            started_at: session 起始 ISO timestamp(可 None,store 後也允許)。

        Returns:
            新建立(或既有 hash 相符的)SessionMemory。
        """
        conv_hash = compute_conversation_hash(session_messages)

        existing = self._store.load(session_id)
        if existing is not None and existing.conversation_hash == conv_hash:
            logger.debug(
                "AutoDreamer.dream skip: session_id=%s already dreamed with same hash",
                session_id,
            )
            return existing

        prompt = self._render_prompt(
            session_id=session_id,
            messages=session_messages,
        )

        try:
            raw_response = await self._llm_caller(prompt)
        except Exception as exc:
            logger.warning("AutoDreamer.dream LLM call failed: %s", exc)
            raw_response = ""

        parsed = _parse_dream_response(raw_response)
        memory = SessionMemory(
            session_id=session_id,
            summary=parsed["summary"],
            topics=tuple(parsed["topics"]),
            lessons=tuple(parsed["lessons"]),
            started_at=started_at,
            ended_at=_utcnow_iso(),
            message_count=len(session_messages),
            conversation_hash=conv_hash,
            model=self._model_name,
        )
        self._store.save(memory)
        return memory

    def dream_in_background(
        self,
        session_messages: list[Message],
        *,
        session_id: str,
        started_at: str | None = None,
    ) -> asyncio.Task[SessionMemory]:
        """在背景跑 :meth:`dream`,回傳 :class:`asyncio.Task` handle。

        caller 可選 ``await task``(等完成)或忽略 task(fire-and-forget)。
        例外會在 task callback 內 log,不冒泡到主 flow。
        """
        task: asyncio.Task[SessionMemory] = asyncio.create_task(
            self.dream(
                session_messages,
                session_id=session_id,
                started_at=started_at,
            ),
            name=f"auto-dream-{session_id}",
        )
        task.add_done_callback(_log_task_error)
        return task

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _render_prompt(self, *, session_id: str, messages: list[Message]) -> str:
        transcript = _render_transcript(messages)
        return self._prompt_template.format(
            session_id=session_id,
            message_count=len(messages),
            transcript=transcript,
        )


# ---------------------------------------------------------------------------
# parse / render helpers
# ---------------------------------------------------------------------------


_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.+?)\s*```", re.DOTALL)


def _parse_dream_response(raw: str) -> dict[str, list[str] | str]:
    """嚴格解析 dream JSON;壞 JSON 退化成 empty summary。

    寬鬆處理:
    - 容忍 LLM 包了 ``` ```json fence ``` ``(雖然 prompt 禁止,model 偶爾會破戒)。
    - 容忍多餘前後 prose — 找第一個 ``{`` 到最後一個 ``}`` 區間。
    - topics / lessons 不是 list 就丟棄(回空 list)。
    """
    text = (raw or "").strip()
    if not text:
        return {"summary": "", "topics": [], "lessons": []}

    # 1) 嘗試剝 fence
    fence_match = _JSON_FENCE_RE.search(text)
    if fence_match:
        text = fence_match.group(1).strip()

    # 2) 找第一個 { 到最後一個 }
    first = text.find("{")
    last = text.rfind("}")
    if first == -1 or last == -1 or last <= first:
        return {"summary": "", "topics": [], "lessons": []}

    candidate = text[first : last + 1]
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        return {"summary": "", "topics": [], "lessons": []}

    if not isinstance(parsed, dict):
        return {"summary": "", "topics": [], "lessons": []}

    summary = parsed.get("summary")
    if not isinstance(summary, str):
        summary = ""

    topics_raw = parsed.get("topics")
    topics: list[str] = []
    if isinstance(topics_raw, list):
        topics = [str(t).strip() for t in topics_raw if str(t).strip()]

    lessons_raw = parsed.get("lessons")
    lessons: list[str] = []
    if isinstance(lessons_raw, list):
        lessons = [str(t).strip() for t in lessons_raw if str(t).strip()]

    return {"summary": summary.strip(), "topics": topics, "lessons": lessons}


def _render_transcript(messages: list[Message]) -> str:
    """把 message list 渲染成 prompt 內的 transcript 區塊。

    格式:``[role] content``;content 為 list of block 時取 text part。
    """
    lines: list[str] = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role", "?")
        content = msg.get("content", "")
        if isinstance(content, list):
            text_parts: list[str] = []
            for block in content:
                if isinstance(block, dict) and isinstance(block.get("text"), str):
                    text_parts.append(block["text"])
            content_str = " ".join(text_parts)
        else:
            content_str = str(content)
        # 避免單條 message 太長拖垮 prompt — 截 4000 chars。
        if len(content_str) > 4000:
            content_str = content_str[:4000] + "...(truncated)"
        lines.append(f"[{role}] {content_str}")
    return "\n".join(lines)


def _log_task_error(task: asyncio.Task[object]) -> None:
    """background task done callback — 例外只 log,不冒泡。"""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.warning("AutoDreamer background task failed: %s", exc)


def _utcnow_iso() -> str:
    """測試 friendly 的 UTC ISO timestamp。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
