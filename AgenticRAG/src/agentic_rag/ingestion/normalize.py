"""Traditional Chinese text normalization.

Applied symmetrically at ingestion (before chunking) and at query time so the
same canonical form ends up in the index and in user input. Pure stdlib.

What it canonicalizes:
- NFKC: full-width ASCII, ligatures, compatibility forms unified.
- A small set of CJK punctuation that NFKC leaves alone (，。；：！？etc.).
- Whitespace runs and stray single ASCII spaces between CJK ideographs (a
  common PDF text-extraction artefact).
"""
from __future__ import annotations

import re
import unicodedata

# NFKC does not fold these into ASCII counterparts. Mapping keeps query and
# stored content symbolically aligned for both FTS and ILIKE substring search.
_CJK_PUNCT_TO_ASCII: dict[str, str] = {
    "、": ",",   # 、
    "，": ",",   # ，
    "。": ".",   # 。
    "；": ";",   # ；
    "：": ":",   # ：
    "！": "!",   # ！
    "？": "?",   # ？
    "（": "(",   # （
    "）": ")",   # ）
    "‘": "'",
    "’": "'",
    "“": '"',
    "”": '"',
}

_PUNCT_TRANS = str.maketrans(_CJK_PUNCT_TO_ASCII)

_CJK_CHAR = r"[㐀-䶿一-鿿]"
_CJK_INTERIOR_SPACE = re.compile(rf"({_CJK_CHAR}) ({_CJK_CHAR})")
_INLINE_WS = re.compile(r"[ \t 　]+")


def normalize_zh(text: str) -> str:
    """Canonicalize Traditional Chinese text. Idempotent."""
    if not text:
        return text

    out = unicodedata.normalize("NFKC", text)
    out = out.translate(_PUNCT_TRANS)
    out = "\n".join(_INLINE_WS.sub(" ", line) for line in out.split("\n"))

    while True:
        new_out = _CJK_INTERIOR_SPACE.sub(r"\1\2", out)
        if new_out == out:
            break
        out = new_out

    return "\n".join(line.rstrip() for line in out.split("\n"))


def normalize_query(text: str) -> str:
    """Query-side normalization. Same canonical form as documents."""
    return normalize_zh(text)
