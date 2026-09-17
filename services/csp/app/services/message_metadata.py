"""Message metadata size guard with reasoning-only degrade.

The 64KB envelope stays. A legal long ``reasoning`` string is no longer
treated as “impossible metadata”; it is shortened from the start (or
omitted) and the persist status is stored next to it. Any other field
that blows the budget still 413s the whole write.
"""
from __future__ import annotations

import json
from typing import Optional

from fastapi import HTTPException


MAX_METADATA_BYTES = 64 * 1024
REASONING_PERSIST_KEY = "reasoning_persist"
# Below this, a kept prefix is not a useful fragment — omit instead.
MIN_REASONING_FRAGMENT_CHARS = 16


def metadata_utf8_size(metadata: dict) -> int:
    return len(json.dumps(metadata, ensure_ascii=False).encode("utf-8"))


def _persist_status(
    status: str,
    original_chars: int,
    kept_chars: int,
    *,
    reason: str | None = None,
) -> dict:
    out = {
        "status": status,
        "original_chars": original_chars,
        "kept_chars": kept_chars,
    }
    if reason:
        out["reason"] = reason
    return out


def _with_reasoning(base: dict, text: Optional[str], persist: dict) -> dict:
    out = {k: v for k, v in base.items() if k not in ("reasoning", REASONING_PERSIST_KEY)}
    if text:
        out["reasoning"] = text
    out[REASONING_PERSIST_KEY] = persist
    return out


def fit_reasoning_into_metadata_budget(metadata: dict) -> dict:
    """Return a copy whose serialized size prefers keeping the body write.

    Caller still runs :func:`prepare_message_metadata` so a leftover oversize
    (other fields) becomes 413.
    """
    if not isinstance(metadata, dict):
        return metadata
    reasoning = metadata.get("reasoning")
    if not isinstance(reasoning, str) or reasoning == "":
        return dict(metadata)

    original_chars = len(reasoning)
    base = dict(metadata)
    incoming_persist = metadata.get(REASONING_PERSIST_KEY)
    incoming_original = (
        incoming_persist.get("original_chars")
        if isinstance(incoming_persist, dict)
        else None
    )
    incoming_status = (
        incoming_persist.get("status")
        if isinstance(incoming_persist, dict)
        else None
    )

    full = _with_reasoning(
        base,
        reasoning,
        _persist_status("full", original_chars, original_chars),
    )
    if metadata_utf8_size(full) <= MAX_METADATA_BYTES:
        # A prior pass already dropped the tail. Do not relabel the leftover
        # prefix as a complete persist just because it now fits.
        if (
            incoming_status == "truncated"
            and isinstance(incoming_original, int)
            and incoming_original > original_chars
        ):
            return _with_reasoning(
                base,
                reasoning,
                _persist_status(
                    "truncated",
                    incoming_original,
                    original_chars,
                    reason="over_budget",
                ),
            )
        return full

    omitted = _with_reasoning(
        base,
        None,
        _persist_status("omitted", original_chars, 0, reason="over_budget"),
    )
    if metadata_utf8_size(omitted) > MAX_METADATA_BYTES:
        # Other fields already overflow. Do not delete them to sneak through.
        return dict(metadata)

    lo, hi = 0, original_chars
    best = omitted
    while lo <= hi:
        mid = (lo + hi) // 2
        prefix = reasoning[:mid]
        if mid == 0:
            cand = omitted
        else:
            cand = _with_reasoning(
                base,
                prefix,
                _persist_status(
                    "truncated", original_chars, len(prefix), reason="over_budget",
                ),
            )
        if metadata_utf8_size(cand) <= MAX_METADATA_BYTES:
            best = cand
            lo = mid + 1
        else:
            hi = mid - 1

    kept = best.get("reasoning") or ""
    if len(kept) < MIN_REASONING_FRAGMENT_CHARS:
        return omitted
    return best


def prepare_message_metadata(metadata: Optional[dict]) -> Optional[dict]:
    if metadata is None:
        return None
    if not isinstance(metadata, dict):
        raise HTTPException(status_code=400, detail="metadata 不是合法的 JSON 物件")
    fitted = fit_reasoning_into_metadata_budget(metadata)
    try:
        size = metadata_utf8_size(fitted)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=400, detail="metadata 不是合法的 JSON 物件"
        ) from exc
    if size > MAX_METADATA_BYTES:
        raise HTTPException(status_code=413, detail="metadata 過大")
    return fitted
