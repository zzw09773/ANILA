"""Strip data-URL / base64 images from older conversation turns.

Used as the first compact step: replace bulky image payloads with a short
placeholder so token estimates (and the upstream context window) shrink
before summarization or hard truncation.
"""

from __future__ import annotations

import copy
import re
from collections.abc import Mapping, Sequence
from typing import Any

from ..models.message import AssistantMessage, Message, UserMessage
from ..text.token_count import count_text_tokens, flatten_openai_text, prompt_text_for_tokenize

# Align with Claude Code IMAGE_MAX_TOKEN_SIZE: a vision image is never
# cheaper than a short paragraph, and never counted as the raw base64.
IMAGE_MIN_TOKENS = 800
IMAGE_MAX_TOKENS = 2000

# Embedded ``data:*;base64,`` fragments smaller than this stay intact.
_INLINE_BASE64_MIN_BYTES = 2048

_DATA_URL_RE = re.compile(
    r"data:([^;,]+);base64,([A-Za-z0-9+/=\s]+)",
    re.IGNORECASE,
)

_IMAGE_PART_TYPES = frozenset({"image", "image_url"})


def estimate_image_tokens(payload_b64: str) -> int:
    """Convert a base64 payload to clamped vision-token estimate.

    Uses the same chars/4 scale as text estimation, then clamps to
    ``[IMAGE_MIN_TOKENS, IMAGE_MAX_TOKENS]`` so a screenshot cannot be
    estimated as 0 (which would skip compact) or as tens of thousands
    (VLMs downsample).
    """
    compact = re.sub(r"\s+", "", payload_b64)
    raw = max(1, len(compact) // 4)
    return min(IMAGE_MAX_TOKENS, max(IMAGE_MIN_TOKENS, raw))


def image_placeholder(mime: str, decoded_bytes: int) -> str:
    kb = max(1, decoded_bytes // 1024)
    return f"[圖片已省略：{mime or 'image'}, ~{kb} KB]"


def measure_openai_content(content: Any) -> tuple[int, int]:
    """Return ``(text_chars, image_tokens)`` for an OpenAI-shaped content value."""
    if isinstance(content, str):
        return _measure_text(content)
    if not isinstance(content, list):
        if content is None:
            return 0, 0
        return len(str(content)), 0
    chars = 0
    images = 0
    for block in content:
        if isinstance(block, str):
            c, i = _measure_text(block)
            chars += c
            images += i
            continue
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype in _IMAGE_PART_TYPES:
            images += _tokens_for_image_part(block)
            continue
        if btype == "tool_result":
            c, i = measure_openai_content(block.get("content"))
            chars += c
            images += i
            continue
        text = block.get("text")
        if isinstance(text, str):
            c, i = _measure_text(text)
            chars += c
            images += i
            continue
        inner = block.get("content")
        if isinstance(inner, str):
            c, i = _measure_text(inner)
            chars += c
            images += i
        elif isinstance(inner, list):
            c, i = measure_openai_content(inner)
            chars += c
            images += i
    return chars, images


def _text_without_large_data_urls(text: str) -> str:
    """Drop bulky ``data:`` payloads so base64 is not counted as Latin."""

    def _repl(match: re.Match[str]) -> str:
        if _is_large_data_url(match):
            return ""
        return match.group(0)

    return _DATA_URL_RE.sub(_repl, text)


def visible_text_for_tokens(content: Any) -> str:
    """Flattened chat text with large inline images removed."""
    return _text_without_large_data_urls(flatten_openai_text(content))


def visible_prompt_for_tokenize(messages: Sequence[Mapping[str, Any]]) -> str:
    """Join visible chat text for a model ``/tokenize`` call."""
    return _text_without_large_data_urls(prompt_text_for_tokenize(messages))


def estimate_openai_image_tokens(messages: Sequence[Mapping[str, Any]]) -> int:
    """Clamped vision-token total; text is ignored."""
    images = 0
    for msg in messages:
        content = msg.get("content") if isinstance(msg, Mapping) else None
        _chars, img = measure_openai_content(content)
        images += img
    return images


def estimate_openai_tokens_with_images(messages: Sequence[Mapping[str, Any]]) -> int:
    """CJK-aware text tokens plus clamped per-image tokens.

    Compact thresholds use this count. Latin keeps the old 4/3 pad so
    English estimates stay conservative; CJK is 1 token/char (GLM／Qwen).
    """
    tokens = 0
    images = 0
    for msg in messages:
        content = msg.get("content") if isinstance(msg, Mapping) else None
        _chars, img = measure_openai_content(content)
        tokens += count_text_tokens(visible_text_for_tokens(content))
        images += img
        if isinstance(msg, Mapping):
            reasoning = msg.get("reasoning") or msg.get("reasoning_content")
            if isinstance(reasoning, str):
                tokens += count_text_tokens(reasoning)
    return tokens + images


def estimate_message_tokens_with_images(messages: Sequence[Message]) -> int:
    tokens = 0
    images = 0
    for msg in messages:
        content = getattr(msg, "content", None)
        if content is None:
            continue
        _chars, img = measure_openai_content(content)
        tokens += count_text_tokens(visible_text_for_tokens(content))
        images += img
        reasoning = getattr(msg, "reasoning", None) or getattr(msg, "reasoning_content", None)
        if isinstance(reasoning, str):
            tokens += count_text_tokens(reasoning)
    return tokens + images


def strip_images_openai(
    messages: list[dict[str, Any]],
    *,
    keep_recent_turns: int = 1,
) -> tuple[list[dict[str, Any]], int]:
    """Replace older-turn data-URL images with placeholders.

    Returns ``(new_messages, tokens_saved)``. Does not mutate ``messages``.
    """
    if not messages:
        return [], 0
    cloned: list[dict[str, Any]] = copy.deepcopy(messages)
    system, turns = _split_openai_turns(cloned)
    keep_n = max(1, keep_recent_turns)
    recent_start = max(0, len(turns) - keep_n)

    out: list[dict[str, Any]] = []
    for msg in system:
        _strip_openai_message_inplace(msg)
        out.append(msg)
    for idx, turn in enumerate(turns):
        if idx < recent_start:
            for msg in turn:
                _strip_openai_message_inplace(msg)
        out.extend(turn)

    before = estimate_openai_tokens_with_images(messages)
    after = estimate_openai_tokens_with_images(out)
    return out, max(0, before - after)


def strip_images_messages(
    messages: list[Message],
    *,
    keep_recent_turns: int = 1,
) -> tuple[list[Message], int]:
    """Same as :func:`strip_images_openai` for QueryEngine ``Message`` objects."""
    if not messages:
        return [], 0
    cloned: list[Message] = [m.model_copy(deep=True) for m in messages]
    turns = _split_message_turns(cloned)
    keep_n = max(1, keep_recent_turns)
    recent_start = max(0, len(turns) - keep_n)

    out: list[Message] = []
    for idx, turn in enumerate(turns):
        if idx < recent_start:
            for i, msg in enumerate(turn):
                turn[i] = _strip_message(msg)
        out.extend(turn)

    before = estimate_message_tokens_with_images(messages)
    after = estimate_message_tokens_with_images(out)
    return out, max(0, before - after)


def _decoded_bytes(payload_b64: str) -> int:
    compact = re.sub(r"\s+", "", payload_b64)
    return len(compact) * 3 // 4


def _measure_text(text: str) -> tuple[int, int]:
    chars = len(text)
    images = 0
    for match in _DATA_URL_RE.finditer(text):
        if not _is_large_data_url(match):
            continue
        chars -= len(match.group(0))
        images += estimate_image_tokens(match.group(2))
    return max(0, chars), images


def _is_large_data_url(match: re.Match[str]) -> bool:
    payload = match.group(2)
    return _decoded_bytes(payload) >= _INLINE_BASE64_MIN_BYTES or len(match.group(0)) > _INLINE_BASE64_MIN_BYTES


def _parse_data_url(url: str) -> tuple[str, str] | None:
    match = _DATA_URL_RE.search(url)
    if not match:
        return None
    return match.group(1), match.group(2)


def _placeholder_for_url(url: str) -> str | None:
    parsed = _parse_data_url(url)
    if parsed is None:
        return None
    mime, payload = parsed
    return image_placeholder(mime, _decoded_bytes(payload))


def _image_url_from_part(part: Mapping[str, Any]) -> str | None:
    raw = part.get("image_url")
    if isinstance(raw, str):
        return raw
    if isinstance(raw, Mapping):
        url = raw.get("url")
        return url if isinstance(url, str) else None
    url = part.get("url")
    return url if isinstance(url, str) else None


def _tokens_for_image_part(part: Mapping[str, Any]) -> int:
    url = _image_url_from_part(part)
    if isinstance(url, str) and url.lower().startswith("data:"):
        parsed = _parse_data_url(url)
        if parsed is not None:
            return estimate_image_tokens(parsed[1])
        return IMAGE_MIN_TOKENS
    source = part.get("source")
    if isinstance(source, Mapping):
        data = source.get("data")
        if isinstance(data, str) and data:
            if data.lower().startswith("data:"):
                parsed = _parse_data_url(data)
                if parsed is not None:
                    return estimate_image_tokens(parsed[1])
            return estimate_image_tokens(data)
    return 0


def _placeholder_for_image_part(part: Mapping[str, Any]) -> str | None:
    url = _image_url_from_part(part)
    if isinstance(url, str) and url.lower().startswith("data:"):
        return _placeholder_for_url(url)
    source = part.get("source")
    if isinstance(source, Mapping):
        data = source.get("data")
        mime = source.get("media_type") or source.get("mime_type") or "image"
        if isinstance(data, str) and data:
            if data.lower().startswith("data:"):
                return _placeholder_for_url(data)
            return image_placeholder(str(mime), _decoded_bytes(data))
    return None


def _replace_embedded_data_urls(text: str) -> str:
    def _repl(match: re.Match[str]) -> str:
        if not _is_large_data_url(match):
            return match.group(0)
        return image_placeholder(match.group(1), _decoded_bytes(match.group(2)))

    return _DATA_URL_RE.sub(_repl, text)


def _strip_content_value(content: Any) -> Any:
    if isinstance(content, str):
        return _replace_embedded_data_urls(content)
    if not isinstance(content, list):
        return content
    new_parts: list[Any] = []
    for part in content:
        if isinstance(part, str):
            new_parts.append(_replace_embedded_data_urls(part))
            continue
        if not isinstance(part, dict):
            new_parts.append(part)
            continue
        btype = part.get("type")
        if btype in _IMAGE_PART_TYPES:
            placeholder = _placeholder_for_image_part(part)
            if placeholder is not None:
                new_parts.append({"type": "text", "text": placeholder})
                continue
        if btype == "tool_result":
            new_part = dict(part)
            new_part["content"] = _strip_content_value(part.get("content"))
            new_parts.append(new_part)
            continue
        new_part = dict(part)
        if isinstance(new_part.get("text"), str):
            new_part["text"] = _replace_embedded_data_urls(new_part["text"])
        elif isinstance(new_part.get("content"), (str, list)):
            new_part["content"] = _strip_content_value(new_part["content"])
        new_parts.append(new_part)
    return new_parts


def _strip_openai_message_inplace(msg: dict[str, Any]) -> None:
    if "content" in msg:
        msg["content"] = _strip_content_value(msg.get("content"))


def _strip_message(msg: Message) -> Message:
    if not isinstance(msg, (UserMessage, AssistantMessage)):
        return msg
    new_content = _strip_content_value(msg.content)
    return msg.model_copy(update={"content": new_content})


def _is_tool_result_openai(msg: Mapping[str, Any]) -> bool:
    content = msg.get("content")
    if not isinstance(content, list):
        return False
    return any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content)


def _split_openai_turns(
    messages: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[list[dict[str, Any]]]]:
    system: list[dict[str, Any]] = []
    convo: list[dict[str, Any]] = []
    for msg in messages:
        if msg.get("role") == "system":
            system.append(msg)
        else:
            convo.append(msg)
    turns: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for msg in convo:
        if msg.get("role") == "user" and current and not _is_tool_result_openai(msg):
            turns.append(current)
            current = []
        current.append(msg)
    if current:
        turns.append(current)
    return system, turns


def _is_tool_result_message(msg: Message) -> bool:
    if not isinstance(msg, UserMessage):
        return False
    content = msg.content
    return isinstance(content, list) and any(
        isinstance(b, dict) and b.get("type") == "tool_result" for b in content
    )


def _split_message_turns(messages: list[Message]) -> list[list[Message]]:
    turns: list[list[Message]] = []
    current: list[Message] = []
    for msg in messages:
        if isinstance(msg, UserMessage) and current and not _is_tool_result_message(msg):
            turns.append(current)
            current = []
        current.append(msg)
    if current:
        turns.append(current)
    return turns
