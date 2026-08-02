"""國防／院內用語對照 — 跑在 OpenCC s2twp 之後。

s2twp 管一般用詞（视频→影片），管不了「導彈／激光」這類大陸繁體寫法，
也會把已正確的「演算法」加成「演演算法」。本模組補這兩類缺口。
最長鍵優先；「算法→演算法」用 negative lookbehind 避開已是「演算法」的固定點。
"""
from __future__ import annotations

import re

# 字面替換（含 s2twp 產物再收斂、以及 OpenCC 加倍修復）。
# 刻意不映射「質量→品質」：國防／工程語境「質量」多半是 mass，
# 無條件改寫會污染物理術語（質量守恆、彈頭質量、質量流率）。
# 簡體「质量」仍經 s2twp 轉成「質量」（字形轉換，非詞替換）。
_LITERAL_MAP: dict[str, str] = {
    "人工智能": "人工智慧",
    "演演算法": "演算法",  # 修 s2twp 對「演算法」的加倍
    "信息化": "資訊化",
    "智能化": "智慧化",
    "導彈": "飛彈",
    "激光": "雷射",
    "鐳射": "雷射",  # s2twp 把「激光」收成「鐳射」後再對齊院內用語
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
