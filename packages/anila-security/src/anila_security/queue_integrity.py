"""HMAC proof for authority-bearing jobs sent through shared Redis queues."""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any, Mapping


def create_queue_proof(
    key: str, *, task_name: str, payload: Mapping[str, Any]
) -> str:
    if len(key) < 32:
        raise ValueError("queue integrity key must contain at least 32 characters")
    canonical = json.dumps(
        {"task_name": task_name, "payload": dict(payload)},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hmac.new(key.encode("utf-8"), canonical, hashlib.sha256).hexdigest()


def verify_queue_proof(
    key: str,
    *,
    task_name: str,
    payload: Mapping[str, Any],
    proof: str | None,
) -> None:
    expected = create_queue_proof(key, task_name=task_name, payload=payload)
    if not isinstance(proof, str) or not hmac.compare_digest(proof, expected):
        raise PermissionError("queue job integrity proof is missing or invalid")
