"""把 CSP 的額度 429 原文交給畫面。其他上游內文仍然不外流。"""

from __future__ import annotations

import json


def quota_exceeded_message(status: int, body: str | None) -> str | None:
    if status != 429 or not body:
        return None
    try:
        payload = json.loads(body)
    except (TypeError, ValueError):
        return None
    detail = payload.get("detail") if isinstance(payload, dict) else None
    if not isinstance(detail, dict):
        return None
    if detail.get("code") != "quota_exceeded":
        return None
    message = detail.get("message")
    if not isinstance(message, str):
        return None
    text = message.strip()
    if not text.startswith("已達") or len(text) > 300:
        return None
    return text
