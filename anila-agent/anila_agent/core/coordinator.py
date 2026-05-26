"""P1-13 coordinatorMode XML notification 機制。

本模組對應 enhancement roadmap §4.11「coordinatorMode XML notification 系統」,
參考上游 ``src/coordinator/coordinatorMode.ts``。

# 為什麼用 XML message 而不是 JSON tool result

claude-code 的 coordinator mode 讓 parent agent 與 sub-agent 之間用 **XML tag**
(例如 ``<task-notification>...</task-notification>``)傳遞 structured
notification,而不是塞進 tool function 的 JSON return。三個關鍵理由:

1. **LLM 容易解析** — gemma4 / claude 對 ``<task>...</task>`` 這類「自然語言內
   嵌 XML」訓練過,parsing 與 emit 的成功率比強迫產 JSON schema 高。
2. **不依賴 tool function schema** — 不用每加一種訊息就改 tool 的 args
   schema,coordinator 想新增 notification 種類時只要約定新 tag。
3. **可廣播** — 在多 sub-agent 並行情境(§4.3 concurrency partition 已啟用),
   parent 可以把同一段 XML 訊息塞給多個 sub-agent,或把多份 sub-agent 回報
   合併成一個 string 餵回 parent context,不必逐筆 tool result。

# 與其他 P0 / P1 模組的關係

- **P0-8 AgentTool**(``agent_tool.py``)— sub-agent dispatch entry point。
  parent 想對 sub-agent 多塞 ``<task>`` 結構化訊息時,把 :meth:`CoordinatorMessage.to_xml`
  的字串附在 ``prompt`` 後面即可;sub-agent 回應時把 final output 包進
  ``<summary>``,parent 收到後用 :func:`parse_coordinator_messages` 抽取。
- **P1-1 prompt cache prefix**(``prompt_cache.py``)— XML notification 設計上
  接在 prompt 後段(directive 之後),**不影響** prefix bytes,所以 byte-identical
  prefix 仍然命中。
- **P1-3 hook context**(``hook_context.py``)— hook 內若想拿 coordinator
  notification,把 raw text 餵給 :func:`parse_coordinator_messages` 即可。

# 設計取捨

- **不 raise on malformed** — LLM output 不保證合法 XML,strict parser
  會讓整段對話 fail。本模組改成「能抽多少抽多少」+ ``logger.warning``。
- **std lib only** — 用 ``xml.etree.ElementTree`` 不引新 dependency,但
  ``ElementTree`` 對 unclosed tag 會 raise ``ParseError``,因此用 wrapper
  逐段截 well-formed fragment 再 parse。
- **immutable** — :class:`CoordinatorMessage` 是 ``@dataclass(frozen=True)``,
  parse 出來的 list 是新的物件,不會被 mutator 動到。
"""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from enum import Enum
from typing import Final

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 預設 tag 名稱列舉 — 對應上游 coordinatorMode.ts 內 XML tag
# ---------------------------------------------------------------------------


class CoordinatorNotification(str, Enum):
    """常用 coordinator XML tag 列舉。

    對應上游 ``coordinatorMode.ts`` L142-184 的 ``<task-notification>`` 與其
    nested 子 tag,以及本層為 ANILA 多 agent 場景額外擴的 PLAN_CHECK / HANDOFF。

    成員直接是 tag name(``str``),可以做 ``message.tag == CoordinatorNotification.TASK``
    比對,也可以 ``CoordinatorNotification("task")`` 反查。
    """

    TASK = "task"
    """parent → sub-agent:派發新任務,通常含 ``id`` attribute 與描述 body。"""

    SUMMARY = "summary"
    """sub-agent → parent:任務完成後的 human-readable 摘要。"""

    PLAN_CHECK = "plan_check"
    """parent → sub-agent:在執行前對 plan 進行 sanity check 的請求。"""

    STATUS = "status"
    """sub-agent → parent:目前狀態(``running`` / ``completed`` / ``failed``)。"""

    ERROR = "error"
    """sub-agent → parent:錯誤訊息;若 ``status=failed`` 通常會伴隨此 tag。"""

    HANDOFF = "handoff"
    """parent ↔ sub-agent:在 multi-agent flow 中把控制權移交給另一個 worker。"""

    RESULT = "result"
    """sub-agent → parent:任務 final output 完整文字內容(可能很長)。"""

    USAGE = "usage"
    """sub-agent → parent:token / tool usage 統計,通常 nested 在 summary 內。"""


# 允許做為 valid XML tag 開頭的字元 set(ASCII letter / underscore)
# parse 時用來辨識 ``<...>`` 是否為合法 tag 起點,避免把 ``<`` / ``<3`` 誤吃進。
_VALID_TAG_NAME_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_\-]*$"
)


# ---------------------------------------------------------------------------
# CoordinatorMessage dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CoordinatorMessage:
    """parent ↔ sub-agent 之間的單一 XML 訊息。

    Attributes:
        tag: XML tag 名稱(``"task"`` / ``"summary"`` 等)。允許 ASCII letter +
            digit + ``_-`` 字元組,parse / emit 不做大小寫正規化。
        body: tag 的文字 body。若內含子 tag,以原始字串保留(可再用
            :func:`parse_coordinator_messages` 對 body 做 nested parse)。
        attributes: XML attribute mapping,例如 ``{"id": "task_1", "priority": "high"}``。
            預設空 dict。

    Examples:
        >>> msg = CoordinatorMessage(
        ...     tag="task",
        ...     body="Investigate auth bug",
        ...     attributes={"id": "task_1", "priority": "high"},
        ... )
        >>> msg.to_xml()
        '<task id="task_1" priority="high">Investigate auth bug</task>'
    """

    tag: str
    body: str = ""
    attributes: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """validate tag 形狀;空字串或含 ``<>`` / 空白會 raise ``ValueError``。

        attribute 不做白名單(LLM 想塞什麼 attr 都接),只在 emit 時用
        ElementTree 標準 escaping。
        """
        if not _VALID_TAG_NAME_PATTERN.match(self.tag):
            raise ValueError(
                f"invalid coordinator XML tag: {self.tag!r} "
                f"(must match {_VALID_TAG_NAME_PATTERN.pattern})"
            )

    def to_xml(self) -> str:
        """把本訊息序列化為 ``<tag attr=...>body</tag>`` 字串。

        - attribute 順序依 ``self.attributes`` insertion order(Python 3.7+ dict
          有序),測試需要 deterministic 順序時請插入時就排好。
        - body 走 ElementTree 標準 XML escaping(``<`` → ``&lt;`` 等),但若
          body 內已含 nested XML(故意保留 sub-tag),呼叫端應直接用字串拼接
          而不是丟進 body 欄位。
        - 空 body 仍會 emit 完整 open + close tag(``<tag></tag>``),方便
          parse 端取 attribute 用。
        """
        element = ET.Element(self.tag, attrib=self.attributes)
        element.text = self.body
        # ElementTree 預設 short empty tag(``<tag />``),用 short_empty_elements=False
        # 強制長格式;這樣 to_xml / from_xml roundtrip 也比較穩。
        return ET.tostring(
            element,
            encoding="unicode",
            short_empty_elements=False,
        )

    @classmethod
    def from_xml(cls, s: str) -> "CoordinatorMessage":
        """從單一 well-formed XML 字串 parse 出 :class:`CoordinatorMessage`。

        若 ``s`` 不是合法 XML(missing close tag / 多個 root / 空字串),
        raise :class:`ValueError`。malformed 容錯流程請改用
        :func:`parse_coordinator_messages`(對 list 友善)。

        Args:
            s: 完整 ``<tag>...</tag>`` 字串。

        Returns:
            一個 :class:`CoordinatorMessage`。

        Raises:
            ValueError: ``s`` 不是合法 XML。
        """
        try:
            element = ET.fromstring(s)
        except ET.ParseError as exc:
            raise ValueError(f"not well-formed XML: {exc}") from exc

        return cls(
            tag=element.tag,
            body=element.text or "",
            attributes=dict(element.attrib),
        )


# ---------------------------------------------------------------------------
# parse_coordinator_messages — 從 LLM text 抽多段 XML
# ---------------------------------------------------------------------------


# 用來辨識「``<tag>`` 開頭」的 regex;不要求合法 close tag,let ElementTree 把關。
# tag 允許 ``a-zA-Z0-9_-``,attribute 允許任意非 ``>`` 字元(被 ElementTree 再 validate)。
_TAG_OPEN_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"<(?P<tag>[A-Za-z_][A-Za-z0-9_\-]*)(?P<attrs>\s[^>]*)?>"
)


def _find_matching_close(text: str, tag: str, start: int) -> int:
    """從 ``text[start:]`` 找對應 ``</tag>`` 位置(支援 nested 同名 tag)。

    Args:
        text: 原始文字。
        tag: 要找的 close tag 名稱。
        start: 已 consumed 到第一個 ``<tag>`` 之後的 index。

    Returns:
        ``</tag>`` 的起始 index(``<`` 位置)。若找不到,回 -1。
    """
    open_re = re.compile(rf"<{re.escape(tag)}(\s[^>]*)?>")
    close_re = re.compile(rf"</{re.escape(tag)}\s*>")
    depth = 1
    pos = start
    while pos < len(text):
        next_open = open_re.search(text, pos)
        next_close = close_re.search(text, pos)
        if next_close is None:
            return -1
        if next_open is not None and next_open.start() < next_close.start():
            depth += 1
            pos = next_open.end()
            continue
        depth -= 1
        if depth == 0:
            return next_close.start()
        pos = next_close.end()
    return -1


def parse_coordinator_messages(text: str) -> list[CoordinatorMessage]:
    """從一段 LLM 輸出文字內抽出所有 top-level coordinator XML 訊息。

    特性:

    - **top-level only** — 只回傳最外層 tag;若 body 內仍是 XML,保留在
      ``body`` 字串中,呼叫端可再對 body 跑一次 :func:`parse_coordinator_messages`。
    - **nested 同名 tag** — 例如 ``<task>...<task>...</task>...</task>``
      會正確配對到外層 close tag(用 :func:`_find_matching_close` 計算 depth)。
    - **malformed 容錯** — 對 ``<task>`` 沒有 close 的片段:略過該片段、
      ``logger.warning`` 警告、繼續往後找下一個 tag。不會 raise。
    - **attribute parse** — 透過 ``xml.etree.ElementTree`` 取 attribute,
      escape / quoting 走標準解法。

    Args:
        text: LLM 輸出,可能混雜自然語言與多段 XML tag。

    Returns:
        ``list[CoordinatorMessage]``,順序依 text 中出現順序。沒抽到任何
        well-formed tag 時回空 list(不 raise)。
    """
    messages: list[CoordinatorMessage] = []
    pos = 0
    while pos < len(text):
        match = _TAG_OPEN_PATTERN.search(text, pos)
        if match is None:
            break
        tag = match.group("tag")
        body_start = match.end()
        close_start = _find_matching_close(text, tag, body_start)
        if close_start < 0:
            # malformed:open tag 沒對應 close。warn 並從這個 open tag 之後繼續找。
            logger.warning(
                "coordinator parse: dangling open tag %r at offset %d — skipped",
                tag,
                match.start(),
            )
            pos = match.end()
            continue
        # 重新組一段 well-formed XML 給 ElementTree 解析,確保 attribute 處理一致。
        close_end_match = re.compile(rf"</{re.escape(tag)}\s*>").search(text, close_start)
        if close_end_match is None:  # pragma: no cover — _find_matching_close 已保證有
            pos = match.end()
            continue
        fragment = text[match.start() : close_end_match.end()]
        try:
            element = ET.fromstring(fragment)
        except ET.ParseError as exc:
            logger.warning(
                "coordinator parse: malformed fragment for tag %r: %s — skipped",
                tag,
                exc,
            )
            pos = close_end_match.end()
            continue
        # body 取「原始字串」而不是 element.text — 為了保留 nested 子 tag 文字。
        # 因為 ElementTree 把子 tag 拆成 children 後,element.text 只有第一個
        # 子 tag 之前的內容。對 coordinator 場景 nested body 重要,所以這裡
        # 直接從 fragment 抓 inner xml 段落。
        inner_start = match.end()
        inner_end = close_start
        raw_body = text[inner_start:inner_end]
        messages.append(
            CoordinatorMessage(
                tag=element.tag,
                body=raw_body,
                attributes=dict(element.attrib),
            )
        )
        pos = close_end_match.end()
    return messages


# ---------------------------------------------------------------------------
# format_coordinator_messages — 把多段訊息渲染為 string
# ---------------------------------------------------------------------------


def format_coordinator_messages(msgs: list[CoordinatorMessage]) -> str:
    """把多段訊息合併為單一字串,可直接丟給 LLM 當 input。

    - 每條訊息一行(``<tag>body</tag>``),以 ``\\n`` 分隔。
    - 結尾固定附 ``\\n``(方便後面 append 其他內容,LLM 也看慣換行邊界)。
    - 空 list 仍回 ``"\\n"`` 嗎?不,回 ``""``(無內容就不要加雜訊行)。

    Args:
        msgs: 訊息列表。

    Returns:
        以 ``\\n`` 分隔、結尾 ``\\n`` 收尾的 XML 串(空 list → ``""``)。
    """
    if not msgs:
        return ""
    parts = [m.to_xml() for m in msgs]
    return "\n".join(parts) + "\n"


__all__ = [
    "CoordinatorMessage",
    "CoordinatorNotification",
    "format_coordinator_messages",
    "parse_coordinator_messages",
]
