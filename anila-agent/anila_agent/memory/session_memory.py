"""P2-6 SessionMemory — 跨 session 的對話脈絡 summary 與 retriever。

本模組對齊 claude-code-src 的 ``services/SessionMemory`` 與 deep-dive
§4.16「SessionMemory + autoDream」:在 memdir(個人事實長期記憶,P0-7)與
short-term ``CompactingSession``(turn 內 token 視窗,P1-6)之間,補一層
**session-level 記憶**。

三層 hierarchy:

- **session note**:當前 session 的 working memory — 由 :class:`AutoDreamer`
  在 session 結束(或 idle)時抽出 summary / topics / lessons。
- **memdir**:跨 session 的事實記憶(P0-7 ``LongTermMemory``)。
- **dream**:當累積 N 個 session 後,可在 :class:`AutoDreamer` 上層再跑一輪
  consolidation(本 module 只負責原子單位,合併策略由呼叫端決定)。

設計取捨
~~~~~~~~

- ``SessionMemory`` 是 ``frozen=True`` dataclass(對齊 ``rules/python/coding-style.md``
  immutability 要求),只負責 serialize / deserialize 語意。
- ``SessionMemoryStore`` 是 :class:`typing.Protocol`,讓未來換 SQLite / postgres
  時 caller 不用改;預設 :class:`JsonFileSessionMemoryStore` 對齊 P1-8
  ``JsonFileStateStore`` 的 ``.json`` 落地風格(同樣不 file-lock、deterministic
  bytes、find by id)。
- conversation hash 用 SHA-256(message 列表的 stable JSON dump)— autoDream
  跑兩次同 session 不會生重複條目;若 caller 想強制 re-dream,可呼叫
  ``store.delete(session_id)`` 再 dream。
- 全部 type annotation,公開 API 沒有 ``Any`` 漏網。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from anila_agent.memory.compaction import Message

__all__ = [
    "JsonFileSessionMemoryStore",
    "SessionMemory",
    "SessionMemoryRetriever",
    "SessionMemoryStore",
    "compute_conversation_hash",
]


# ---------------------------------------------------------------------------
# SessionMemory dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SessionMemory:
    """單一 session 的 high-level summary。

    Attributes:
        session_id: session 唯一識別 — 對齊 P1-3 ``SessionContext.session_id``。
        summary: 一段 free-form 中文(或英文)摘要,描述「這個 session 在做什麼」。
        topics: 主題標籤(供 :class:`SessionMemoryRetriever` 做 keyword overlap)。
        lessons: 從 session 抽出的 lesson learned(經驗條目)。
        started_at: session 起始 UTC ISO timestamp。
        ended_at: session 結束 UTC ISO timestamp(若仍在進行可為 ``None``)。
        message_count: 來源 conversation 訊息數,做為 sanity check。
        conversation_hash: SHA-256(stable JSON of source messages)— 用來 dedup
            避免同 session 被 dream 兩次。
        model: 跑 dream 的 LLM 名稱(metadata,不參與比對)。
    """

    session_id: str
    summary: str
    topics: tuple[str, ...] = field(default_factory=tuple)
    lessons: tuple[str, ...] = field(default_factory=tuple)
    started_at: str | None = None
    ended_at: str | None = None
    message_count: int = 0
    conversation_hash: str = ""
    model: str | None = None

    def to_json(self) -> str:
        """序列化成 deterministic JSON 字串(同 instance 必出同 bytes)。"""
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True, indent=2)

    def to_dict(self) -> dict[str, Any]:
        """轉成可 JSON dump 的 plain dict;tuple 變 list。"""
        return {
            "session_id": self.session_id,
            "summary": self.summary,
            "topics": list(self.topics),
            "lessons": list(self.lessons),
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "message_count": self.message_count,
            "conversation_hash": self.conversation_hash,
            "model": self.model,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> SessionMemory:
        """從 dict 還原 SessionMemory。

        Raises:
            ValueError: 缺必要欄位(``session_id`` / ``summary``)。
        """
        if "session_id" not in data:
            raise ValueError("SessionMemory.from_dict: missing 'session_id'")
        if "summary" not in data:
            raise ValueError("SessionMemory.from_dict: missing 'summary'")
        topics = tuple(str(t) for t in data.get("topics") or ())
        lessons = tuple(str(t) for t in data.get("lessons") or ())
        return cls(
            session_id=str(data["session_id"]),
            summary=str(data["summary"]),
            topics=topics,
            lessons=lessons,
            started_at=data.get("started_at"),
            ended_at=data.get("ended_at"),
            message_count=int(data.get("message_count", 0)),
            conversation_hash=str(data.get("conversation_hash", "")),
            model=data.get("model"),
        )

    @classmethod
    def from_json(cls, raw: str) -> SessionMemory:
        """從 JSON 字串還原。"""
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise ValueError(f"SessionMemory.from_json: invalid JSON: {e}") from e
        if not isinstance(data, dict):
            raise ValueError(
                f"SessionMemory.from_json: top-level must be object, got {type(data).__name__}"
            )
        return cls.from_dict(data)


def compute_conversation_hash(messages: Iterable[Message]) -> str:
    """算一個跨 process 穩定的 conversation hash。

    取每筆 message 的 ``role`` / ``content`` / ``type`` 三個 well-known key,
    sort_keys 後 dump 成 UTF-8 bytes,跑 SHA-256。其他欄位(timestamp / id)
    刻意 ignore — 同一段對話內容 hash 必相同,即使 caller 重新 attach 也不會
    多生一筆 SessionMemory。

    Args:
        messages: P1-6 ``Message`` dict 列表。

    Returns:
        小寫 hex SHA-256 字串。
    """
    normalised: list[dict[str, Any]] = []
    for msg in messages:
        if not isinstance(msg, Mapping):
            continue
        normalised.append(
            {
                "role": msg.get("role"),
                "content": msg.get("content"),
                "type": msg.get("type"),
            }
        )
    raw = json.dumps(normalised, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


# ---------------------------------------------------------------------------
# Protocol — caller 可換實作
# ---------------------------------------------------------------------------


@runtime_checkable
class SessionMemoryStore(Protocol):
    """SessionMemory 落地介面;預設實作為 :class:`JsonFileSessionMemoryStore`。"""

    def save(self, memory: SessionMemory) -> None:
        """落地一筆 SessionMemory。同 session_id 第二次呼叫應 overwrite。"""

    def load(self, session_id: str) -> SessionMemory | None:
        """讀一筆 SessionMemory;找不到回 ``None``(對齊 ``find`` 慣例)。"""

    def find_by_topic(self, topic: str, top_k: int = 3) -> list[SessionMemory]:
        """依主題 / keyword 找最相關的前 ``top_k`` 筆 SessionMemory。"""

    def list_all(self, limit: int | None = None) -> list[SessionMemory]:
        """列出所有 SessionMemory(預設按 ``ended_at`` desc;最新先)。"""

    def delete(self, session_id: str) -> bool:
        """刪一筆;不存在回 False(不視為錯誤)。"""


# ---------------------------------------------------------------------------
# JsonFile 預設實作
# ---------------------------------------------------------------------------


class JsonFileSessionMemoryStore:
    """把 :class:`SessionMemory` 序列化成 ``<dir>/<session_id>.json``。

    對齊 P1-8 ``JsonFileStateStore`` 風格:

    * 不做 file lock(預設單 process / 單 thread)。
    * deterministic save(同 memory 多次 save 產 byte-identical 檔)。
    * load 找不到不 raise,回 ``None`` — 配合 :class:`SessionMemoryRetriever`
      的 best-effort 語意。
    * ``find_by_topic`` 用 token-overlap heuristic — 不依賴 LLM,純本地、
      tests 無需網路。

    Args:
        dir: 落地目錄;不存在會在 ``__init__`` 自動建立。
    """

    def __init__(self, dir: Path) -> None:
        self._dir = Path(dir)
        self._dir.mkdir(parents=True, exist_ok=True)

    @property
    def dir(self) -> Path:
        """回傳目前的存放目錄(唯讀 view)。"""
        return self._dir

    def path_for(self, session_id: str) -> Path:
        """組出指定 session_id 對應的檔案路徑。"""
        if "/" in session_id or "\\" in session_id or session_id in ("", ".", ".."):
            raise ValueError(
                f"JsonFileSessionMemoryStore: 不合法的 session_id={session_id!r}"
            )
        return self._dir / f"{session_id}.json"

    def save(self, memory: SessionMemory) -> Path:
        """寫入 ``<dir>/<session_id>.json``,回傳實際路徑。"""
        path = self.path_for(memory.session_id)
        path.write_text(memory.to_json(), encoding="utf-8")
        return path

    def load(self, session_id: str) -> SessionMemory | None:
        """讀回一筆;檔案不存在 / 內容壞掉都回 ``None``。"""
        try:
            path = self.path_for(session_id)
        except ValueError:
            return None
        if not path.exists():
            return None
        try:
            return SessionMemory.from_json(path.read_text(encoding="utf-8"))
        except ValueError:
            return None

    def find_by_topic(self, topic: str, top_k: int = 3) -> list[SessionMemory]:
        """token-overlap 找前 ``top_k`` 筆相關 SessionMemory。

        評分:topic 切 token 後,跟每筆 memory 的
        ``topics + summary + lessons`` 算交集 size,大者排前。零交集略過。
        若 topic 為空或沒任何 memory 命中,回空 list。
        """
        if top_k <= 0:
            return []
        query_tokens = _tokenise(topic)
        if not query_tokens:
            return []

        scored: list[tuple[int, SessionMemory]] = []
        for memory in self._iter_all():
            haystack_tokens = _memory_tokens(memory)
            overlap = len(query_tokens & haystack_tokens)
            if overlap > 0:
                scored.append((overlap, memory))

        # 主排序 = overlap desc;副排序 = ended_at desc(較新 session 較相關)。
        scored.sort(
            key=lambda pair: (pair[0], pair[1].ended_at or ""),
            reverse=True,
        )
        return [memory for _, memory in scored[:top_k]]

    def list_all(self, limit: int | None = None) -> list[SessionMemory]:
        """列出所有 SessionMemory(ended_at desc)。"""
        memories = list(self._iter_all())
        memories.sort(key=lambda m: m.ended_at or "", reverse=True)
        if limit is not None and limit >= 0:
            return memories[:limit]
        return memories

    def delete(self, session_id: str) -> bool:
        """刪一筆;不存在回 False。"""
        try:
            path = self.path_for(session_id)
        except ValueError:
            return False
        if not path.exists():
            return False
        path.unlink()
        return True

    def list_session_ids(self) -> list[str]:
        """純檔名 view,測試 / debug 用。"""
        return sorted(p.stem for p in self._dir.glob("*.json"))

    # internal --------------------------------------------------------------

    def _iter_all(self) -> Iterable[SessionMemory]:
        for path in self._dir.glob("*.json"):
            try:
                yield SessionMemory.from_json(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                # 壞檔不 fail 整個 list — best-effort recall。
                continue


# ---------------------------------------------------------------------------
# SessionMemoryRetriever — 找跟「當前 session」相關的歷史 memory
# ---------------------------------------------------------------------------


class SessionMemoryRetriever:
    """從歷史 SessionMemory 中,撈出跟「當前 messages」相關的前幾筆。

    與 P1-10 ``UserContextBuilder.add_memory`` 整合:

    >>> retriever = SessionMemoryRetriever(store)
    >>> recalled = retriever.recall_for_current(current_messages, top_k=3)
    >>> user_builder.add_memory(retriever.to_memory_items(recalled))

    :meth:`to_memory_items` 把 SessionMemory 轉成 ``UserContextBuilder``
    認得的 ``{text, ...}`` mapping,避免在 prompt 那層耦合 SessionMemory schema。
    """

    def __init__(self, store: SessionMemoryStore) -> None:
        self._store = store

    def recall_for_current(
        self,
        current_messages: Iterable[Message],
        top_k: int = 3,
    ) -> list[SessionMemory]:
        """從 current_messages 抽 query,呼 ``store.find_by_topic`` 找相關 session。

        Args:
            current_messages: 當前 session 的訊息(任意 :class:`Message` dict)。
            top_k: 最多回幾筆(預設 3)。

        Returns:
            按相關度排序的 SessionMemory 列表;沒命中回空 list。
        """
        if top_k <= 0:
            return []
        query = self._build_query(current_messages)
        if not query:
            return []
        return list(self._store.find_by_topic(query, top_k=top_k))

    @staticmethod
    def to_memory_items(memories: Iterable[SessionMemory]) -> list[dict[str, Any]]:
        """把 SessionMemory 轉成 P1-10 ``UserContextBuilder.add_memory`` 吃得進去的 dict。

        UserContextBuilder 只看 ``text`` / ``content``(其餘 passthrough),
        本處組一段「session <id>: summary // topics // lessons」字串塞 ``text``。
        """
        items: list[dict[str, Any]] = []
        for memory in memories:
            sections = [f"session {memory.session_id}: {memory.summary}"]
            if memory.topics:
                sections.append("topics: " + ", ".join(memory.topics))
            if memory.lessons:
                sections.append("lessons: " + " // ".join(memory.lessons))
            items.append(
                {
                    "text": "\n".join(sections),
                    "session_id": memory.session_id,
                    "kind": "session_memory",
                }
            )
        return items

    @staticmethod
    def _build_query(messages: Iterable[Message]) -> str:
        """從 messages 抽出查詢字串(取 user / human role 的 content)。

        策略:把所有 ``role in {"user", "human"}`` 的 ``content`` 串起來。
        若全部是 assistant turn(罕見),回空字串。
        """
        parts: list[str] = []
        for msg in messages:
            if not isinstance(msg, Mapping):
                continue
            role = msg.get("role")
            if role not in ("user", "human"):
                continue
            content = msg.get("content")
            if isinstance(content, str):
                parts.append(content)
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, Mapping) and isinstance(block.get("text"), str):
                        parts.append(block["text"])
        return " ".join(parts).strip()


# ---------------------------------------------------------------------------
# token / overlap helpers — 純 stdlib,無 LLM 依賴
# ---------------------------------------------------------------------------


def _tokenise(text: str) -> set[str]:
    """簡易 tokeniser — 取 lowercase word(\\w+)。"""
    import re

    return {m.group(0).lower() for m in re.finditer(r"\w+", text)}


def _memory_tokens(memory: SessionMemory) -> set[str]:
    """把 SessionMemory 的 topic / summary / lessons 串起來 tokenise。"""
    haystack = " ".join(
        [
            memory.summary,
            " ".join(memory.topics),
            " ".join(memory.lessons),
            memory.session_id,
        ]
    )
    return _tokenise(haystack)


def _utcnow_iso() -> str:
    """測試 friendly 的 UTC ISO timestamp(Z 結尾)。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
