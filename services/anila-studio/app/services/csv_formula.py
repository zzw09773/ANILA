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
    """
    text = "" if value is None else str(value)
    if text[:1] in FORMULA_TRIGGER_PREFIXES:
        return "'" + text
    return text
