"""SSE parsing / aggregation helpers for the CSP proxy.

Split verbatim out of ``app/services/proxy_service.py`` (Doc-10 Slice 1,
behavior-preserving refactor).
"""
import json
import time

def _parse_sse_block(block: str) -> tuple[str | None, str | None]:
    event_name = None
    data_lines: list[str] = []
    for line in block.splitlines():
        if line.startswith("event:"):
            event_name = line[6:].strip()
        elif line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
    data = "\n".join(data_lines) if data_lines else None
    return event_name, data


def _aggregate_sse_to_chat_completion(sse_text: str, model_name: str) -> dict:
    """Fold an OpenAI-SSE stream body into a single chat.completion dict.

    Concatenates all ``choices[0].delta.content`` / ``.reasoning_content``
    pieces and preserves the last non-null ``finish_reason``. Used when we
    asked for non-stream but the upstream only speaks SSE.
    """
    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    finish_reason: str | None = None
    last_id: str | None = None
    usage: dict | None = None

    for raw_block in sse_text.split("\n\n"):
        data = None
        for line in raw_block.splitlines():
            if line.startswith("data:"):
                data = line[5:].strip()
                break
        if not data or data == "[DONE]":
            continue
        try:
            obj = json.loads(data)
        except json.JSONDecodeError:
            continue
        last_id = obj.get("id") or last_id
        if obj.get("usage"):
            usage = obj["usage"]
        choices = obj.get("choices") or []
        if not choices:
            continue
        ch = choices[0]
        delta = ch.get("delta") or ch.get("message") or {}
        piece = delta.get("content")
        if isinstance(piece, str):
            content_parts.append(piece)
        rpiece = delta.get("reasoning_content") or delta.get("reasoning")
        if isinstance(rpiece, str):
            reasoning_parts.append(rpiece)
        if ch.get("finish_reason"):
            finish_reason = ch["finish_reason"]

    message: dict = {"role": "assistant", "content": "".join(content_parts)}
    if reasoning_parts:
        merged = "".join(reasoning_parts)
        message["reasoning_content"] = merged
        message["reasoning"] = merged

    return {
        "id": last_id or f"chatcmpl-agg-{int(time.time())}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model_name,
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason or "stop"}],
        "usage": usage or {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }
