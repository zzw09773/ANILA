"""CJK-aware token approximation and /tokenize JSON helpers."""

from __future__ import annotations

from anila_core.text.token_count import (
    count_text_tokens,
    derive_tokenize_urls,
    flatten_openai_text,
    is_cjk,
    parse_tokenize_response,
    prompt_text_for_tokenize,
)


def test_cjk_ranges() -> None:
    assert is_cjk("中")
    assert is_cjk("あ")
    assert is_cjk("한")
    assert not is_cjk("a")
    assert not is_cjk(" ")
    assert not is_cjk("")


def test_count_empty_and_latin_pad() -> None:
    assert count_text_tokens("") == 0
    assert count_text_tokens("a") == 1
    assert count_text_tokens("a" * 120) == 40
    assert count_text_tokens("中" * 120) == 120
    assert count_text_tokens("中a" * 10) == 10 + 3  # 10 CJK + int(10/4*4/3)=3


def test_flatten_skips_image_parts() -> None:
    text = flatten_openai_text([
        {"type": "text", "text": "看"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
    ])
    assert text == "看"


def test_parse_tokenize_shapes() -> None:
    assert parse_tokenize_response(7) == 7
    assert parse_tokenize_response({"count": 12}) == 12
    assert parse_tokenize_response({"tokens": [1, 2, 3]}) == 3
    assert parse_tokenize_response({"token_ids": [9, 8]}) == 2
    assert parse_tokenize_response({"length": 4}) == 4
    assert parse_tokenize_response({"nope": True}) is None
    assert parse_tokenize_response(-1) is None


def test_derive_tokenize_urls_from_v1() -> None:
    assert derive_tokenize_urls("http://glm:8000/v1") == [
        "http://glm:8000/tokenize",
        "http://glm:8000/v1/tokenize",
    ]
    assert derive_tokenize_urls("grpc://glm:8000") == []
    assert derive_tokenize_urls("") == []


def test_prompt_text_joins_roles() -> None:
    prompt = prompt_text_for_tokenize([
        {"role": "user", "content": "問"},
        {"role": "assistant", "content": "答", "reasoning": "想"},
    ])
    assert prompt == "問\n答\n想"
