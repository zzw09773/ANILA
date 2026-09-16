"""Shared token approximation for compact + chunking.

No tiktoken / transformers. CJK ideographs are ~1 token each on the
campus GLM／Qwen tokenisers; Latin stays near 4 chars/token with the
same 4/3 pad compact already used so English estimates do not shrink.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

_CHARS_PER_TOKEN = 4
_LATIN_PAD = 4 / 3


def is_cjk(char: str) -> bool:
    """CJK Unified Ideographs, Hiragana, Katakana, Hangul Syllables."""
    if not char:
        return False
    c = char[0]
    return (
        "一" <= c <= "鿿"
        or "぀" <= c <= "ヿ"
        or "가" <= c <= "힯"
    )


def count_text_tokens(text: str) -> int:
    """Approximate tokens for a single string.

    Empty → 0. Non-empty always ≥ 1.
    """
    if not text:
        return 0
    cjk = 0
    other = 0
    for ch in text:
        if is_cjk(ch):
            cjk += 1
        else:
            other += 1
    latin = int((other / _CHARS_PER_TOKEN) * _LATIN_PAD) if other else 0
    return max(1, cjk + latin)


def flatten_openai_text(content: Any) -> str:
    """Collect visible text from an OpenAI-shaped content value."""
    if isinstance(content, str):
        return content
    if content is None:
        return ""
    if not isinstance(content, list):
        return str(content)
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
            continue
        if not isinstance(block, dict):
            continue
        text = block.get("text")
        if isinstance(text, str):
            parts.append(text)
            continue
        inner = block.get("content")
        if isinstance(inner, str):
            parts.append(inner)
        elif isinstance(inner, list):
            parts.append(flatten_openai_text(inner))
    return "".join(parts)


def count_openai_text_tokens(messages: Sequence[Mapping[str, Any]]) -> int:
    """Sum CJK-aware text tokens across OpenAI chat messages (no images)."""
    total = 0
    for msg in messages:
        if not isinstance(msg, Mapping):
            continue
        total += count_text_tokens(flatten_openai_text(msg.get("content")))
        reasoning = msg.get("reasoning") or msg.get("reasoning_content")
        if isinstance(reasoning, str):
            total += count_text_tokens(reasoning)
    return total


def parse_tokenize_response(payload: Any) -> int | None:
    """Read a count from vLLM / HuggingFace ``/tokenize`` JSON."""
    if isinstance(payload, int) and payload >= 0:
        return payload
    if not isinstance(payload, dict):
        return None
    count = payload.get("count")
    if isinstance(count, int) and count >= 0:
        return count
    for key in ("tokens", "token_ids", "input_ids"):
        tokens = payload.get(key)
        if isinstance(tokens, list):
            return len(tokens)
    length = payload.get("length")
    if isinstance(length, int) and length >= 0:
        return length
    return None


def prompt_text_for_tokenize(messages: Sequence[Mapping[str, Any]]) -> str:
    """Visible chat text for a model ``/tokenize`` prompt (no images)."""
    parts: list[str] = []
    for msg in messages:
        if not isinstance(msg, Mapping):
            continue
        text = flatten_openai_text(msg.get("content")).strip()
        if text:
            parts.append(text)
        reasoning = msg.get("reasoning") or msg.get("reasoning_content")
        if isinstance(reasoning, str) and reasoning.strip():
            parts.append(reasoning.strip())
    return "\n".join(parts)


def derive_tokenize_urls(endpoint_url: str) -> list[str]:
    """Guess vLLM-style tokenize paths from an OpenAI ``/v1`` base."""
    base = (endpoint_url or "").strip().rstrip("/")
    if not base.startswith(("http://", "https://")):
        return []
    urls: list[str] = []
    if base.endswith("/v1"):
        root = base[:-3].rstrip("/")
        urls.append(f"{root}/tokenize")
        urls.append(f"{base}/tokenize")
    else:
        urls.append(f"{base}/tokenize")
        urls.append(f"{base}/v1/tokenize")
    # unique, keep order
    seen: set[str] = set()
    out: list[str] = []
    for url in urls:
        if url not in seen:
            seen.add(url)
            out.append(url)
    return out
