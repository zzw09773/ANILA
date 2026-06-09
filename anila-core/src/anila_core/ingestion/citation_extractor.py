"""Regex citation extractor for ROC-style regulation text (document-relations Phase 1).

Pure text → ``list[Citation]``. No DB, no resolution (that lives in the
worker/CSP layer, which matches ``target_title`` against
``ingestion_documents.normalized_title``).

Design: docs/ingestion/document-relations-design.md §5/§6.

Approach (codex review #6): anchor each citation on a **cue verb**
(依/依據/修正/廢止/準用/補充 …) and capture the regulation name that FOLLOWS
it. This avoids matching the document's own self-reference ("本辦法" / "本規定")
and gives an unambiguous ``relation_type`` per span. A sentence may yield
multiple citations.

ReDoS (codex review #8): every pattern is compiled once at import; quantifiers
are bounded and non-nested (linear); ``extract_citations`` caps input length
and truncates ``evidence``.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

__all__ = ["Citation", "extract_citations", "cjk_to_int", "normalize_title"]


# ── Chinese numeral parsing ──────────────────────────────────────────────────
_CJK_DIGIT = {
    "〇": 0, "零": 0, "一": 1, "二": 2, "兩": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}
_CJK_UNIT = {"十": 10, "百": 100, "千": 1000}


def cjk_to_int(s: str | None) -> int | None:
    """Parse an Arabic / full-width / Chinese numeral to int. ``None`` if not numeric.

    Handles 〇/零, 一–九, 十/百/千 positional, 兩=2, 廿=20, 卅=30, and plain
    (incl. full-width) digits. Returns None for empty / non-numeric input.
    """
    if not s:
        return None
    s = unicodedata.normalize("NFKC", s).strip()
    if not s:
        return None
    if s.isdigit():
        return int(s)
    s = s.replace("廿", "二十").replace("卅", "三十")
    result = 0
    current = 0
    for ch in s:
        if ch in _CJK_DIGIT:
            current = _CJK_DIGIT[ch]
        elif ch in _CJK_UNIT:
            result += (current or 1) * _CJK_UNIT[ch]
            current = 0
        else:
            return None
    return result + current


# ── Title normalization ──────────────────────────────────────────────────────
_BRACKET_CHARS = set("「」『』《》〈〉【】〔〕[]()")
_WS_RE = re.compile(r"\s+")


def normalize_title(name: str | None) -> str:
    """Normalize a regulation name for matching: NFKC (full→half width),
    strip whitespace and CJK/ASCII brackets. Idempotent."""
    if not name:
        return ""
    s = unicodedata.normalize("NFKC", name)
    s = _WS_RE.sub("", s)
    return "".join(ch for ch in s if ch not in _BRACKET_CHARS)


# ── Citation model ───────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Citation:
    relation_type: str          # based_on / amends / supersedes / cites / supplements / relates
    target_title: str           # normalized regulation name, for dst resolution
    article: str | None         # normalized "第N條[之M][至第M條][第K項][第L款][第P目]" or None
    target_ref: str             # target_title + (" " + article) if article else target_title
    evidence: str               # display original span (truncated)


# ── Patterns (compiled once — ReDoS-safe: bounded, non-nested) ────────────────
# Cue verbs, longest-first so 依據 wins over 依.
_CUE_TO_TYPE: dict[str, str] = {
    "依據": "based_on", "依照": "based_on", "根據": "based_on", "按": "based_on", "依": "based_on",
    "修正": "amends",
    "廢止": "supersedes", "停止適用": "supersedes", "不再適用": "supersedes",
    "準用": "cites", "參照": "cites", "參見": "cites", "詳見": "cites", "另見": "cites",
    "補充": "supplements", "增訂": "supplements",
}
_CUE_ALT = "|".join(sorted(_CUE_TO_TYPE, key=len, reverse=True))
_NAME_SUFFIX = "條例|辦法|規則|規定|要點|準則|綱要|實施計畫|法"
_NUM = r"[0-9〇零一二三四五六七八九十百千兩廿卅]"
_ART = (
    rf"第\s*({_NUM}+)\s*條"
    rf"(?:\s*之\s*({_NUM}+))?"
    rf"(?:\s*至\s*第\s*({_NUM}+)\s*條)?"
    rf"(?:\s*第\s*({_NUM}+)\s*項)?"
    rf"(?:\s*第\s*({_NUM}+)\s*款)?"
    rf"(?:\s*第\s*({_NUM}+)\s*目)?"
)
# Name = CJK run ending in a reg-type suffix. ``(?!第)`` forbids the run from
# swallowing an article ("第N條"): without it "本法第五十七條規定" collapses into
# one bogus name instead of (name=本法, article=第57條). The article is captured
# separately by the optional ``_ART`` group that follows.
_CITATION_RE = re.compile(
    rf"(?P<cue>{_CUE_ALT})\s*[《「]?\s*"
    rf"(?P<name>(?:(?!第)[一-鿿]){{2,28}}?(?:{_NAME_SUFFIX}))"
    rf"\s*[」》]?\s*(?P<art>{_ART})?"
)
# Self / relative references ("本法" "本章" "前項" "該辦法" …) are NOT other
# documents — they point inside the current one. Drop names that begin with a
# self-reference token + a structural noun. Deliberately narrow: a real name
# like "前瞻基礎建設特別條例" (前+瞻) is NOT matched (瞻 ∉ the noun set).
_SELF_REF_RE = re.compile(
    r"^(?:本|該|同|前)(?:法|律|條|章|節|項|款|目|則|細則|辦法|規則|規定|條例|要點|準則|綱要|計畫)"
)
_SENT_DELIMS = set("。；;！？!?\n")


def _format_article(art_text: str | None) -> str | None:
    """Normalize an article span ("第五條之一" / "第三條至第八條" / "第5條第2項")
    to a canonical string. Re-parses the span standalone to avoid group-number
    confusion with the embedding citation regex."""
    if not art_text:
        return None
    am = re.match(_ART, art_text)
    if not am:
        return None
    a1, a2, a3, a4, a5, a6 = am.groups()
    parts = [f"第{cjk_to_int(a1)}條"]
    if a2:
        # 之一/之二 is the canonical ROC sub-article form — keep as authored,
        # do not arabic-ize ("第10條之一", not "第10條之1").
        parts.append(f"之{a2}")
    if a3:
        parts.append(f"至第{cjk_to_int(a3)}條")
    if a4:
        parts.append(f"第{cjk_to_int(a4)}項")
    if a5:
        parts.append(f"第{cjk_to_int(a5)}款")
    if a6:
        parts.append(f"第{cjk_to_int(a6)}目")
    return "".join(parts)


def _evidence(text: str, start: int, end: int, cap: int) -> str:
    s = start
    while s > 0 and text[s - 1] not in _SENT_DELIMS:
        s -= 1
    return text[s:end].strip()[:cap]


def extract_citations(
    text: str,
    *,
    max_chars: int = 2_000_000,
    max_evidence: int = 500,
) -> list[Citation]:
    """Extract regulation citations from ``text``.

    Each cue-anchored match → a ``Citation``. Deduped by
    ``(relation_type, target_ref)``. Input is NFKC-normalized and capped at
    ``max_chars``; evidence is truncated to ``max_evidence``.
    """
    if not text:
        return []
    text = unicodedata.normalize("NFKC", text)[:max_chars]

    out: list[Citation] = []
    seen: set[tuple[str, str]] = set()
    for m in _CITATION_RE.finditer(text):
        rel = _CUE_TO_TYPE[m.group("cue")]
        raw_name = m.group("name")
        # "本法 / 本章 / 前項 / 該辦法 …" reference the current document, not a
        # citable other one — skip (design §5: avoid self-references).
        if _SELF_REF_RE.match(raw_name):
            continue
        title = normalize_title(raw_name)
        if not title:
            continue
        article = _format_article(m.group("art"))
        target_ref = f"{title} {article}" if article else title
        key = (rel, target_ref)
        if key in seen:
            continue
        seen.add(key)
        out.append(
            Citation(
                relation_type=rel,
                target_title=title,
                article=article,
                target_ref=target_ref,
                evidence=_evidence(text, m.start(), m.end(), max_evidence),
            )
        )
    return out
