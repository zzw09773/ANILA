"""繁體中文（zh-TW）輸出正規化：OpenCC s2twp → 域內用語。

字典載入約 30–50ms，用 lru_cache 做成行程內單例。Never raise 給呼叫端 —
例外由 CSP hook 攔下；本模組本身只做純轉換。
"""
from __future__ import annotations

from functools import lru_cache
from typing import Optional

from opencc import OpenCC

from anila_core.text.domain_terms import apply_domain_terms


@lru_cache(maxsize=1)
def _get_converter() -> OpenCC:
    return OpenCC("s2twp")


def _changed_char_count(before: str, after: str) -> int:
    """粗算變更字元數（供 log；不追求編輯距離精確值）。"""
    if before == after:
        return 0
    n = min(len(before), len(after))
    diff = sum(1 for i in range(n) if before[i] != after[i])
    return diff + abs(len(before) - len(after))


def normalize_zh_tw(text: Optional[str]) -> Optional[str]:
    """s2twp 再接域內用語；None／空字串安全。"""
    if text is None:
        return None
    if text == "":
        return ""
    converted = _get_converter().convert(text)
    return apply_domain_terms(converted)


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
