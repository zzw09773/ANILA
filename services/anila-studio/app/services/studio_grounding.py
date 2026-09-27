"""標題與大字數字的事實接地。

模型常把來源沒寫的波段、型號、數字寫進簡報標題。這裡只檢查數字（含單位）
與拉丁字母組成的專名；中文一般用詞不標。波段、頻段只有緊接在拉丁字母後面
（例如「X 波段」）才算。比對前做 NFKC、去掉空白，並把 16×16 與 16x16 視為相同。

仍不接地時從標題拿掉該詞、記一筆日誌、在工作狀態留軟警告。這一步不讓工作失敗。
"""
from __future__ import annotations

import copy
import logging
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

GROUNDING_TITLE_WARNING = "部分標題用詞在來源中找不到，已移除"

# 簡報結構用詞，不是事實宣稱。比對時不分大小寫。名單保持這一份。
GENERIC_PRESENTATION_TOKENS = frozenset({
    "q&a",
    "faq",
    "sop",
    "kpi",
    "ai",
    "vs",
    "ppt",
    "agenda",
    "summary",
})

GROUNDING_PROMPT_RULE = "\n".join([
    "── 事實接地（標題、副標、條列、大字數字）──",
    "簡報標題、各頁標題、副標、條列，以及大字統計裡的專有名詞、型號、料號、單位與數字，",
    "都必須來自檢索到的來源段落，不可另行編造。",
    "來源沒有給名稱時，用平實的描述當標題，不要自創名稱、波段、頻段、型號或料號。",
    "使用台灣繁體中文。",
])

_BAND_RE = re.compile(r"(?<![A-Za-z0-9])([A-Za-z]{1,8})\s*(波段|頻段)")
_NUMBER_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:"
    r"\d+(?:\.\d+)?(?:\s*[xX×✕✖]\s*\d+(?:\.\d+)?)+"
    r"|\d{1,3}(?:,\d{3})+(?:\.\d+)?"
    r"|\d+(?:\.\d+)?"
    r")(?:\s*(?:%|％|℃)|\s*°[CFcf]?|\s*度)?"
)
_PROPER_RE = re.compile(
    r"(?<![A-Za-z0-9])(?=[A-Za-z0-9]*[A-Za-z])"
    r"[A-Za-z0-9]+(?:[./-][A-Za-z0-9]+)*"
)
_FOLD_TIMES = re.compile(r"(?<=\d)\s*[×✕✖xX]\s*(?=\d)")
_FOLD_COMMA = re.compile(r"(?<=\d),(?=\d)")
_QA_RE = re.compile(r"(?<![A-Za-z0-9])Q\s*&\s*A(?![A-Za-z0-9])", re.IGNORECASE)
_CHAPTER_BEFORE = re.compile(r"第\s*$")
_CHAPTER_AFTER = re.compile(r"^\s*章")
_TITLE_EDGE = re.compile(r"^(?:[，、,：:；;\-—]+\s*)+|(?:\s*[，、,：:；;\-—]+)+$")

_FALLBACK = {"deck": "簡報", "slide": "本頁", "heading": "本章"}
_CN_MARKS = "甲乙丙丁戊己庚辛壬癸"


@dataclass
class GroundingResult:
    data: dict[str, Any]
    warning: str | None
    removed: list[str]


def passages_from_chunks(chunks: list[dict[str, Any]]) -> str:
    """檢索段落正文。檔名不算依據。"""
    parts: list[str] = []
    for chunk in chunks:
        if isinstance(chunk, dict) and chunk.get("content"):
            parts.append(str(chunk["content"]))
    return "\n".join(parts)


def grounding_corpus(
    passages: str,
    *,
    collection_name: str | None = None,
    extra_instructions: str | None = None,
    preset: str | None = None,
) -> str:
    """段落，加上使用者自己寫過的知識庫名稱、補充指示與風格名稱。"""
    parts: list[str] = []
    if passages and passages.strip():
        parts.append(passages.strip())
    for piece in (collection_name, extra_instructions, preset):
        if piece and str(piece).strip():
            parts.append(str(piece).strip())
    return "\n".join(parts)


def grounding_retry_instruction(tokens: list[str]) -> str:
    listed = "、".join(tokens)
    return (
        "以下用詞不在來源段落中，請移除或改成來源裡已有的說法"
        "（these are not in the source; remove or replace them）：\n"
        f"{listed}\n\n"
        "請重新輸出整個 JSON 物件。第一個字元必須是 {，最後一個字元必須是 }，"
        "不要前言、不要程式碼圍欄。"
        "標題、副標、條列與大字數字裡的專有名詞、型號、料號、單位和數字"
        "只能來自來源；來源沒有名稱時，用平實的描述當標題。"
    )


def ungrounded_tokens(text: str, source: str) -> list[str]:
    return _ungrounded_in_texts([text], source)


def _fold(text: str) -> str:
    folded = unicodedata.normalize("NFKC", text).replace("\u3000", " ")
    folded = _FOLD_TIMES.sub("x", folded)
    folded = _FOLD_COMMA.sub("", folded)
    return re.sub(r"\s+", "", folded)


def _normalize_search(text: str) -> str:
    """NFKC、乘號與千分位對齊。空白留著，讓「2026 Q3」裡的 2026 與 Q3 仍分開。"""
    text = unicodedata.normalize("NFKC", text).replace("\u3000", " ")
    text = _FOLD_TIMES.sub("x", text)
    return _FOLD_COMMA.sub("", text)


def _present(token: str, source: str) -> bool:
    pattern = _token_pattern(token)
    if not pattern:
        return True
    return re.search(pattern, _normalize_search(source)) is not None


def _is_generic_token(token: str) -> bool:
    compact = re.sub(r"\s+", "", token).casefold()
    return compact in GENERIC_PRESENTATION_TOKENS


def _is_section_index(norm: str, match: re.Match[str]) -> bool:
    """01／02 這種節次，以及「第 1 章」。兩位數以內的章次才算，年份不算。"""
    body = re.sub(r"\s+", "", match.group(0))
    if re.fullmatch(r"0\d", body):
        return True
    if not re.fullmatch(r"\d{1,2}", body):
        return False
    before = norm[max(0, match.start() - 3):match.start()]
    after = norm[match.end():match.end() + 2]
    return _CHAPTER_BEFORE.search(before) is not None and _CHAPTER_AFTER.match(after) is not None


def _extract_tokens(text: str) -> list[str]:
    norm = unicodedata.normalize("NFKC", text).replace("\u3000", " ")
    occupied = [False] * len(norm)
    tokens: list[str] = []

    def take(match: re.Match[str], token: str) -> None:
        span = range(match.start(), match.end())
        if any(occupied[i] for i in span):
            return
        for i in span:
            occupied[i] = True
        tokens.append(token)

    for match in _QA_RE.finditer(norm):
        if _is_generic_token(match.group(0)):
            for index in range(match.start(), match.end()):
                occupied[index] = True
            continue
        take(match, re.sub(r"\s+", "", match.group(0)))
    for match in _BAND_RE.finditer(norm):
        take(match, f"{match.group(1)} {match.group(2)}")
    for match in _NUMBER_RE.finditer(norm):
        if _is_section_index(norm, match):
            for index in range(match.start(), match.end()):
                occupied[index] = True
            continue
        take(match, re.sub(r"\s+", " ", match.group(0).strip()))
    for match in _PROPER_RE.finditer(norm):
        raw = match.group(0)
        if len(raw) < 2 or _is_generic_token(raw):
            continue
        take(match, raw)
    return tokens


def _ungrounded_in_texts(texts: list[str], source: str) -> list[str]:
    found: list[str] = []
    for text in texts:
        for token in _extract_tokens(text):
            if token in found or _is_generic_token(token) or _present(token, source):
                continue
            found.append(token)
    return found


def _checked_texts(data: dict[str, Any], kind: str) -> list[str]:
    texts: list[str] = []
    title = data.get("title")
    if isinstance(title, str):
        texts.append(title)
    if kind == "outline":
        for section in data.get("sections") or []:
            if not isinstance(section, dict):
                continue
            heading = section.get("heading")
            if isinstance(heading, str):
                texts.append(heading)
            for slide in section.get("slides") or []:
                if isinstance(slide, dict) and isinstance(slide.get("title"), str):
                    texts.append(slide["title"])
        return texts
    for slide in data.get("slides") or []:
        if not isinstance(slide, dict):
            continue
        if isinstance(slide.get("title"), str):
            texts.append(slide["title"])
        stat = slide.get("stat")
        if isinstance(stat, dict):
            for key in ("value", "baseline"):
                value = stat.get(key)
                if isinstance(value, str) and value.strip():
                    texts.append(value)
    return texts


def _token_pattern(token: str) -> str:
    folded = _fold(token)
    if not folded:
        return ""
    parts: list[str] = []
    for index, char in enumerate(folded):
        prev_digit = index > 0 and folded[index - 1].isdigit()
        next_digit = index + 1 < len(folded) and folded[index + 1].isdigit()
        if char == "x" and prev_digit and next_digit:
            parts.append(r"[xX×✕✖]")
        else:
            parts.append(re.escape(char))
    prefix = r"(?<![A-Za-z0-9])" if folded[0].isalnum() else ""
    suffix = r"(?![A-Za-z0-9])" if folded[-1].isalnum() else ""
    return prefix + r"\s*".join(parts) + suffix


def _clean_title(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return _TITLE_EDGE.sub("", text).strip()


def _fresh_title(kind: str, used: set[str]) -> str:
    base = _FALLBACK[kind]
    if base not in used:
        used.add(base)
        return base
    continued = f"{base}（續）"
    if continued not in used:
        used.add(continued)
        return continued
    for mark in _CN_MARKS:
        candidate = f"{base}（{mark}）"
        if candidate not in used:
            used.add(candidate)
            return candidate
    used.add(base)
    return base


def _strip_text(text: str, tokens: list[str]) -> tuple[str, list[str]]:
    hit: list[str] = []
    updated = text
    for token in sorted(tokens, key=len, reverse=True):
        pattern = _token_pattern(token)
        if pattern and re.search(pattern, updated):
            updated = re.sub(pattern, " ", updated)
            hit.append(token)
    return _clean_title(updated), hit


def _write_title(
    obj: dict[str, Any],
    key: str,
    tokens: list[str],
    fallback_kind: str,
    used: set[str],
    removed: list[str],
) -> None:
    value = obj.get(key)
    if not isinstance(value, str):
        return
    updated, hit = _strip_text(value, tokens)
    if not hit:
        used.add(value.strip())
        return
    for token in tokens:
        if token in hit and token not in removed:
            removed.append(token)
    if not updated or updated in used:
        updated = _fresh_title(fallback_kind, used)
    else:
        used.add(updated)
    obj[key] = updated


def _strip_document(data: dict[str, Any], tokens: list[str], kind: str) -> list[str]:
    """拿掉標題裡不接地的詞。簡報總標題可以和某一頁相同；同一層標題不重複。"""
    removed: list[str] = []
    _write_title(data, "title", tokens, "deck", set(), removed)
    headings: set[str] = set()
    slides: set[str] = set()
    if kind == "outline":
        for section in data.get("sections") or []:
            if not isinstance(section, dict):
                continue
            _write_title(section, "heading", tokens, "heading", headings, removed)
            for slide in section.get("slides") or []:
                if isinstance(slide, dict):
                    _write_title(slide, "title", tokens, "slide", slides, removed)
        return removed
    for slide in data.get("slides") or []:
        if isinstance(slide, dict):
            _write_title(slide, "title", tokens, "slide", slides, removed)
    return removed


async def apply_grounding(
    data: dict[str, Any],
    source: str,
    *,
    kind: str,
    reask: Callable[[list[str]], Awaitable[dict[str, Any] | None]],
    collection_name: str | None = None,
    extra_instructions: str | None = None,
    preset: str | None = None,
) -> GroundingResult:
    """不接地就重問一次；還是不接地就改標題。不因這件事拋出例外。"""
    corpus = grounding_corpus(
        source,
        collection_name=collection_name,
        extra_instructions=extra_instructions,
        preset=preset,
    )
    current = copy.deepcopy(data)
    tokens = _ungrounded_in_texts(_checked_texts(current, kind), corpus)
    if not tokens:
        return GroundingResult(current, None, [])

    revised: dict[str, Any] | None = None
    try:
        revised = await reask(list(tokens))
    except Exception:
        logger.warning("grounding retry failed", exc_info=True)
        revised = None

    if isinstance(revised, dict):
        current = copy.deepcopy(revised)
        tokens = _ungrounded_in_texts(_checked_texts(current, kind), corpus)
        if not tokens:
            return GroundingResult(current, None, [])

    removed = _strip_document(current, tokens, kind)
    if removed:
        logger.info("grounding removed title tokens: %s", "、".join(removed))
    return GroundingResult(current, GROUNDING_TITLE_WARNING, removed)
