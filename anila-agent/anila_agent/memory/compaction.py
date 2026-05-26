"""P1-6 CompactingSession 與三層 compactor 策略。

長對話超過 LLM context window 時,需在送 model 前「壓縮」較舊的 message。本
模組提供三種策略 + 一個 ``Session`` decorator,對齊 openai-agents 的
``CompactingSession`` 想法(`templete/openai-agents-python/src/agents/memory/
openai_responses_compaction_session.py`)與 claude-code 的 microcompact /
autoCompact / snip 分層(`docs/agent-framework/claude-code-src-deep-dive.md`
§4.12)。

設計重點
--------
- ``Message`` 採 ``dict[str, Any]`` 型別別名,**不綁** openai-agents 的
  ``TResponseInputItem``。理由:本模組同時要服務 LiteLLM / 自家 model
  callsite,泛型 dict 才能 cover Chat Completion / Responses 兩種 wire format。
  壓縮邏輯只看 ``role``、``content``、``type``、``tool_call_id`` 幾個 key,
  其他欄位 passthrough。
- 三個 compactor 都是 **pure function-like**(吃 list、回新 list),不 mutate 輸入。
- ``CompactingSession`` 是 thin wrapper,將既有 ``Session`` 包起來,只在 read
  path(``get_items``)套 compactor。write path passthrough — 因為 compaction
  屬「呈現給 LLM 的視圖」,raw 歷史應保留在底層 store 以便 audit / replay。
- tracing(P0-9)為**可選依賴**:若呼叫端塞 ``Tracer``,每次 compact 開
  ``memory.compact.<name>`` span 並寫入 before / after token / messages_dropped
  屬性;若 ``None`` 則完全略過,不影響主流程。
- 不引入新 dep — token 估算用「rough char/4」啟發式,LLM call 用 mock
  /test injection,真實 model wiring 留給 runner 接點。
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from typing import Any, Literal

from anila_agent.tracing import Tracer

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 型別別名
# ---------------------------------------------------------------------------


# Message 採用最寬鬆的 dict 型別 — 同時相容 openai-agents 的
# ``TResponseInputItem``(本身也是 TypedDict / dict 結構)與 LiteLLM 的 Chat
# Completion shape(``{"role": ..., "content": ...}``)。compactor 內部
# 不假設 schema,只看 well-known key。
Message = dict[str, Any]


CompactStrategy = Literal["pre_send", "post_recv"]
"""壓縮觸發時機策略。

- ``pre_send``(預設):read 時(送 LLM 前)動態 compact,raw 歷史不動。
- ``post_recv``:write 時(收 LLM 後)立即 compact 並覆寫底層 store。

本模組目前實作 ``pre_send`` — ``post_recv`` 保留語意位但未啟用 mutation,
理由是「raw 歷史保留」對 debug / replay 較友善;若未來需要 post_recv,
只要在 ``add_items`` 後接一個 compact + clear + re-add 流程即可。
"""


# ---------------------------------------------------------------------------
# 觀測用 dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CompactionStats:
    """單次 compact 的觀測 metric。

    供 tracing span attribute 與測試直接驗證。
    """

    compactor_name: str
    before_messages: int
    after_messages: int
    before_tokens: int
    after_tokens: int
    messages_dropped: int

    def to_attributes(self) -> dict[str, Any]:
        """轉成 ``Span.attributes`` dict(key 對齊 OTel-ish 命名)。"""
        return {
            "compactor.name": self.compactor_name,
            "compactor.before_messages": self.before_messages,
            "compactor.after_messages": self.after_messages,
            "compactor.before_tokens": self.before_tokens,
            "compactor.after_tokens": self.after_tokens,
            "compactor.messages_dropped": self.messages_dropped,
        }


# ---------------------------------------------------------------------------
# token 估算啟發式
# ---------------------------------------------------------------------------


def _estimate_message_tokens(msg: Message) -> int:
    """粗估單一 message 的 token 數。

    GPT-4 系列實測 token≈char/4(英文);中文更密但 prod 環境 vLLM gemma4
    的 tokenizer 也接近此比例。本模組只用「相對大小比較」決定 compactor
    觸發條件,精確 token count 留給呼叫端傳入 ``usage`` 字典。
    """
    content = msg.get("content")
    if isinstance(content, str):
        return max(1, len(content) // 4)
    if isinstance(content, list):
        # multipart content (e.g. Responses input_text + input_image)
        total = 0
        for part in content:
            if isinstance(part, dict):
                text = part.get("text") or part.get("input_text") or ""
                if isinstance(text, str):
                    total += len(text) // 4
        return max(1, total)
    return 1


def estimate_total_tokens(messages: Iterable[Message]) -> int:
    """sum tokens 給呼叫端做 threshold 判斷。"""
    return sum(_estimate_message_tokens(m) for m in messages)


async def _as_coroutine(awaitable: Awaitable[str]) -> str:
    """把任意 ``Awaitable[str]`` 包成 coroutine。

    ``asyncio.run`` 與 ``await`` 對 generic Awaitable 的 mypy narrowing 不夠
    精確,wrap 一層 coroutine 讓型別系統與 runtime 都安心。
    """
    return await awaitable


# ---------------------------------------------------------------------------
# Compactor 抽象
# ---------------------------------------------------------------------------


class CompactorABC(ABC):
    """所有 compactor 的共同介面。

    ``name`` 用於 tracing span 命名與 stats 標記;子類請覆寫成可辨識字串
    (例:``"micro"``、``"llm_summary"``、``"snip"``)。
    """

    name: str = "compactor"

    @abstractmethod
    def compact(self, messages: list[Message]) -> list[Message]:
        """回傳壓縮後的 message list(不 mutate 輸入)。"""


# ---------------------------------------------------------------------------
# 1. MicroCompactor — 砍掉舊 tool 互動細節
# ---------------------------------------------------------------------------


# 我們把以下 type 視為「tool 互動細節」 — 可被摘要替換:
_TOOL_DETAIL_TYPES: frozenset[str] = frozenset(
    {
        "function_call",
        "function_call_output",
        "tool_call",
        "tool_result",
        "tool",
        "computer_call",
        "computer_call_output",
        "file_search_call",
        "web_search_call",
        "reasoning",
    }
)


def _is_tool_detail(msg: Message) -> bool:
    """判斷 message 是否為 tool 互動 detail(會被 micro-compact 收掉)。

    判斷規則對齊 openai-agents Responses API 與 Chat Completion 兩種 shape:
    - ``type`` 落在 ``_TOOL_DETAIL_TYPES`` → 是
    - ``role == "tool"`` 或 ``role == "function"`` → 是
    - 其餘(user / assistant / system message)→ 否
    """
    if not isinstance(msg, dict):
        return False
    msg_type = msg.get("type")
    if isinstance(msg_type, str) and msg_type in _TOOL_DETAIL_TYPES:
        return True
    role = msg.get("role")
    return role in {"tool", "function"}


class MicroCompactor(CompactorABC):
    """保留最近 ``retain_last_n`` 個 message,把更早的 tool 互動細節摘成 placeholder。

    對齊 claude-code ``microCompact.ts``:不動 user / assistant 主訊息,
    只壓 tool_call / tool_result。這樣保留對話「敘事」的同時釋出大量舊
    tool result 的 token 佔用(read tool 結果常吃幾千 token)。

    Args:
        retain_last_n: 最近 N 則完整保留(不論 type)。N=10 與 claude-code
            預設 microcompact threshold 一致。
    """

    name = "micro"

    def __init__(self, retain_last_n: int = 10) -> None:
        if retain_last_n < 0:
            raise ValueError("retain_last_n must be non-negative")
        self.retain_last_n = retain_last_n

    def compact(self, messages: list[Message]) -> list[Message]:
        if len(messages) <= self.retain_last_n:
            return list(messages)

        cutoff = len(messages) - self.retain_last_n
        older, recent = messages[:cutoff], messages[cutoff:]

        kept: list[Message] = []
        dropped_tool_details = 0
        for m in older:
            if _is_tool_detail(m):
                dropped_tool_details += 1
                continue
            kept.append(dict(m))  # shallow copy,避免下游 mutation 反汙染

        if dropped_tool_details > 0:
            # 用 system message 留下「這裡被壓掉了 X 個 tool 互動」標記,讓
            # 後續 LLM call 知道 conversation 有 gap(避免 hallucinate 找不
            # 到的 tool_call_id)。
            kept.append(
                {
                    "role": "system",
                    "content": f"[已壓縮 {dropped_tool_details} 個 tool 互動]",
                }
            )

        kept.extend(dict(m) for m in recent)
        return kept


# ---------------------------------------------------------------------------
# 2. LlmSummaryCompactor — 觸 threshold 後 call LLM 摘要中間段
# ---------------------------------------------------------------------------


SummaryFn = Callable[[list[Message]], Awaitable[str]] | Callable[[list[Message]], str]
"""LLM 摘要呼叫的可注入函式。

兼用 sync / async,internal helper 會以 ``inspect.iscoroutine`` 偵測。
測試用 mock 通常傳 sync function;真實 runner 接 LiteLLM async client。
"""


class LlmSummaryCompactor(CompactorABC):
    """token 達 threshold 時,把中段 message 摘成單一 system summary。

    保留:
    - 前 ``preserve_first_n`` 則(通常含 system prompt、user 初始 task)。
    - 後 ``preserve_last_n`` 則(最近上下文)。
    - 中間段 → 一條 ``{"role": "system", "content": "[對話摘要] ..."}``。

    Args:
        summarize_fn: 接 messages 回 summary 字串(可 sync 可 async);本模
            組**不**直接 import openai-agents Runner,以避免 prod 環境依賴
            漏配時 import 階段就炸掉。runner 接點負責 wrap 真 LLM call。
        threshold_tokens: token 估算超過此值才觸發。預設 8000(與大多數
            32k context 的安全水位對齊)。
        preserve_first_n: 開頭保留 N 則(預設 2 = system + 首個 user)。
        preserve_last_n: 結尾保留 N 則(預設 4)。

    Note:
        本 task 寫 interface 用 mock 測,真實 LLM call 等 runner 整合 wire up。
        ``summarize_fn`` 設計成可注入即代表這點。
    """

    name = "llm_summary"

    def __init__(
        self,
        summarize_fn: SummaryFn,
        *,
        threshold_tokens: int = 8000,
        preserve_first_n: int = 2,
        preserve_last_n: int = 4,
    ) -> None:
        if threshold_tokens <= 0:
            raise ValueError("threshold_tokens must be positive")
        if preserve_first_n < 0 or preserve_last_n < 0:
            raise ValueError("preserve_* must be non-negative")
        self._summarize_fn = summarize_fn
        self.threshold_tokens = threshold_tokens
        self.preserve_first_n = preserve_first_n
        self.preserve_last_n = preserve_last_n

    def compact(self, messages: list[Message]) -> list[Message]:
        """sync 入口 — 內部處理 sync / async summarize_fn 兩種。

        Note:
            為了符合 CompactorABC 的 sync 介面,async summarize_fn 會用
            ``asyncio.run`` 跑;這只在無 active loop 時可用。在 runner 內
            常已有 loop 跑著 — 預期上層改呼叫 :meth:`acompact`(async 版)。
            本 task 沒被 wire 進 runner,sync path 對單元測試與
            sync caller(例如 CLI 一次性 inspect)夠用。
        """
        import asyncio
        import inspect

        if not self._should_summarize(messages):
            return list(messages)

        result = self._summarize_fn(self._middle_slice(messages))
        if inspect.isawaitable(result):
            # 注意:sync compact 在已有 event loop 的 caller 內呼叫會炸;此 path
            # 僅供 sync caller(CLI inspect / 單元測試)使用,async caller 請走
            # ``acompact``。
            summary: str = asyncio.run(_as_coroutine(result))
        else:
            summary = result
        return self._apply_summary(messages, summary)

    async def acompact(self, messages: list[Message]) -> list[Message]:
        """async 入口 — runner 整合時走這條,避免 nested loop 問題。"""
        import inspect

        if not self._should_summarize(messages):
            return list(messages)

        result = self._summarize_fn(self._middle_slice(messages))
        summary: str = await _as_coroutine(result) if inspect.isawaitable(result) else result
        return self._apply_summary(messages, summary)

    # ------------------------------------------------------------------
    # helpers(私有 — 兩個入口共用)
    # ------------------------------------------------------------------
    def _should_summarize(self, messages: list[Message]) -> bool:
        if estimate_total_tokens(messages) < self.threshold_tokens:
            return False
        # 開頭 + 結尾保留範圍已涵蓋所有 message,沒有「中段」可摘要。
        return len(messages) > self.preserve_first_n + self.preserve_last_n

    def _middle_slice(self, messages: list[Message]) -> list[Message]:
        end = len(messages) - self.preserve_last_n
        return messages[self.preserve_first_n : end]

    def _apply_summary(self, messages: list[Message], summary: str) -> list[Message]:
        head = [dict(m) for m in messages[: self.preserve_first_n]]
        tail = [dict(m) for m in messages[len(messages) - self.preserve_last_n :]]
        summary_msg: Message = {
            "role": "system",
            "content": f"[對話摘要] {summary}",
        }
        return [*head, summary_msg, *tail]


# ---------------------------------------------------------------------------
# 3. SnipCompactor — 對單一 message 切中間
# ---------------------------------------------------------------------------


class SnipCompactor(CompactorABC):
    """對「太長的單一 message」中間挖洞,留頭尾。

    對齊 claude-code ``snipCompact.ts``:常見場景是 tool 回傳一個超大檔
    (例如整份 log)塞進 history;snip 把中間切掉,留 head/tail 給 LLM 有
    context 推斷內容類型,同時保留 token budget。

    僅處理 ``content`` 為 string 的 message;multipart(list)或 None 不動。

    Args:
        max_chars_per_message: 單則 content 字元上限。超過則中間用
            ``"[...snipped X chars...]"`` 替代。
    """

    name = "snip"

    def __init__(self, max_chars_per_message: int = 4000) -> None:
        if max_chars_per_message < 100:
            raise ValueError("max_chars_per_message must be >= 100 to retain context")
        self.max_chars_per_message = max_chars_per_message

    def compact(self, messages: list[Message]) -> list[Message]:
        out: list[Message] = []
        for msg in messages:
            out.append(self._snip_one(msg))
        return out

    def _snip_one(self, msg: Message) -> Message:
        content = msg.get("content")
        if not isinstance(content, str):
            return dict(msg)
        if len(content) <= self.max_chars_per_message:
            return dict(msg)

        # 預留切割標記長度,實際 head/tail 各取 (max - marker_len) / 2。
        marker_len_est = 40  # 預估 marker 含字數的長度,夠粗即可。
        side = max(50, (self.max_chars_per_message - marker_len_est) // 2)
        snipped_count = len(content) - 2 * side
        new_content = content[:side] + f"[...snipped {snipped_count} chars...]" + content[-side:]
        new_msg = dict(msg)
        new_msg["content"] = new_content
        return new_msg


# ---------------------------------------------------------------------------
# CompactingSession — chain compactors 套在既有 Session 上
# ---------------------------------------------------------------------------


@dataclass
class CompactingSession:
    """``Session`` decorator — chain 多個 compactor 在 read path 上動態套用。

    ``underlying`` 必須是符合 openai-agents ``Session`` Protocol 的物件
    (含 ``session_id``、``get_items``、``add_items``、``pop_item``、
    ``clear_session`` 5 個 async 方法)。

    Args:
        underlying: 底層 Session(例:``open_session(...)`` 拿到的
            ``SQLiteSession``,或任何自家實作)。
        compactors: 依序套用的 compactor list。後一個吃前一個的 output。
        strategy: ``pre_send`` 預設 — read 時動態 compact、底層 store 不動。
            ``post_recv`` 預留語意位,目前等同 ``pre_send``。
        tracer: 可選 P0-9 tracer。每次 compact 開 span 與 attribute。
    """

    underlying: Any  # Session protocol — 不用 import 避免循環依賴
    compactors: list[CompactorABC] = field(default_factory=list)
    strategy: CompactStrategy = "pre_send"
    tracer: Tracer | None = None

    # 觀測用:最近一次每個 compactor 跑出來的 stats(供測試與 debug)。
    last_stats: list[CompactionStats] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.strategy not in ("pre_send", "post_recv"):
            raise ValueError(f"unknown strategy: {self.strategy}")

    @property
    def session_id(self) -> str:
        """Session protocol 必要欄位 — 透給底層。"""
        sid = getattr(self.underlying, "session_id", None)
        return sid if isinstance(sid, str) else ""

    # ------------------------------------------------------------------
    # read path — apply compactors
    # ------------------------------------------------------------------
    async def get_items(self, limit: int | None = None) -> list[Message]:
        raw = await self.underlying.get_items(limit)
        # raw 來自 openai-agents 時是 TResponseInputItem(本身就是 dict);
        # 直接 cast 成 Message。
        messages: list[Message] = [dict(m) if isinstance(m, dict) else m for m in raw]

        for compactor in self.compactors:
            messages = self._run_with_tracing(compactor, messages)

        return messages

    async def add_items(self, items: list[Message]) -> None:
        """write passthrough — raw 歷史完整存底層,read 時才壓。"""
        await self.underlying.add_items(items)

    async def pop_item(self) -> Message | None:
        popped = await self.underlying.pop_item()
        return popped if popped is None or isinstance(popped, dict) else dict(popped)

    async def clear_session(self) -> None:
        await self.underlying.clear_session()
        self.last_stats = []

    # ------------------------------------------------------------------
    # internal — 跑單個 compactor 並開 tracing span
    # ------------------------------------------------------------------
    def _run_with_tracing(self, compactor: CompactorABC, messages: list[Message]) -> list[Message]:
        before_msgs = len(messages)
        before_tokens = estimate_total_tokens(messages)

        if self.tracer is not None:
            with self.tracer.start_span(f"memory.compact.{compactor.name}") as span:
                compacted = compactor.compact(messages)
                stats = _build_stats(compactor, messages, compacted)
                span.attributes.update(stats.to_attributes())
        else:
            compacted = compactor.compact(messages)
            stats = _build_stats(compactor, messages, compacted)

        self.last_stats.append(stats)
        logger.debug(
            "compact[%s]: messages %d->%d, tokens ~%d->~%d, dropped=%d",
            compactor.name,
            before_msgs,
            len(compacted),
            before_tokens,
            stats.after_tokens,
            stats.messages_dropped,
        )
        return compacted


# ---------------------------------------------------------------------------
# 輔助:由 before / after 算 stats
# ---------------------------------------------------------------------------


def _build_stats(
    compactor: CompactorABC,
    before: list[Message],
    after: list[Message],
) -> CompactionStats:
    before_tokens = estimate_total_tokens(before)
    after_tokens = estimate_total_tokens(after)
    return CompactionStats(
        compactor_name=compactor.name,
        before_messages=len(before),
        after_messages=len(after),
        before_tokens=before_tokens,
        after_tokens=after_tokens,
        messages_dropped=max(0, len(before) - len(after)),
    )


__all__ = [
    "CompactStrategy",
    "CompactingSession",
    "CompactionStats",
    "CompactorABC",
    "LlmSummaryCompactor",
    "Message",
    "MicroCompactor",
    "SnipCompactor",
    "SummaryFn",
    "estimate_total_tokens",
]
