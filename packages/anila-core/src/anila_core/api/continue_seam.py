"""把下一輪接到已經送出的答案後面。

跟 Shell 的 continueSeam 同一套：只有整段重複（至少 40 字）才剪掉
續寫開頭。程式碼圍欄沒關時不剪重疊、也不插空白。
標記自己佔一行，所以重疊比對會再試一次拿掉結尾換行的前文。
"""

from __future__ import annotations

import re

_MIN_OVERLAP = 40
_FENCE_LINE = re.compile(r"^( {0,3})(`{3,}|~{3,})(.*)$")


def _fence_marker(line: str) -> dict[str, object] | None:
    matched = _FENCE_LINE.match(line)
    if matched is None:
        return None
    tick = matched.group(2)
    info = matched.group(3) or ""
    if tick[0] == "`" and "`" in info:
        return None
    return {"char": tick[0], "length": len(tick), "info": info}


def _unclosed_fence(text: str) -> dict[str, object] | None:
    open_fence: dict[str, object] | None = None
    for line in str(text or "").split("\n"):
        marker = _fence_marker(line)
        if marker is None:
            continue
        if (
            open_fence is not None
            and marker["char"] == open_fence["char"]
            and int(marker["length"]) >= int(open_fence["length"])
            and str(marker["info"]).strip() == ""
        ):
            open_fence = None
            continue
        if open_fence is None:
            open_fence = marker
    return open_fence


def _strip_repeated_opener(extra: str, open_fence: dict[str, object]) -> str:
    lines = extra.split("\n")
    index = 0
    while index < len(lines) and lines[index].strip() == "":
        index += 1
    if index >= len(lines):
        return extra
    marker = _fence_marker(lines[index])
    if marker is None or marker["char"] != open_fence["char"]:
        return extra
    closes = (
        int(marker["length"]) >= int(open_fence["length"])
        and str(marker["info"]).strip() == ""
    )
    if closes:
        return extra
    same = (
        int(marker["length"]) == int(open_fence["length"])
        and str(marker["info"]).strip() == str(open_fence["info"]).strip()
    )
    if not same:
        return extra
    del lines[: index + 1]
    return "\n".join(lines)


def _overlap_length(prior: str, extra: str) -> int:
    limit = min(len(prior), len(extra), 400)
    for size in range(limit, _MIN_OVERLAP - 1, -1):
        if prior[-size:] == extra[:size]:
            return size
    return 0


def _drop_overlap(prior: str, extra: str) -> str:
    direct = _overlap_length(prior, extra)
    if direct > 0:
        return extra[direct:]
    stripped = re.sub(r"^\n+", "", extra)
    if stripped == extra:
        return extra
    nested = _overlap_length(prior, stripped)
    if nested > 0:
        return stripped[nested:]
    return extra


def _boundary_space(base: str, extra: str) -> str:
    left = base[-1:] if base else ""
    right = extra[:1] if extra else ""
    if not left or not right:
        return ""
    if left.isspace() or right.isspace():
        return ""
    if re.match(r"[A-Za-z0-9]", left) and re.match(r"[A-Za-z0-9]", right):
        return " "
    return ""


def join_continuation(prior: str, addition: str) -> str:
    """跟 Shell ``joinContinuation`` 相同的接縫。"""
    base = prior if isinstance(prior, str) else ""
    extra = addition if isinstance(addition, str) else ""
    if not extra:
        return base
    if not base:
        return extra
    open_fence = _unclosed_fence(base)
    if open_fence is not None:
        extra = _strip_repeated_opener(extra, open_fence)
    if open_fence is None:
        extra = _drop_overlap(base, extra)
    if not extra:
        return base
    gap = "" if open_fence is not None else _boundary_space(base, extra)
    return base + gap + extra


def continuation_suffix(prior: str, addition: str) -> str:
    """前文已經送出。只回傳該接在後面的那一段。

    標記獨佔最後一行，前文常以換行作收。重疊若卡在那個換行前，
    就改用拿掉換行的前文來剪，已送出的換行留著。
    """
    extra = addition if isinstance(addition, str) else ""
    base = prior if isinstance(prior, str) else ""
    if not extra:
        return ""
    if not base:
        return extra
    direct = join_continuation(base, extra)
    suffix = direct[len(base) :] if direct.startswith(base) else extra
    if base.endswith("\n"):
        trimmed = base[:-1]
        alt = join_continuation(trimmed, extra)
        if alt.startswith(trimmed) and len(alt) < len(trimmed) + len(extra):
            return alt[len(trimmed) :]
    return suffix


def append_round(prior: str, addition: str) -> str:
    """把下一輪接到同一則答案。空的一輪就留原文。"""
    base = prior if isinstance(prior, str) else ""
    extra = addition if isinstance(addition, str) else ""
    if not base:
        return extra
    if not extra:
        return base
    return base + continuation_suffix(base, extra)


class ContinuationSeam:
    """串流時先留住開頭，重疊剪掉之後才送出。之後的字原樣接上。"""

    def __init__(self, prior: str) -> None:
        self.prior = prior if isinstance(prior, str) else ""
        self.buf = ""
        self.done = False

    def feed(self, chunk: str) -> str:
        if self.done:
            return chunk
        if chunk:
            self.buf += chunk
        if len(self.buf) < 400:
            return ""
        return self._release()

    def finish(self) -> str:
        if self.done:
            return ""
        return self._release()

    def _release(self) -> str:
        self.done = True
        addition = self.buf
        self.buf = ""
        return continuation_suffix(self.prior, addition)
