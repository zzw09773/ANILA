"""原文搭配倉庫原有固定台灣用語表；不提供完整簡繁轉換。"""
from __future__ import annotations

from typing import Optional

from anila_core.text.domain_terms import apply_domain_terms


def _changed_char_count(before: str, after: str) -> int:
    """粗算變更字元數（供 log；不追求編輯距離精確值）。"""
    if before == after:
        return 0
    n = min(len(before), len(after))
    diff = sum(1 for i in range(n) if before[i] != after[i])
    return diff + abs(len(before) - len(after))


def normalize_zh_tw(text: Optional[str]) -> Optional[str]:
    """只替換固定域內用語，其他字元保留原文；None／空字串安全。"""
    if text is None:
        return None
    if text == "":
        return ""
    return apply_domain_terms(text)


def normalize_report(text: Optional[str]) -> tuple[Optional[str], int]:
    """回傳 (正規化結果, 變更字元數)，供落庫前記 log。"""
    if text is None:
        return None, 0
    if text == "":
        return "", 0
    normalized = normalize_zh_tw(text)
    # normalize_zh_tw 對非空 str 必回 str
    assert normalized is not None
    return normalized, _changed_char_count(text, normalized)
