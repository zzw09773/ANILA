"""Prompt contract + provider-side loop discard."""
from __future__ import annotations

from anila_core.providers.vision import _DEFAULT_PROMPT


def test_prompt_asks_zh_tw_and_keeps_source_terms():
    assert "繁體中文" in _DEFAULT_PROMPT
    assert "原樣保留" in _DEFAULT_PROMPT
    assert "Transition Area" in _DEFAULT_PROMPT
    assert "same language as any text" not in _DEFAULT_PROMPT
    assert "verbatim (OCR)" not in _DEFAULT_PROMPT


def test_prompt_forbids_invented_ocr():
    assert "不要臆造 OCR" in _DEFAULT_PROMPT or "不要臆造" in _DEFAULT_PROMPT
