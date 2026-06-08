"""P2-15 / claude-code §4.23 — autoCompactBoundary 訊息。

Compaction 完成後,在 message 序列中插入一條「邊界 marker」。它的功能:

- **Resume 安全**:session resume 跨 compaction boundary 時,後續 turn 能辨識
  「這之前是壓縮過的歷史」,LLM 不會去找已不在 wire 上的 tool_call_id。
- **Trace / audit**:JSONL 寫出時可以一眼看到 compaction 發生在哪一輪。
- **多次 compact 的差分**:若同一 session 反覆 compact,每次都會留 boundary,
  形成 chronological breadcrumb,debug 時方便還原當下的 conversation snapshot。

對齊 claude-code-src ``src/services/compact/grouping.ts`` 的設計;Python 端
不需要整套 grouping 演算法,只需要可序列化的 marker schema + 一組 helpers。

模組保證:
- ``AutoCompactBoundary`` 是 ``frozen dataclass``,immutable;
- helper 都是 pure function,接受 / 回傳 ``list[Message]`` 不 mutate 輸入;
- 不依賴 ``CompactorABC`` —— 任何 compactor 都可在 compact 後手動 insert
  boundary,或讓上層 ``CompactingSession`` 統一處理。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

# Message 型別跟 ``memory.compaction`` 保持一致 —— 寬鬆 dict,跨 Chat
# Completion / Responses 兩種 wire format。
Message = dict[str, Any]

# 用 sentinel role 而非 ``"system"`` —— 避免跟一般 system prompt 混淆,讓
# downstream filter / UI 一眼分辨「這是 compaction marker」。openai-agents
# 與 LiteLLM 都把 unknown role 視為 noop / pass-through,不會炸。
BOUNDARY_ROLE: str = "system"

# message ``type`` 欄位(對齊 Responses API 的 type-tagged shape);走 dedicated
# type 讓 grouping 演算法可以精準 filter。
BOUNDARY_TYPE: str = "compact_boundary"

# JSON-ish marker prefix —— LLM 看 content 也能直觀識別。
BOUNDARY_CONTENT_PREFIX: str = "[compact boundary]"


@dataclass(frozen=True)
class AutoCompactBoundary:
    """單一 compaction boundary 的不可變描述。

    Attributes:
        boundary_id: UUID hex,跨 marker 的 unique id(供 trace correlate)。
        compactor_name: 觸發 compact 的 compactor(``"micro"`` /
            ``"llm_summary"`` / ``"snip"`` / 其他自訂)。
        before_messages: compact 前 message 數。
        after_messages: compact 後 message 數。
        before_tokens: compact 前粗估 token 數。
        after_tokens: compact 後粗估 token 數。
        timestamp: 邊界產生的 UTC 時間(供 audit / resume time-range query)。
        extra: 給呼叫端附加任意 metadata(例如 chain_id / trace_id);
            序列化時併入 marker dict。
    """

    boundary_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    compactor_name: str = ""
    before_messages: int = 0
    after_messages: int = 0
    before_tokens: int = 0
    after_tokens: int = 0
    timestamp: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    extra: dict[str, Any] = field(default_factory=dict)

    def to_message(self) -> Message:
        """轉成 wire format 的 message dict。

        ``role`` / ``type`` / ``content`` 三個欄位走 stable schema,讓
        ``is_boundary_message`` 能在 cross-version dump 仍認得出來。其餘
        metadata 都裝在 ``meta`` 子 dict 內 —— upstream LiteLLM / openai-agents
        不會碰 unknown key。
        """
        return {
            "role": BOUNDARY_ROLE,
            "type": BOUNDARY_TYPE,
            "content": (
                f"{BOUNDARY_CONTENT_PREFIX} {self.compactor_name}: "
                f"{self.before_messages}→{self.after_messages} msgs, "
                f"{self.before_tokens}→{self.after_tokens} tokens"
            ),
            "meta": {
                "boundary_id": self.boundary_id,
                "compactor_name": self.compactor_name,
                "before_messages": self.before_messages,
                "after_messages": self.after_messages,
                "before_tokens": self.before_tokens,
                "after_tokens": self.after_tokens,
                "timestamp": self.timestamp.isoformat(),
                **self.extra,
            },
        }


def is_boundary_message(msg: Message) -> bool:
    """判斷單一 message 是否為 boundary marker。

    走 ``type == BOUNDARY_TYPE`` 為主,fallback 看 ``content`` prefix —— 後者
    在某些 wire format 把 ``type`` 欄位丟掉的情境下仍能辨識。
    """
    if not isinstance(msg, dict):
        return False
    if msg.get("type") == BOUNDARY_TYPE:
        return True
    content = msg.get("content")
    if isinstance(content, str) and content.startswith(BOUNDARY_CONTENT_PREFIX):
        return True
    return False


def insert_boundary(
    messages: list[Message], boundary: AutoCompactBoundary
) -> list[Message]:
    """回傳新 list:在尾端追加 boundary marker。

    不 mutate 輸入,跟 ``compaction.py`` 既有 compactor 的 immutability 約定一致。
    """
    new_messages = list(messages)
    new_messages.append(boundary.to_message())
    return new_messages


def extract_boundaries(messages: list[Message]) -> list[Message]:
    """從 message 序列中抽出所有 boundary marker(順序保留)。

    給 audit / debug 用 —— 例如 ``/compact --show-history`` 把所有 boundary
    倒出來看 compaction 次數與規模演變。
    """
    return [m for m in messages if is_boundary_message(m)]


def strip_boundaries(messages: list[Message]) -> list[Message]:
    """回傳新 list:移除所有 boundary marker。

    某些下游(例如 export 給 user 看 conversation history)不想露 internal
    marker;此 helper 提供 inverse operation。
    """
    return [m for m in messages if not is_boundary_message(m)]


def split_by_boundaries(messages: list[Message]) -> list[list[Message]]:
    """以 boundary 為切點,把 messages 切成多段 chunk。

    回傳 list-of-lists:第 i 段為「第 i 個 boundary 之前的訊息(不含 boundary
    本身)」。若無 boundary,回 ``[messages]``;若 boundary 在開頭/結尾,對應段
    為空 list(刻意保留以利位置對齊)。
    """
    chunks: list[list[Message]] = []
    current: list[Message] = []
    for m in messages:
        if is_boundary_message(m):
            chunks.append(current)
            current = []
            continue
        current.append(m)
    chunks.append(current)
    return chunks


__all__ = [
    "BOUNDARY_CONTENT_PREFIX",
    "BOUNDARY_ROLE",
    "BOUNDARY_TYPE",
    "AutoCompactBoundary",
    "Message",
    "extract_boundaries",
    "insert_boundary",
    "is_boundary_message",
    "split_by_boundaries",
    "strip_boundaries",
]
