"""HookContext 三層 scope (Session / Turn / Operation) — P1-3。

本模組對應 enhancement roadmap P1-3:把 hook 執行時可看到的「狀態 / metadata」依
**時間生命週期** 切成三層 scope,讓 Inspect / Decide / Transform hook 可以根據自己
關心的層級取對應資料,而不是統統塞到一個 god-object dict 裡。

設計參考 `google.antigravity.hooks.hooks` (上游 `hooks.py:34-86`) 的 SessionContext /
TurnContext / OperationContext 繼承結構,但加上:

* `dataclass` 嚴格欄位 (start/end time、id、index),不是純 key/value store。
* `start_turn()` / `start_operation()` 是 `contextmanager`,自動記錄 start / end 時間,
  避免 hook 作者忘記 `end_turn()` 而 leak 狀態。
* 與 P0-3 `AnilaToolContext` 整合 —— OperationContext 內可選地嵌入 `tool_context`,
  讓 hook 想要拿 workspace / file_state_cache 也直接走 `ctx.operation.tool_context`。

三層 scope 對應 agent 執行不同層級:

1. **SessionContext** —— 整個 conversation session (跨多個 turn 共用)
   * `session_id` / `user_id` / `started_at`
   * `metadata: dict` —— session-wide 自訂 metadata (相當於 ant. SessionContext._store)
2. **TurnContext** —— 一個 user prompt → final response (同 turn 內多 tool call 共用)
   * 嵌在 SessionContext 內,可 `turn.session` 反向取 parent
   * `turn_id` / `turn_index` / `started_at` / `messages_in_this_turn`
3. **OperationContext** —— 一個具體 operation (tool call / llm call / compaction)
   * 嵌在 TurnContext 內,可 `op.turn` / `op.session` 反向取 parent
   * `operation_id` / `operation_type` / `started_at`
   * 可選 `tool_context: AnilaToolContext` —— 沿用 P0-3 結構

`HookContext` 是「三層整合的 wrapper」,給 hook 用 `ctx.session` / `ctx.turn` /
`ctx.operation` 取對應層級的資料;若 hook 跑在沒有 operation 的階段 (例如 SessionStart),
`ctx.operation` 可以是 None,呼叫端要自行檢查。

跟 P0-5 `HookExecutor` 的整合:`HookExecutor` 的 `Ctx` 泛型本來就接受任何型別,因此
`HookContext` 直接拿來當 Ctx 即可使用;本模組另外提供 `AnyHookCtx` type alias,讓寫
hook 的人有個直接的型別簽章可用。
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

from anila_agent.core.context import AnilaToolContext

# ---------------------------------------------------------------------------
# OperationContext (最內層 scope)
# ---------------------------------------------------------------------------


@dataclass
class OperationContext:
    """一次具體 operation 的 scope。

    Operation 是「最內層」的 scope,代表 agent 跑出來的某一個原子動作,例如:

    * `operation_type="tool_call"` —— 一次工具呼叫 (e.g. `read_file`)
    * `operation_type="llm_call"` —— 一次對 LLM 的 inference
    * `operation_type="compaction"` —— context window 收斂

    Attributes:
        operation_id: operation 的唯一識別字 (建議用 uuid4)。
        operation_type: operation 種類字串,自由欄位 (e.g. `"tool_call"`)。
        started_at: operation 開始 (epoch seconds, monotonic 為輔助)。
        ended_at: operation 結束,結束前為 None。
        metadata: operation 範圍 user metadata,Inspect/Decide/Transform 可自由
            寫入但要記得這層生命週期一結束就被丟掉。
        tool_context: 可選的 P0-3 `AnilaToolContext`。若本 operation 是 tool call,
            建議 attach 上來,讓 hook 拿 workspace / file_state_cache 不用另外查表。
        turn: 反向指回 parent TurnContext。建構時可給 None (例如獨立測試),正常
            執行期都會由 `TurnContext.start_operation()` 自動串好。
    """

    operation_id: str
    operation_type: str
    started_at: float = field(default_factory=time.time)
    ended_at: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    tool_context: AnilaToolContext | None = None
    turn: TurnContext | None = None

    # ---- session 反查 ----------------------------------------------------

    @property
    def session(self) -> SessionContext | None:
        """直接從 operation 反查 session,等於 `self.turn.session`。"""
        if self.turn is None:
            return None
        return self.turn.session

    # ---- 生命週期 --------------------------------------------------------

    def mark_ended(self, *, ended_at: float | None = None) -> None:
        """標記 operation 結束。多次呼叫只在第一次寫入 `ended_at`,避免被覆蓋。"""
        if self.ended_at is None:
            self.ended_at = ended_at if ended_at is not None else time.time()

    @property
    def duration(self) -> float | None:
        """operation 持續時間 (秒);若未結束則回 None。"""
        if self.ended_at is None:
            return None
        return self.ended_at - self.started_at


# ---------------------------------------------------------------------------
# TurnContext (中層 scope)
# ---------------------------------------------------------------------------


@dataclass
class TurnContext:
    """一個 turn (user prompt → final response) 的 scope。

    Turn 是「中層」scope,可包多個 operation (例如一次回答內呼叫 3 個 tool)。

    Attributes:
        turn_id: turn 的唯一識別字 (建議用 uuid4)。
        turn_index: 在 session 內第幾個 turn (從 0 開始)。
        started_at: turn 開始時間。
        ended_at: turn 結束時間,結束前為 None。
        messages_in_this_turn: 本 turn 收到 / 送出的訊息序列;格式由呼叫端決定
            (例如 `[{"role": "user", "content": ...}, ...]`)。
        metadata: turn 範圍 user metadata。
        session: 反向指回 parent SessionContext。
    """

    turn_id: str
    turn_index: int
    started_at: float = field(default_factory=time.time)
    ended_at: float | None = None
    messages_in_this_turn: list[Any] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    session: SessionContext | None = None

    # ---- 生命週期 --------------------------------------------------------

    def mark_ended(self, *, ended_at: float | None = None) -> None:
        """標記 turn 結束。多次呼叫只在第一次寫入。"""
        if self.ended_at is None:
            self.ended_at = ended_at if ended_at is not None else time.time()

    @property
    def duration(self) -> float | None:
        """turn 持續時間 (秒);若未結束則回 None。"""
        if self.ended_at is None:
            return None
        return self.ended_at - self.started_at

    # ---- 子層 builder ---------------------------------------------------

    @contextlib.contextmanager
    def start_operation(
        self,
        operation_type: str,
        *,
        operation_id: str | None = None,
        tool_context: AnilaToolContext | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Iterator[OperationContext]:
        """以 context manager 開啟一個 operation scope,結束自動 `mark_ended`。

        用法:

        ```python
        with turn.start_operation("tool_call", tool_context=tc) as op:
            ...  # 跑 hook / tool
        # 離開 with 之後 op.ended_at 已被自動填上。
        ```

        Args:
            operation_type: operation 種類,例 `"tool_call"` / `"llm_call"`。
            operation_id: 自訂 id;不給就生 uuid4。
            tool_context: 可選 P0-3 `AnilaToolContext`,讓 hook 可拿 workspace。
            metadata: 初始 metadata dict。
        """
        op = OperationContext(
            operation_id=operation_id or uuid.uuid4().hex,
            operation_type=operation_type,
            tool_context=tool_context,
            metadata=dict(metadata) if metadata else {},
            turn=self,
        )
        try:
            yield op
        finally:
            op.mark_ended()


# ---------------------------------------------------------------------------
# SessionContext (最外層 scope)
# ---------------------------------------------------------------------------


@dataclass
class SessionContext:
    """整個 conversation session 的 scope (最外層)。

    Session 是最外層 scope,跨多個 turn 共用。典型用途:

    * 存 correlation id / trace id 給整 session 用
    * 存 user identity / agent identity
    * 跨 turn 累計 metric (例如 total_tokens、total_cost)

    Attributes:
        session_id: session 的唯一識別字。
        user_id: 可選 user id (對應到外部系統的使用者)。
        started_at: session 開始時間。
        ended_at: session 結束時間,結束前為 None。
        metadata: session 範圍 user metadata。
        message_queue: P1-4 trigger 注入 agent 的訊息佇列。trigger callback
            內呼叫 :meth:`queue_message` 把訊息塞進來,agent loop 下次 turn
            開始時 ``message_queue.get_nowait()`` 拉出來 prepend 給 LLM。
            預設為空 asyncio.Queue。
    """

    session_id: str
    user_id: str | None = None
    started_at: float = field(default_factory=time.time)
    ended_at: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    message_queue: asyncio.Queue[dict[str, Any]] = field(
        default_factory=asyncio.Queue, repr=False
    )

    # ---- 生命週期 --------------------------------------------------------

    def mark_ended(self, *, ended_at: float | None = None) -> None:
        """標記 session 結束。多次呼叫只在第一次寫入。"""
        if self.ended_at is None:
            self.ended_at = ended_at if ended_at is not None else time.time()

    @property
    def duration(self) -> float | None:
        """session 持續時間 (秒);若未結束則回 None。"""
        if self.ended_at is None:
            return None
        return self.ended_at - self.started_at

    # ---- P1-4 trigger 注入訊息 -----------------------------------------

    def queue_message(
        self,
        content: str,
        *,
        role: str = "system",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """把一則訊息塞進 session message queue,給 agent 下次 turn 帶入。

        典型呼叫者:P1-4 Trigger callback,例如 PeriodicTrigger 偵測到
        vLLM 異常後呼叫 ``ctx.session.queue_message("[health] vllm down")``。

        實作上使用 :meth:`asyncio.Queue.put_nowait`,因為 trigger callback
        在 agent loop 同一個 event loop 內、queue 沒有 maxsize 限制,所以
        理論上 put_nowait 不會丟例外。若 user 自行加 maxsize 並滿了,
        ``put_nowait`` 會丟 ``QueueFull``,呼叫端要自行處理。

        Args:
            content: 訊息內容。
            role: chat role,預設 ``"system"`` (trigger 來源訊息通常標 system)。
            metadata: 額外 metadata (例如 trigger 名稱 / timestamp)。

        Returns:
            None。queue 內容格式為
            ``{"role": role, "content": content, "metadata": metadata}``。
        """
        self.message_queue.put_nowait(
            {
                "role": role,
                "content": content,
                "metadata": dict(metadata) if metadata else {},
            }
        )

    def drain_messages(self) -> list[dict[str, Any]]:
        """把目前 queue 內所有訊息一次拉光,回傳 list (FIFO 順序)。

        agent runner 在 turn 開始前呼叫,把這批訊息 prepend 給 LLM 看到。
        非阻塞:queue 空就回空 list。
        """
        drained: list[dict[str, Any]] = []
        while True:
            try:
                drained.append(self.message_queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        return drained

    # ---- 子層 builder ---------------------------------------------------

    @contextlib.contextmanager
    def start_turn(
        self,
        *,
        turn_id: str | None = None,
        turn_index: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Iterator[TurnContext]:
        """以 context manager 開啟一個 turn scope,結束自動 `mark_ended`。

        Args:
            turn_id: 自訂 turn id;不給就生 uuid4。
            turn_index: 自訂 turn 序號;不給就用 `len(self._turn_history)`,
                依本 session 已建立過幾個 turn 自動遞增。
            metadata: 初始 metadata dict。

        Yields:
            TurnContext: 開啟的 turn 物件,離開 with 自動 mark_ended。
        """
        idx = turn_index if turn_index is not None else self._next_turn_index
        turn = TurnContext(
            turn_id=turn_id or uuid.uuid4().hex,
            turn_index=idx,
            metadata=dict(metadata) if metadata else {},
            session=self,
        )
        self._turn_history.append(turn)
        try:
            yield turn
        finally:
            turn.mark_ended()

    # ---- 內部紀錄 --------------------------------------------------------

    # dataclass field 但加底線表示「內部用」;暴露給呼叫端只透過 helper。
    _turn_history: list[TurnContext] = field(default_factory=list, repr=False)

    @property
    def _next_turn_index(self) -> int:
        """下一個 turn 在 session 內的 index (0-based)。"""
        return len(self._turn_history)

    @property
    def turn_history(self) -> list[TurnContext]:
        """回傳本 session 已建立過的 turn (含已結束)。淺拷貝避免外部 mutate。"""
        return list(self._turn_history)


# ---------------------------------------------------------------------------
# HookContext —— 三層整合的 wrapper
# ---------------------------------------------------------------------------


@dataclass
class HookContext:
    """三層 scope 的整合 wrapper,給 hook 用 `ctx.session/turn/operation` 直接取。

    這個 wrapper 是 ergonomics 用:hook 寫起來可以 `ctx.session.metadata[...]` /
    `ctx.operation.tool_context.workspace` 等,而不必自己 traverse `op.turn.session`。

    Lifecycle 約定:

    * `session` 一定要有 —— hook 不可能在無 session 的情境下執行。
    * `turn` 可選 —— 在 session-level hook (例如 OnSessionStart) 階段可以 None。
    * `operation` 可選 —— 在 turn-level hook (例如 PreTurn / PostTurn) 階段可以 None。

    便利方法:

    * `HookContext.from_operation(op)` —— 從 OperationContext 反推三層。
    * `HookContext.from_turn(turn)` —— 從 TurnContext 反推 session,operation 給 None。
    * `HookContext.from_session(sess)` —— turn / operation 皆 None。

    與 P0-5 HookExecutor 的整合:`HookExecutor` 的 `Ctx` 泛型可直接帶 `HookContext`。
    """

    session: SessionContext
    turn: TurnContext | None = None
    operation: OperationContext | None = None

    # ---- 建構 helper ----------------------------------------------------

    @classmethod
    def from_session(cls, session: SessionContext) -> HookContext:
        """只有 session,turn / operation 皆 None。用於 OnSessionStart / OnSessionEnd。"""
        return cls(session=session)

    @classmethod
    def from_turn(cls, turn: TurnContext) -> HookContext:
        """從 TurnContext 反推 session。operation 仍為 None,用於 PreTurn / PostTurn。"""
        if turn.session is None:
            raise ValueError("TurnContext.session is None — cannot derive HookContext")
        return cls(session=turn.session, turn=turn)

    @classmethod
    def from_operation(cls, op: OperationContext) -> HookContext:
        """從 OperationContext 反推 turn / session。三層皆 non-None。"""
        if op.turn is None:
            raise ValueError("OperationContext.turn is None — cannot derive HookContext")
        if op.turn.session is None:
            raise ValueError(
                "OperationContext.turn.session is None — cannot derive HookContext"
            )
        return cls(session=op.turn.session, turn=op.turn, operation=op)

    # ---- 便利 accessor --------------------------------------------------

    @property
    def tool_context(self) -> AnilaToolContext | None:
        """直接拿 `operation.tool_context`;若 operation 是 None 也回 None。"""
        if self.operation is None:
            return None
        return self.operation.tool_context


# ---------------------------------------------------------------------------
# Type alias / public API
# ---------------------------------------------------------------------------


# 給 hook 作者用的 type alias —— hook 函式可標 `ctx: AnyHookCtx`,
# 表示「我接受任一形態的 HookContext (可能 operation 為 None 等)」。
AnyHookCtx = HookContext


__all__ = [
    "AnyHookCtx",
    "HookContext",
    "OperationContext",
    "SessionContext",
    "TurnContext",
]
