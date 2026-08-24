"""Judge VLM caption *content*, not length.

Length only says the decoder hit a ceiling. A 601-char loop of
``$\\text{}`` and a 601-char real description share that signal;
only the first is garbage. Judge by whether a short token sequence
repeats.

This list of detectors will not grow by itself — add a case when a
new failure shape is measured, not guessed.
"""
from __future__ import annotations

import re
from typing import Literal


CaptionKind = Literal["ok", "repetitive", "truncated"]

# A token that occupies most of the tail is a loop, not a sentence.
_LATEX_EMPTY = re.compile(r"(?:\\text\{\}\s*){6,}")
_HTML_TAG_STUTTER = re.compile(r"(?:</?[a-zA-Z][^>]*>\s*){8,}")
_TRUNCATION_MARK = "…（截斷）"


def is_repetitive_caption(text: str) -> bool:
    """True when a short token occupies most of the string.

    Criterion is content, not length. A coherent paragraph of any
    length returns False.
    """
    s = (text or "").strip()
    if len(s) < 40:
        return False
    if _LATEX_EMPTY.search(s) or _HTML_TAG_STUTTER.search(s):
        return True
    # Adjacent copy of a short token (decoder stutter), not a sentence
    # that happens to appear twice in a long caption.
    n = len(s)
    for width in (4, 6, 8, 12):
        runs = 0
        i = 0
        while i + 2 * width <= n:
            if s[i : i + width] == s[i + width : i + 2 * width] and not s[i : i + width].isspace():
                runs += 1
                i += width
                if runs >= 5:
                    return True
            else:
                runs = 0
                i += 1
    return False


def classify_caption(text: str, *, hit_token_limit: bool = False) -> CaptionKind:
    """repetitive | truncated | ok. Length never decides by itself."""
    if is_repetitive_caption(text):
        return "repetitive"
    if hit_token_limit:
        return "truncated"
    return "ok"


def mark_truncated(text: str) -> str:
    s = (text or "").rstrip()
    if not s:
        return s
    if s.endswith(_TRUNCATION_MARK):
        return s
    return s + _TRUNCATION_MARK


def finish_reason_hit_limit(finish_reason: str | None) -> bool:
    return (finish_reason or "").lower() in {"length", "max_tokens"}
