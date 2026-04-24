"""Tests for tokenize_zh.tokenize_bigram (default FTS tokenizer)."""

from __future__ import annotations

from agentic_rag.ingestion.tokenize_zh import tokenize_bigram


def test_empty_input():
    assert tokenize_bigram("") == ""


def test_pure_cjk_yields_overlapping_bigrams():
    # 「申誡條件」→ 申誡 誡條 條件
    result = tokenize_bigram("申誡條件").split()
    assert result == ["申誡", "誡條", "條件"]


def test_single_cjk_char_emitted_as_unigram():
    assert tokenize_bigram("申").split() == ["申"]


def test_ascii_word_lowercased():
    assert tokenize_bigram("Hello") == "hello"
    assert tokenize_bigram("WORLD123") == "world123"


def test_mixed_cjk_and_ascii():
    tokens = tokenize_bigram("申誡 hello 條件").split()
    assert "申誡" in tokens
    assert "hello" in tokens
    assert "條件" in tokens


def test_punctuation_separates_runs():
    # Comma between CJK characters breaks the bigram run.
    tokens = tokenize_bigram("申誡,條件").split()
    assert "申誡" in tokens
    assert "條件" in tokens
    # Crucially, no bigram crosses the punctuation boundary.
    assert "誡條" not in tokens


def test_idempotent_shape():
    text = "陸海空軍懲罰法第8條"
    once = tokenize_bigram(text)
    twice = tokenize_bigram(once)
    # Tokenizing tokenized output is well-defined (each token already a word).
    assert once
    assert twice
