"""Keyword fallback when the active embedding model cannot be used.

Traditional Chinese has no spaces, so ``to_tsvector('simple', content)``
does not split it. ``pg_trgm`` is already installed (migration 0044).
Search still extracts CJK bigrams and trigrams and matches them with
``ILIKE``, which a trigram GIN index can serve.
"""

from __future__ import annotations

import re
from typing import Sequence, TypeVar


KEYWORD_FALLBACK_NOTICE = (
    "嵌入模型暫時無法使用，本次用關鍵字搜尋，結果可能較不準"
)

_CJK = re.compile(r"[\u3400-\u9fff]+")
_WORD = re.compile(r"[A-Za-z0-9]{2,}")

T = TypeVar("T")


def cjk_search_units(text: str, *, limit: int = 12) -> list[str]:
    """Bigrams and trigrams for CJK runs, plus Latin/digit words.

    Order is stable and duplicates are dropped. Short CJK (one
    character) is kept so a single-character query still matches.
    """
    raw = (text or "").strip()
    if not raw:
        return []
    units: list[str] = []
    seen: set[str] = set()

    def add(token: str) -> None:
        if token and token not in seen and len(units) < limit:
            seen.add(token)
            units.append(token)

    for run in _CJK.findall(raw):
        if len(run) == 1:
            add(run)
            continue
        for size in (2, 3):
            if len(run) < size:
                continue
            for i in range(len(run) - size + 1):
                add(run[i : i + size])
                if len(units) >= limit:
                    return units
    for word in _WORD.findall(raw):
        add(word.lower())
        if len(units) >= limit:
            break
    return units


def ilike_pattern(unit: str) -> str:
    """Escape LIKE metacharacters so a unit is matched literally."""
    escaped = (
        unit.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    )
    return f"%{escaped}%"


def search_path(
    *,
    embed_ok: bool,
    active_vectors_exist: bool,
    corpus_exists: bool,
) -> str:
    """``vector`` or ``keyword``.

    An empty corpus is not an outage. Keyword fallback is for a model
    that cannot be called, or a corpus that has text but no vectors for
    the model search is using.
    """
    if not corpus_exists:
        return "vector"
    if embed_ok and active_vectors_exist:
        return "vector"
    return "keyword"


def parse_rerank_indexes(text: str, n: int) -> list[int] | None:
    """Parse an LLM reply of 1-based positions into a permutation.

    Returns None when the reply does not mention every candidate once.
    """
    if n <= 0:
        return []
    found = [int(tok) for tok in re.findall(r"\d+", text or "")]
    indexes = [i - 1 for i in found if 1 <= i <= n]
    if len(indexes) < n or len(set(indexes)) != n:
        return None
    # Keep the first occurrence of each index, drop extras.
    seen: set[int] = set()
    ordered: list[int] = []
    for idx in indexes:
        if idx in seen:
            continue
        seen.add(idx)
        ordered.append(idx)
        if len(ordered) == n:
            break
    if len(ordered) != n:
        return None
    return ordered


def order_by_indexes(items: Sequence[T], indexes: Sequence[int]) -> list[T]:
    return [items[i] for i in indexes]
