"""倉庫原有固定國防／院內用語表，不作完整簡繁轉換。

未列入對照的文字保留原文。最長鍵優先；「算法→演算法」避開「演算法」固定點。
"""
from __future__ import annotations

import re

# 只套用明列的字面替換。
# 刻意不映射「質量→品質」：國防／工程語境「質量」多半是 mass，
# 無條件改寫會污染物理術語（質量守恆、彈頭質量、質量流率）。
# 未列入的簡體「质量」亦保持原文。
_LITERAL_MAP: dict[str, str] = {
    "人工智能": "人工智慧",
    "演演算法": "演算法",
    "信息化": "資訊化",
    "智能化": "智慧化",
    "導彈": "飛彈",
    "激光": "雷射",
    "鐳射": "雷射",
    "航天": "航太",
    "芯片": "晶片",
}

# 算法→演算法：已是「演算法」時不得變成「演演算法」
_ALGORITHM_RE = re.compile(r"(?<!演)算法")

_LITERAL_KEYS_LONGEST_FIRST: tuple[str, ...] = tuple(
    sorted(_LITERAL_MAP.keys(), key=len, reverse=True)
)


def apply_domain_terms(text: str) -> str:
    """套用域內用語對照；空字串原樣返回。"""
    if not text:
        return text
    out = text
    for key in _LITERAL_KEYS_LONGEST_FIRST:
        out = out.replace(key, _LITERAL_MAP[key])
    out = _ALGORITHM_RE.sub("演算法", out)
    return out
