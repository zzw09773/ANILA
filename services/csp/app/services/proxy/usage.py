"""Usage serialization + token estimation helpers for the CSP proxy.

Split verbatim out of ``app/services/proxy_service.py`` (Doc-10 Slice 1,
behavior-preserving refactor).
"""
import json
import math
import re

def _flatten_content(content) -> str:
    """Best-effort flattening of OpenAI-compatible message content."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
                continue
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                parts.append(str(block.get("text", "")))
            elif "text" in block:
                parts.append(str(block.get("text", "")))
            elif "content" in block:
                parts.append(str(block.get("content", "")))
        return "\n".join(p for p in parts if p)
    return str(content)


def _serialize_request_for_usage(request_body: dict) -> str:
    """Flatten request payload into a prompt string for token estimation.

    Sprint 5: also handles embedding-shaped bodies — ``input`` may be a
    string or list of strings (OpenAI-compatible). Without this, embedding
    calls always estimated to 0 tokens because the loops below only
    looked at chat ``messages`` / ``tools``.
    """
    parts: list[str] = []

    # Embedding-shape: ``input`` is the prompt material.
    embedding_input = request_body.get("input")
    if isinstance(embedding_input, str):
        parts.append(embedding_input)
    elif isinstance(embedding_input, list):
        for item in embedding_input:
            if isinstance(item, str):
                parts.append(item)

    for msg in request_body.get("messages", []) or []:
        if not isinstance(msg, dict):
            continue
        role = str(msg.get("role", "user"))
        content = _flatten_content(msg.get("content"))
        if content:
            parts.append(f"{role}: {content}")

    for tool in request_body.get("tools", []) or []:
        try:
            parts.append(json.dumps(tool, ensure_ascii=False, sort_keys=True))
        except TypeError:
            parts.append(str(tool))

    return "\n".join(parts)


def _extract_response_text(result: dict) -> str:
    """Extract assistant-visible text from a non-streaming response."""
    texts: list[str] = []
    for choice in result.get("choices", []) or []:
        if not isinstance(choice, dict):
            continue
        message = choice.get("message") or {}
        if isinstance(message, dict):
            content = _flatten_content(message.get("content"))
            if content:
                texts.append(content)
            reasoning = message.get("reasoning") or message.get("reasoning_content")
            if reasoning:
                texts.append(str(reasoning))
    return "\n".join(texts)


def _extract_stream_text(chunk: dict) -> str:
    """Extract text/reasoning/tool-call deltas from a streaming chunk."""
    parts: list[str] = []
    choices = chunk.get("choices")
    if not isinstance(choices, list):
        choice = chunk.get("choice")
        if isinstance(choice, list):
            choices = choice
        elif isinstance(choice, dict):
            choices = [choice]
        else:
            choices = []
    for choice in choices:
        if not isinstance(choice, dict):
            continue
        delta = choice.get("delta") or {}
        if not isinstance(delta, dict):
            delta = {}
        message = choice.get("message") or {}
        if not isinstance(message, dict):
            message = {}
        content = (
            _flatten_content(delta.get("content"))
            or _flatten_content(message.get("content"))
            or _flatten_content(choice.get("text"))
        )
        if content:
            parts.append(content)
        for key in ("reasoning", "reasoning_content"):
            value = delta.get(key) or message.get(key)
            if value:
                parts.append(str(value))
        for tool_call in delta.get("tool_calls", []) or []:
            if not isinstance(tool_call, dict):
                continue
            fn = tool_call.get("function") or {}
            if isinstance(fn, dict):
                if fn.get("name"):
                    parts.append(str(fn["name"]))
                if fn.get("arguments"):
                    parts.append(str(fn["arguments"]))
    return "".join(parts)


def _estimate_token_count(model_name: str | None, text: str) -> int:
    """Estimate tokens when the upstream does not provide usage.

    Strategy:
    1. Try `tiktoken` when available.
    2. Fall back to a mixed heuristic for ASCII/CJK text.
    """
    if not text:
        return 0

    if model_name:
        try:
            import tiktoken  # type: ignore[import-not-found]

            try:
                encoder = tiktoken.encoding_for_model(model_name)
            except KeyError:
                encoder = tiktoken.get_encoding("cl100k_base")
            return len(encoder.encode(text))
        except Exception:
            pass

    cjk_chars = len(re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]", text))
    ascii_chars = sum(1 for ch in text if ord(ch) < 128 and not ch.isspace())
    other_chars = sum(
        1 for ch in text if not ch.isspace() and ord(ch) >= 128 and not re.match(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]", ch)
    )
    wordish = len(re.findall(r"[A-Za-z0-9_]+|[^\w\s]", text))
    heuristic = math.ceil((ascii_chars / 4.0) + (cjk_chars * 1.15) + (other_chars / 2.0))
    return max(wordish, heuristic, 1)
