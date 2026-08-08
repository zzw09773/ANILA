"""檢索查詢擴展：民國↔西元雙向換算＋域內大陸／台灣用語同義。

擴展詞以空白接在原查詢後方，不取代原文。向量檢索會把整段（含擴展詞）
送去 embedding——刻意為之，讓向量往院內詞彙空間靠近。

Precision beats recall：錯誤擴展詞會污染 embedding，拿不準就不擴。
"""

from __future__ import annotations

import re

from sqlalchemy.orm import Session

from app.models.platform_setting import get_setting

#: 登錄表裡這一顆的 key。env 回退（``ANILA_QUERY_EXPANSION``，判準 ``!= "0"``）
#: 由 ``get_setting`` 那一層負責；本模組不再自己讀 env，否則會出現「畫面顯示
#: 已關閉、檢索照樣擴展」這種沒有錯誤訊息的分歧。
SETTING_KEY = "intl.query_expansion"

_MAX_ADDED = 12
_ROC_MIN = 80
_ROC_MAX = 130
# 西元範圍由 ROC 對稱推導：80–130 ⇔ 1991–2041（不可再寫死不對稱上下限）
_WESTERN_MIN = _ROC_MIN + 1911
_WESTERN_MAX = _ROC_MAX + 1911

# 最長優先；值可為多個擴展詞（如 中科院 → 全名 + NCSIST）
_SYNONYMS: list[tuple[str, tuple[str, ...]]] = [
    ("激光雷達", ("光達",)),
    ("人工智能", ("人工智慧",)),
    ("中科院", ("國家中山科學研究院", "NCSIST")),
    ("激光", ("雷射",)),
    ("導彈", ("飛彈",)),
    ("芯片", ("晶片",)),
    ("算法", ("演算法",)),
    ("航天", ("航太",)),
    ("信息", ("資訊",)),
]
_SYNONYMS.sort(key=lambda kv: -len(kv[0]))

# 民國／裸 N 年／N年度；數字須完整邊界（前後不得再接 digit）。
# 「年」後不得接 式/型/級（如 113年式步槍）。
_ROC_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"民國\s*(?<!\d)(\d{2,3})(?!\d)\s*年(?![式型級])"),
    re.compile(r"(?<!\d)(\d{2,3})(?!\d)年度"),
    re.compile(r"「(?<!\d)(\d{2,3})(?!\d)年」"),
    # 裸「113年」「113 年」；排除 式/型/級 後綴
    re.compile(r"(?<!\d)(\d{2,3})(?!\d)\s*年(?![式型級度])"),
)

# 西元年必須有語境：後接「年」／「年度」，或完整日期 2024/05、2024-05。
# 裸四位數（ISO 2024、型號2024）一律不擴。
_WESTERN_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?<!\d)(19\d{2}|20\d{2})(?!\d)\s*年(?:度)?"),
    re.compile(r"(?<!\d)(19\d{2}|20\d{2})(?!\d)[/-](?:0?[1-9]|1[0-2])(?!\d)"),
)

# 編號／型號／序號／第 + 數字 → 當識別碼，不當年份。
_ID_PREFIXES: tuple[str, ...] = ("編號", "型號", "序號", "第")


def _preceded_by_identifier(q: str, digit_start: int) -> bool:
    """數字是否落在識別碼語境（編號/型號/序號/第…）。

    實務守衛：略過數字左側空白後，檢查緊鄰視窗（最多 8 字）是否以
    上述前綴結尾。

    已知限制：不跨句／不跨標點消歧——例如「見編號。113年度」句號切開後
    仍會擴展；也無法辨識英文 ID 前綴（P-113、No.113）。拿不準時靠
    數字邊界與西元年語境要求擋下多數誤傷。
    """
    i = digit_start
    while i > 0 and q[i - 1].isspace():
        i -= 1
    window = q[max(0, i - 8) : i]
    return any(window.endswith(p) for p in _ID_PREFIXES)


def expand_query(db: Session, q: str) -> str:
    """回傳原查詢＋擴展詞（空白分隔）。``intl.query_expansion`` 關閉時原樣回傳。

    開關**每一次檢索都重新解一次**（DB 那一列 → env → 程式預設），所以管理員
    從畫面關掉之後，下一個檢索請求就不再擴展，不必重啟容器。
    """
    if not q:
        return q
    if not get_setting(db, SETTING_KEY):
        return q

    added: list[str] = []
    seen: set[str] = set()

    def _add(term: str) -> None:
        if len(added) >= _MAX_ADDED:
            return
        if not term or term in seen or term in q:
            return
        seen.add(term)
        added.append(term)

    for pat in _ROC_PATTERNS:
        for m in pat.finditer(q):
            if _preceded_by_identifier(q, m.start(1)):
                continue
            n = int(m.group(1))
            if _ROC_MIN <= n <= _ROC_MAX:
                _add(str(n + 1911))

    for pat in _WESTERN_PATTERNS:
        for m in pat.finditer(q):
            if _preceded_by_identifier(q, m.start(1)):
                continue
            y = int(m.group(1))
            if _WESTERN_MIN <= y <= _WESTERN_MAX:
                _add(f"民國{y - 1911}年")

    # 子字串命中即可（允許重疊：激光雷達 → 光達，同時激光 → 雷射）
    # 中科院：若查詢已含「中國科學院」則不做 NCSIST／全名擴展（消歧）。
    has_cas = "中國科學院" in q
    for key, vals in _SYNONYMS:
        if key == "中科院" and has_cas:
            continue
        if key in q:
            for v in vals:
                _add(v)

    if not added:
        return q
    return q + " " + " ".join(added)
