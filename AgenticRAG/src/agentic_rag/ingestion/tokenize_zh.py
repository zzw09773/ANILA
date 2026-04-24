"""CJK-aware tokenizer for PostgreSQL ``tsvector('simple', ...)``.

Default backend: character-bigram for CJK runs, lowercased word tokens for
ASCII. Zero external dependencies. Bigrams give us substring-style recall
for Traditional Chinese without a real word segmenter — enough that FTS
beats ILIKE on multi-character queries while ILIKE still backstops single
characters.

Optional CKIPtagger backend (Academia Sinica, Taiwan) is loaded lazily
when ``RAG_TOKENIZER=ckip`` and the ``ckip-tagger`` package is installed.
"""
from __future__ import annotations

import logging
import os
from typing import Callable, Optional

logger = logging.getLogger(__name__)


def _is_cjk(ch: str) -> bool:
    cp = ord(ch)
    return 0x4E00 <= cp <= 0x9FFF or 0x3400 <= cp <= 0x4DBF


def tokenize_bigram(text: str) -> str:
    """Return a space-separated token stream from *text*."""
    if not text:
        return ""

    tokens: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if _is_cjk(ch):
            j = i
            while j < n and _is_cjk(text[j]):
                j += 1
            run = text[i:j]
            if len(run) == 1:
                tokens.append(run)
            else:
                tokens.extend(run[k : k + 2] for k in range(len(run) - 1))
            i = j
        elif ch.isalnum():
            j = i
            while j < n and text[j].isalnum() and not _is_cjk(text[j]):
                j += 1
            tokens.append(text[i:j].lower())
            i = j
        else:
            i += 1
    return " ".join(tokens)


_ckip_segmenter: Optional[Callable[[list[str]], list[list[str]]]] = None
_ckip_tried = False


def _try_load_ckip() -> Optional[Callable[[list[str]], list[list[str]]]]:
    global _ckip_segmenter, _ckip_tried
    if _ckip_tried:
        return _ckip_segmenter
    _ckip_tried = True
    try:
        from ckip_transformers.nlp import CkipWordSegmenter  # type: ignore

        ws = CkipWordSegmenter(model="bert-base")
        _ckip_segmenter = lambda texts: ws(texts)  # noqa: E731
        logger.info("CKIP word segmenter loaded (ckip_transformers)")
    except Exception as exc:
        logger.info("CKIP not available, falling back to bigram tokenizer: %s", exc)
        _ckip_segmenter = None
    return _ckip_segmenter


def tokenize_ckip(text: str) -> str:
    seg = _try_load_ckip()
    if seg is None:
        return tokenize_bigram(text)
    try:
        words = seg([text])[0]
    except Exception as exc:
        logger.warning("CKIP segmentation failed, falling back: %s", exc)
        return tokenize_bigram(text)
    return " ".join(w.strip() for w in words if w and w.strip())


def tokenize(text: str) -> str:
    """Tokenize *text* using the configured backend (env ``RAG_TOKENIZER``)."""
    backend = os.getenv("RAG_TOKENIZER", "bigram").lower()
    if backend == "ckip":
        return tokenize_ckip(text)
    return tokenize_bigram(text)
