"""Spreadsheet formula-injection neutralization (CSV / XLSX).

Prefix the six-character closed trigger set ``= + - @ \\t \\r`` (OWASP)
with a single quote so spreadsheet software stores the cell as text.
The trigger set is defined by spreadsheet parsers, not by attackers;
it is enumerable, so completing it is the right move (not a growing
blacklist of attacker imagination).

Residual risk: the leading apostrophe mutates exported data. A genuine
value that starts with ``-`` becomes ``'-…``. The file is no longer a
verbatim original. Industry-standard tradeoff; do not skip neutralization
to preserve byte-for-byte identity.

Twin implementation (這兩份會漂開，改一邊要改另一邊):
``services/csp/app/utils/csv_formula.py:26``
(``csv_formula_safe``).
"""
from __future__ import annotations

from typing import Any

# Closed set. Spreadsheet parsers define this, not attackers.
FORMULA_TRIGGER_PREFIXES: tuple[str, ...] = ("=", "+", "-", "@", "\t", "\r")


def csv_formula_safe(value: Any) -> str:
    """Return ``str(value)`` with a leading ``'`` if the first character is
    in :data:`FORMULA_TRIGGER_PREFIXES`. ``None`` becomes ``""``.

    Twin: ``services/csp/app/utils/csv_formula.py:26`` (``csv_formula_safe``).
    這兩份會漂開，改一邊要改另一邊。

    涵蓋範圍（2026-08-24 審查長要求寫明）：**本函式只檢查第一個字元**。
    觸發集合是封閉的，檢查的涵蓋範圍不是：任何會讓試算表跳過前導字元的
    讀取器都可能繞過它。已實測 LibreOffice 預設匯入不會（前導空白／tab／
    引號皆存成文字）；**Excel 未測**。若值是被內插進未經 ``csv.writer``
    引號化的文字行（例如註解列），逗號／引號要先另外處理，這裡看不到。
    """
    text = "" if value is None else str(value)
    if text[:1] in FORMULA_TRIGGER_PREFIXES:
        return "'" + text
    return text
