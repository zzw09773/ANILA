"""P2-5 ConversationIndex — turn / compaction index 追蹤。

本模組對應 enhancement roadmap P2-5,從上游 antigravity SDK 的
``google.antigravity.conversation.conversation.Conversation`` 抽出兩個有用
特性,做成「**可選的、與 SessionContext 鬆耦合**」的記錄結構:

1. **Turn record** —— 每個 turn 在 conversation history 內對應的
   ``turn_index``、起訖時間、message index range、tool call 數、compaction 次數。
2. **Compaction record** —— 每次 compaction 發生在哪個 turn / message
   index、是哪個 compactor 跑的、壓掉多少訊息與 token。

這兩種紀錄合起來提供「conversation 層級的稽核 / replay 索引」,讓:

* debug 工具可以快速指到「第 3 個 turn 在 index 12-18,中間有兩次 compaction」。
* future replay loop 可以直接從某個 turn 的 message index 切回。
* 不破壞 P1-3 既有 ``SessionContext`` 的 lifecycle API —— 整個
  ``ConversationIndex`` 是「**optional field**」掛在 ``SessionContext`` 上,
  caller 不開啟就完全不付出開銷。

設計取捨
--------

- **獨立檔案** 而非塞進 ``hook_context.py``。Conversation index 與 hook /
  trace 主流程無關,放 memory/ 比較貼近語意(它紀錄的就是 conversation
  memory 視角)。
- **dataclass** 全 type annotation,frozen 與否視可變性需求而定:Turn /
  Compaction record 跑完就不該改 → ``frozen=True``。``ConversationIndex``
  本身要 append → 不 frozen,但 list 透過 ``record_*`` helper 控進出。
- **不引入 cyclic import**:本檔不從 ``core.hook_context`` import,反向是
  ``hook_context`` 把欄位 declare 成 ``ConversationIndex | None``,
  ``hook_context`` 內 ``TYPE_CHECKING`` import 本模組,避免 runtime cycle。
- **與 CompactingSession (P1-6) 整合**:不直接耦合,而是讓
  ``CompactingSession`` 接受一個 ``index_callback: Callable[..., None]``
  optional field;呼叫端把 ``index.record_compaction`` partial 進去即可。
  這樣 P1-6 既有 API 完全不破。
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# TurnRecord
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TurnRecord:
    """單一 turn 在 conversation index 內的紀錄。

    Frozen — 跑完就不該再改;若 turn 還沒結束(``ended_at is None``),caller
    應在 turn 結束時用 :meth:`ConversationIndex.finalize_turn` 產出新的 record
    覆寫,而非直接 mutate。

    Attributes:
        turn_id: 對應 :class:`anila_agent.core.hook_context.TurnContext.turn_id`。
        turn_index: turn 在 session 內第幾個 (0-based)。
        started_at: turn 開始時間 (epoch seconds)。
        ended_at: turn 結束時間;尚未結束則為 None。
        message_indices: 本 turn 涵蓋的 conversation history message 區間
            ``(start, end)``,半開區間 ``[start, end)``。如果 caller 沒記 message
            index 也可以留 ``(0, 0)`` 或 ``None``。
        tool_call_count: 本 turn 內 tool call 數 (含成功 / 失敗)。
        compaction_count: 本 turn 內觸發的 compaction 次數。
        metadata: turn 範圍 metadata copy(避免外部 mutate)。
    """

    turn_id: str
    turn_index: int
    started_at: float
    ended_at: float | None = None
    message_indices: tuple[int, int] | None = None
    tool_call_count: int = 0
    compaction_count: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def duration(self) -> float | None:
        """turn 持續時間 (秒);未結束則 None。"""
        if self.ended_at is None:
            return None
        return self.ended_at - self.started_at

    def to_dict(self) -> dict[str, Any]:
        """轉成可序列化 dict(JSON-safe)。"""
        return {
            "turn_id": self.turn_id,
            "turn_index": self.turn_index,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "message_indices": list(self.message_indices)
            if self.message_indices is not None
            else None,
            "tool_call_count": self.tool_call_count,
            "compaction_count": self.compaction_count,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TurnRecord:
        """從 :meth:`to_dict` 產出的 dict 還原 TurnRecord。"""
        mi_raw = data.get("message_indices")
        if mi_raw is None:
            message_indices: tuple[int, int] | None = None
        else:
            message_indices = (int(mi_raw[0]), int(mi_raw[1]))
        return cls(
            turn_id=str(data["turn_id"]),
            turn_index=int(data["turn_index"]),
            started_at=float(data["started_at"]),
            ended_at=float(data["ended_at"]) if data.get("ended_at") is not None else None,
            message_indices=message_indices,
            tool_call_count=int(data.get("tool_call_count", 0)),
            compaction_count=int(data.get("compaction_count", 0)),
            metadata=dict(data.get("metadata", {})),
        )


# ---------------------------------------------------------------------------
# CompactionRecord
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CompactionRecord:
    """單次 compaction 事件的紀錄。

    Frozen — 一旦觸發,結果不該再被改寫。對應 P1-6 ``CompactionStats``,但這
    個層級多了 ``at_turn_index`` 與 ``triggered_at``,讓 conversation 層級
    可以 cross-reference「第 N 個 turn 觸發了哪些 compaction」。

    Attributes:
        compaction_id: 該次 compaction 的唯一識別字(預設 uuid4 hex)。
        at_turn_index: 觸發 compaction 時所在的 turn index。如果 caller 在
            session-level 觸發(無 active turn)可給 ``-1``。
        compactor_name: 對應 P1-6 ``CompactorABC.name``。
        messages_dropped: 被壓掉的訊息數(``before - after``)。
        before_tokens: compact 前 token 估算。
        after_tokens: compact 後 token 估算。
        triggered_at: 觸發時間 (epoch seconds)。
    """

    compaction_id: str
    at_turn_index: int
    compactor_name: str
    messages_dropped: int
    before_tokens: int
    after_tokens: int
    triggered_at: float = field(default_factory=time.time)

    @property
    def tokens_saved(self) -> int:
        """估算節省下來的 token 數;若 after > before 回 0。"""
        return max(0, self.before_tokens - self.after_tokens)

    def to_dict(self) -> dict[str, Any]:
        """轉成可序列化 dict(JSON-safe)。"""
        return {
            "compaction_id": self.compaction_id,
            "at_turn_index": self.at_turn_index,
            "compactor_name": self.compactor_name,
            "messages_dropped": self.messages_dropped,
            "before_tokens": self.before_tokens,
            "after_tokens": self.after_tokens,
            "triggered_at": self.triggered_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CompactionRecord:
        """從 :meth:`to_dict` 產出的 dict 還原 CompactionRecord。"""
        return cls(
            compaction_id=str(data["compaction_id"]),
            at_turn_index=int(data["at_turn_index"]),
            compactor_name=str(data["compactor_name"]),
            messages_dropped=int(data["messages_dropped"]),
            before_tokens=int(data["before_tokens"]),
            after_tokens=int(data["after_tokens"]),
            triggered_at=float(data.get("triggered_at", 0.0)),
        )


# ---------------------------------------------------------------------------
# ConversationIndex
# ---------------------------------------------------------------------------


@dataclass
class ConversationIndex:
    """Conversation 層級的 turn / compaction 索引。

    嵌在 :class:`anila_agent.core.hook_context.SessionContext` 內(optional
    field)。caller 若要追蹤就在 session 建立時 attach 一個空 instance,然後在
    turn 開始 / 結束、compaction 發生時呼叫對應 record helper。

    Lifecycle 慣例:

    1. ``session = SessionContext(..., conversation_index=ConversationIndex())``
    2. turn 開始時:``index.start_turn(turn_id=..., turn_index=...)``
    3. tool call 發生時:``index.bump_tool_call(turn_index=...)``
    4. compaction 發生時:``index.record_compaction(...)`` —— 若 caller 提供
       turn_index 會自動把該 turn 的 ``compaction_count`` +1。
    5. turn 結束時:``index.finalize_turn(turn_index=..., ended_at=..., ...)``

    Attributes:
        turns: 已記錄的 turn record list,順序 == turn_index。
        compactions: 已記錄的 compaction record list,順序 == 觸發先後。
    """

    turns: list[TurnRecord] = field(default_factory=list)
    compactions: list[CompactionRecord] = field(default_factory=list)

    # ------------------------------------------------------------------
    # Turn 記錄
    # ------------------------------------------------------------------

    def start_turn(
        self,
        *,
        turn_id: str,
        turn_index: int,
        started_at: float | None = None,
        message_index_start: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> TurnRecord:
        """建立並 append 一個新的 ``TurnRecord``(尚未結束,``ended_at=None``)。

        若 ``turn_index`` 已存在於 ``turns`` 內,會丟 ``ValueError`` —— 同一
        index 不該被 record 兩次。

        Args:
            turn_id: 對應 TurnContext.turn_id。
            turn_index: turn 序號。
            started_at: 起始時間;不給就用 ``time.time()``。
            message_index_start: turn 開始時 message history 長度(供日後
                合成 ``message_indices`` 用)。先存到 metadata
                ``_message_index_start`` 鍵,等 :meth:`finalize_turn` 用。
            metadata: 初始 metadata。

        Returns:
            建立好的 TurnRecord(也已存到 ``self.turns``)。
        """
        if any(t.turn_index == turn_index for t in self.turns):
            raise ValueError(f"turn_index {turn_index} already recorded")

        meta = dict(metadata) if metadata else {}
        if message_index_start is not None:
            meta["_message_index_start"] = int(message_index_start)

        record = TurnRecord(
            turn_id=turn_id,
            turn_index=turn_index,
            started_at=started_at if started_at is not None else time.time(),
            ended_at=None,
            message_indices=None,
            tool_call_count=0,
            compaction_count=0,
            metadata=meta,
        )
        self.turns.append(record)
        return record

    def finalize_turn(
        self,
        *,
        turn_index: int,
        ended_at: float | None = None,
        message_index_end: int | None = None,
        extra_metadata: dict[str, Any] | None = None,
    ) -> TurnRecord:
        """把指定 turn_index 的 record 替換成「已結束」版本。

        因為 ``TurnRecord`` 是 frozen,所以這裡走「移除舊的 → 塞入新的」流
        程,順序保留(就地替換)。

        Args:
            turn_index: 要 finalize 的 turn。
            ended_at: 結束時間;不給就 ``time.time()``。
            message_index_end: turn 結束時 message history 長度;若 caller
                當初在 :meth:`start_turn` 有給 ``message_index_start``,這裡
                兩個合起來會被填到 ``message_indices`` tuple。
            extra_metadata: 額外 metadata 合併進去。

        Returns:
            更新後的新 TurnRecord。

        Raises:
            KeyError: 找不到該 turn_index。
        """
        idx_in_list, old = self._locate_turn(turn_index)

        meta = dict(old.metadata)
        if extra_metadata:
            meta.update(extra_metadata)

        message_indices: tuple[int, int] | None = old.message_indices
        if message_index_end is not None:
            start = int(meta.pop("_message_index_start", 0))
            message_indices = (start, int(message_index_end))

        new_record = TurnRecord(
            turn_id=old.turn_id,
            turn_index=old.turn_index,
            started_at=old.started_at,
            ended_at=ended_at if ended_at is not None else time.time(),
            message_indices=message_indices,
            tool_call_count=old.tool_call_count,
            compaction_count=old.compaction_count,
            metadata=meta,
        )
        self.turns[idx_in_list] = new_record
        return new_record

    def bump_tool_call(self, *, turn_index: int, delta: int = 1) -> TurnRecord:
        """把指定 turn 的 ``tool_call_count`` 加 ``delta`` (預設 +1)。

        Raises:
            KeyError: 找不到該 turn_index。
        """
        idx_in_list, old = self._locate_turn(turn_index)
        new_record = TurnRecord(
            turn_id=old.turn_id,
            turn_index=old.turn_index,
            started_at=old.started_at,
            ended_at=old.ended_at,
            message_indices=old.message_indices,
            tool_call_count=old.tool_call_count + delta,
            compaction_count=old.compaction_count,
            metadata=dict(old.metadata),
        )
        self.turns[idx_in_list] = new_record
        return new_record

    # ------------------------------------------------------------------
    # Compaction 記錄
    # ------------------------------------------------------------------

    def record_compaction(
        self,
        *,
        compactor_name: str,
        messages_dropped: int,
        before_tokens: int,
        after_tokens: int,
        at_turn_index: int = -1,
        compaction_id: str | None = None,
        triggered_at: float | None = None,
    ) -> CompactionRecord:
        """記錄一次 compaction 事件,並若 ``at_turn_index`` 有對應 turn,
        把該 turn 的 ``compaction_count`` +1。

        Args:
            compactor_name: 對應 P1-6 ``CompactorABC.name``(例 ``"micro"``)。
            messages_dropped: 被壓掉的訊息數。
            before_tokens: compact 前 token。
            after_tokens: compact 後 token。
            at_turn_index: 觸發時所在的 turn index;預設 ``-1`` 表示「無 active
                turn」(例:session-end 階段觸發的 compact)。
            compaction_id: 自訂 id;不給就生 uuid4。
            triggered_at: 觸發時間;不給就 ``time.time()``。

        Returns:
            新建好的 CompactionRecord。
        """
        record = CompactionRecord(
            compaction_id=compaction_id or uuid.uuid4().hex,
            at_turn_index=at_turn_index,
            compactor_name=compactor_name,
            messages_dropped=messages_dropped,
            before_tokens=before_tokens,
            after_tokens=after_tokens,
            triggered_at=triggered_at if triggered_at is not None else time.time(),
        )
        self.compactions.append(record)

        # 若該 turn 在 index 內,把 compaction_count 加一(就地替換 frozen
        # record)。找不到就靜默略過 —— 例:caller 直接送 -1 或 turn 已被
        # truncate 走;不應炸 caller。
        try:
            idx_in_list, old = self._locate_turn(at_turn_index)
        except KeyError:
            return record
        self.turns[idx_in_list] = TurnRecord(
            turn_id=old.turn_id,
            turn_index=old.turn_index,
            started_at=old.started_at,
            ended_at=old.ended_at,
            message_indices=old.message_indices,
            tool_call_count=old.tool_call_count,
            compaction_count=old.compaction_count + 1,
            metadata=dict(old.metadata),
        )
        return record

    # ------------------------------------------------------------------
    # 查詢 helper
    # ------------------------------------------------------------------

    def find_turn(self, turn_index: int) -> TurnRecord | None:
        """依 turn_index 查 record;找不到回 None。"""
        for t in self.turns:
            if t.turn_index == turn_index:
                return t
        return None

    def find_turn_by_id(self, turn_id: str) -> TurnRecord | None:
        """依 turn_id 查 record;找不到回 None。"""
        for t in self.turns:
            if t.turn_id == turn_id:
                return t
        return None

    def compactions_in_turn(self, turn_index: int) -> list[CompactionRecord]:
        """回傳所有 ``at_turn_index == turn_index`` 的 compaction record。"""
        return [c for c in self.compactions if c.at_turn_index == turn_index]

    def latest_turn(self) -> TurnRecord | None:
        """最近一筆 turn record;空回 None。"""
        return self.turns[-1] if self.turns else None

    # ------------------------------------------------------------------
    # 統計
    # ------------------------------------------------------------------

    @property
    def total_turns(self) -> int:
        return len(self.turns)

    @property
    def total_compactions(self) -> int:
        return len(self.compactions)

    @property
    def total_tool_calls(self) -> int:
        return sum(t.tool_call_count for t in self.turns)

    @property
    def total_tokens_saved(self) -> int:
        return sum(c.tokens_saved for c in self.compactions)

    def summary(self) -> str:
        """human-readable summary,debug / log 用。

        Returns:
            單行字串總覽,例:
            ``"ConversationIndex: 3 turns, 2 compactions, 5 tool calls, ~1200 tokens saved"``。
        """
        return (
            f"ConversationIndex: {self.total_turns} turns, "
            f"{self.total_compactions} compactions, "
            f"{self.total_tool_calls} tool calls, "
            f"~{self.total_tokens_saved} tokens saved"
        )

    # ------------------------------------------------------------------
    # 序列化
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """整個 index 轉成 JSON-safe dict。"""
        return {
            "turns": [t.to_dict() for t in self.turns],
            "compactions": [c.to_dict() for c in self.compactions],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ConversationIndex:
        """從 :meth:`to_dict` 還原。

        順序保留;不再 validate ``turn_index`` 唯一性(假設來源是自家
        產的 dict)。
        """
        return cls(
            turns=[TurnRecord.from_dict(t) for t in data.get("turns", [])],
            compactions=[CompactionRecord.from_dict(c) for c in data.get("compactions", [])],
        )

    # ------------------------------------------------------------------
    # private helpers
    # ------------------------------------------------------------------

    def _locate_turn(self, turn_index: int) -> tuple[int, TurnRecord]:
        """找 ``turns`` 內第一個 ``turn_index == turn_index`` 的位置與 record。

        Raises:
            KeyError: 找不到。
        """
        for i, t in enumerate(self.turns):
            if t.turn_index == turn_index:
                return i, t
        raise KeyError(f"turn_index {turn_index} not in index")


__all__ = [
    "CompactionRecord",
    "ConversationIndex",
    "TurnRecord",
]
