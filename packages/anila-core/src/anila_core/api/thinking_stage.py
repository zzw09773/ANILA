"""模型自報的階段行。

一整行 ``STAGE: 標題`` 才算。半行先留著，等後續片段或串流結束再決定，
所以切在任何字元上都得到同一份可見文字與同一條階段清單。
引用行與程式碼區塊裡的 STAGE 是正文，不是控制標記。
"""

from __future__ import annotations

import re
from typing import Any

from .router_prompts import redact_internal_details

# 召回與救援走同一條階段清單，標題就是畫面上那一步。
RECALL_STAGE_TITLE = "搜尋過往對話"
RESCUE_STAGE_TITLE = "整理答案"

_STAGE_KEYWORD = "STAGE:"
# 行首的反引號或星號可以包住控制行。> 是引用，不是包裹，留在答案裡。
_STAGE_LINE_RE = re.compile(
    r"^[ \t]*(?:[`*]{1,3}[ \t]*)?STAGE:[ \t]*(.*?)\s*$",
)
# 給使用者看的標題。提示要求 10 到 20 字，多出來的截掉，避免一整段被當成標題。
_TITLE_LIMIT = 40
# CommonMark 圍欄：最多三個空白，至少三個同樣的 ` 或 ~。反引號的資訊字串不能再含反引號。
_FENCE_OPEN_RE = re.compile(r"^( {0,3})(`{3,}|~{3,})(.*)$")
_FENCE_CLOSE_RE = re.compile(r"^( {0,3})(`{3,}|~{3,})[ \t]*$")


def _clean_title(raw: str) -> str:
    title = raw.strip().strip("`*>").strip()
    title = " ".join(redact_internal_details(title).split())
    if len(title) > _TITLE_LIMIT:
        title = title[:_TITLE_LIMIT].rstrip()
    return title


def parse_stage_line(line: str) -> str | None:
    """是階段行就回標題（空標題回空字串）；不是就回 None。"""
    body = line.rstrip("\r\n")
    if body.lstrip(" \t").startswith(">"):
        return None
    matched = _STAGE_LINE_RE.match(body)
    if matched is None:
        return None
    return _clean_title(matched.group(1))


def _keyword_status(text: str) -> str:
    if not text:
        return "hold"
    if text.startswith(_STAGE_KEYWORD):
        return "open"
    if _STAGE_KEYWORD.startswith(text):
        return "hold"
    return "no"


def stage_intro_status(fragment: str) -> str:
    """未完成的這一行還會不會變成 STAGE。

    ``open`` 已經寫出 STAGE:，標題還沒換行。
    ``hold`` 仍是 STAGE 的前綴，或只有空白、反引號、星號。
    ``no`` 再多幾個字也不會是階段行，可以送出去。引用符號不算包裹。
    """
    if not fragment:
        return "hold"
    index = 0
    limit = len(fragment)
    while index < limit and fragment[index] in " \t":
        index += 1
    if index == limit:
        return "hold"
    rest = fragment[index:]
    best = _keyword_status(rest)
    if rest[0] not in "`*":
        return best
    wrapped = 0
    while wrapped < len(rest) and wrapped < 3 and rest[wrapped] in "`*":
        wrapped += 1
    if wrapped == len(rest):
        return "hold"
    for width in range(1, wrapped + 1):
        after = rest[width:]
        spaces = 0
        while spaces < len(after) and after[spaces] in " \t":
            spaces += 1
        if spaces == len(after):
            best = "hold"
            continue
        status = _keyword_status(after[spaces:])
        if status == "open":
            return "open"
        if status == "hold":
            best = "hold"
    return best


def _newline_end(text: str, *, final: bool) -> int | None:
    """第一個換行的結束位置。結尾單獨的 \\r 先留著，避免切成兩個換行。"""
    limit = len(text)
    index = 0
    while index < limit:
        char = text[index]
        if char == "\n":
            return index + 1
        if char == "\r":
            if index + 1 < limit and text[index + 1] == "\n":
                return index + 2
            if index + 1 == limit and not final:
                return None
            return index + 1
        index += 1
    return None


def _opening_fence(body: str) -> tuple[str, int] | None:
    """完整的一行若是開欄，回傳圍欄字元與長度。"""
    matched = _FENCE_OPEN_RE.match(body)
    if matched is None:
        return None
    marks = matched.group(2)
    info = matched.group(3)
    if marks[0] == "`" and "`" in info:
        return None
    return marks[0], len(marks)


def _closes_fence(body: str, char: str, length: int) -> bool:
    matched = _FENCE_CLOSE_RE.match(body)
    if matched is None:
        return False
    marks = matched.group(2)
    return marks[0] == char and len(marks) >= length


def _could_be_opening_fence(fragment: str) -> bool:
    """這一行還沒結束，但仍可能長成開欄。"""
    if not fragment or "\n" in fragment or "\r" in fragment:
        return not fragment
    spaces = 0
    while spaces < len(fragment) and fragment[spaces] == " " and spaces < 3:
        spaces += 1
    rest = fragment[spaces:]
    if not rest:
        return True
    if rest[0] == " ":
        return False
    mark = rest[0]
    if mark not in "`~":
        return False
    run = 0
    while run < len(rest) and rest[run] == mark:
        run += 1
    tail = rest[run:]
    if not tail:
        return True
    if run < 3:
        return False
    if mark == "`" and "`" in tail:
        return False
    return True


def _could_be_closing_fence(fragment: str, char: str, length: int) -> bool:
    """區塊裡這一行還沒結束，但仍可能是關欄。"""
    if not fragment or "\n" in fragment or "\r" in fragment:
        return not fragment
    spaces = 0
    while spaces < len(fragment) and fragment[spaces] == " " and spaces < 3:
        spaces += 1
    rest = fragment[spaces:]
    if not rest:
        return True
    if rest[0] == " ":
        return False
    if rest[0] != char:
        return False
    run = 0
    while run < len(rest) and rest[run] == char:
        run += 1
    tail = rest[run:]
    if not tail:
        return True
    if run < length:
        return False
    return all(item in " \t" for item in tail)


class StageSieve:
    """把一段串流裡的階段行拿掉，其餘文字原樣送出。

    圍欄有沒有關上要跨片段記住。區塊裡與引用行都不解析 STAGE。
    """

    def __init__(self) -> None:
        self.pending = ""
        # 這一行已經確定不是階段。後面的字直接送出，不再把空白當成下一行 STAGE 的開頭。
        self.passthrough = False
        self.fence_char = ""
        self.fence_len = 0

    def _in_fence(self) -> bool:
        return self.fence_len > 0

    def _should_hold(self, fragment: str) -> bool:
        if self._in_fence():
            return _could_be_closing_fence(fragment, self.fence_char, self.fence_len)
        if _could_be_opening_fence(fragment):
            return True
        return stage_intro_status(fragment) != "no"

    def _consume_line(self, raw_line: str, visible: list[str], titles: list[str]) -> None:
        body = raw_line.rstrip("\r\n")
        if self._in_fence():
            if _closes_fence(body, self.fence_char, self.fence_len):
                self.fence_char = ""
                self.fence_len = 0
            visible.append(raw_line)
            return
        opened = _opening_fence(body)
        if opened is not None:
            self.fence_char, self.fence_len = opened
            visible.append(raw_line)
            return
        parsed = parse_stage_line(body)
        if parsed is None:
            visible.append(raw_line)
        elif parsed:
            titles.append(parsed)

    def feed(self, chunk: str, *, final: bool = False) -> tuple[str, list[str]]:
        if chunk:
            self.pending += chunk
        visible: list[str] = []
        titles: list[str] = []
        while True:
            if self.passthrough:
                end = _newline_end(self.pending, final=final)
                if end is None:
                    if self.pending:
                        visible.append(self.pending)
                        self.pending = ""
                    if final:
                        self.passthrough = False
                    break
                visible.append(self.pending[:end])
                self.pending = self.pending[end:]
                self.passthrough = False
                continue
            end = _newline_end(self.pending, final=final)
            if end is not None:
                raw_line = self.pending[:end]
                self.pending = self.pending[end:]
                self._consume_line(raw_line, visible, titles)
                continue
            if final:
                if self.pending:
                    self._consume_line(self.pending, visible, titles)
                    self.pending = ""
                self.passthrough = False
                break
            if self.pending and not self._should_hold(self.pending):
                visible.append(self.pending)
                self.pending = ""
                self.passthrough = True
                continue
            break
        return "".join(visible), titles

    def finish(self) -> tuple[str, list[str]]:
        return self.feed("", final=True)


class ThinkingStageBook:
    """一條階段清單。新的一筆開始時，上一筆進行中的改為完成。"""

    def __init__(self) -> None:
        self.stages: list[dict[str, Any]] = []

    def open(self, title: str) -> list[dict[str, Any]]:
        cleaned = title.strip()
        if not cleaned:
            return []
        events: list[dict[str, Any]] = []
        if self.stages and self.stages[-1]["status"] == "running":
            self.stages[-1]["status"] = "done"
            events.append(dict(self.stages[-1]))
        item = {"index": len(self.stages), "title": cleaned, "status": "running"}
        self.stages.append(item)
        events.append(dict(item))
        return events

    def settle(self, status: str) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        for item in self.stages:
            if item["status"] != "running":
                continue
            item["status"] = status
            events.append(dict(item))
        return events

    def snapshot(self) -> list[dict[str, Any]]:
        return [dict(item) for item in self.stages]


class LiveThinkingStages:
    """思考與正文各留一行緩衝，編號共用同一本清單。"""

    def __init__(self) -> None:
        self.book = ThinkingStageBook()
        self.reasoning = StageSieve()
        self.content = StageSieve()

    def _open_all(self, titles: list[str]) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        for title in titles:
            events.extend(self.book.open(title))
        return events

    def feed_reasoning(self, chunk: str) -> tuple[str, list[dict[str, Any]]]:
        visible, titles = self.reasoning.feed(chunk)
        return visible, self._open_all(titles)

    def feed_content(self, chunk: str) -> tuple[str, list[dict[str, Any]]]:
        visible, titles = self.content.feed(chunk)
        return visible, self._open_all(titles)

    def flush(self) -> tuple[str, str, list[dict[str, Any]]]:
        """串流結束，把還沒換行的半行定案。"""
        reasoning, reasoning_titles = self.reasoning.finish()
        content, content_titles = self.content.finish()
        return reasoning, content, self._open_all(reasoning_titles + content_titles)

    def open_named(self, title: str) -> list[dict[str, Any]]:
        return self.book.open(title)

    def settle(self, status: str) -> list[dict[str, Any]]:
        return self.book.settle(status)

    def absorb_turn(
        self,
        reasoning: str,
        content: str,
        *,
        rescued: bool = False,
    ) -> tuple[str, str, list[dict[str, Any]]]:
        """非串流的一整輪。思考裡的階段先於正文；救援插在兩者之間。"""
        reasoning_text, reasoning_titles = StageSieve().feed(reasoning or "", final=True)
        content_text, content_titles = StageSieve().feed(content or "", final=True)
        events = self._open_all(reasoning_titles)
        if rescued:
            events.extend(self.book.open(RESCUE_STAGE_TITLE))
            events.extend(self.book.settle("done"))
        events.extend(self._open_all(content_titles))
        return reasoning_text, content_text, events

    def snapshot(self) -> list[dict[str, Any]]:
        return self.book.snapshot()
