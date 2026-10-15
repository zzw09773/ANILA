"""Distinguish recovered document text from parser-generated placeholders."""

from __future__ import annotations

import re

_START = re.compile(r"\[\[IMAGE:|!\[|<img\b|<!--", re.I)
_PAGE = re.compile(
    r"^\s*(?:#{1,6}\s*)?(?:\[\[PAGE:\s*\d+\]\]|page\s*\d+(?:\s*(?:of|/)\s*\d+)?|第\s*\d+\s*頁)\s*$",
    re.I,
)


def has_recovered_text(content: str) -> bool:
    """Ignore image/page markers, empty Markdown tables, and whitespace."""
    pieces: list[str] = []
    cursor = 0
    while match := _START.search(content, cursor):
        pieces.append(content[cursor : match.start()])
        token = match.group().lower()
        end = match.end()
        delimiter = "]]" if token == "[[image:" else "-->" if token == "<!--" else ">"
        if token == "![":
            label_end = content.find("](", end)
            if label_end < 0:
                cursor = match.start()
                break
            end, delimiter = label_end + 2, ")"
        close = content.find(delimiter, end)
        if close < 0:
            cursor = match.start()
            break
        cursor = close + len(delimiter)
    pieces.append(content[cursor:])
    text = "".join(pieces)
    for line in text.splitlines():
        if _PAGE.fullmatch(line):
            continue
        if not line.strip():
            continue
        if "|" in line and not line.strip(" \t\r\n|:-"):
            continue
        return True
    return False
