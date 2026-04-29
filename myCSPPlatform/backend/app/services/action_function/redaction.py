"""Audit redaction — defense in depth, NOT primary protection.

Spec §7.4 reframes this as a tertiary defense behind:

  1. egress proxy + allowlist (worker network can't exfiltrate)
  2. minimal valve injection (subprocess only sees declared fields)
  3. ``code`` visibility ACL (non-author developers can't read code to
     learn the secret pattern)

Substring redaction can't catch base64-encoded, sliced, hashed, or
header-exfil leaks. It only catches the simplest cases — typo-level
``print(self.valves.token)`` style accidents. Don't market this as a
real safeguard.

Secrets shorter than 8 characters are not redacted because the false-
positive rate explodes (common short strings would get masked across
unrelated event payloads). Real secrets ought to be ≥ 16 chars anyway.
"""

from __future__ import annotations

from typing import Any


MIN_SECRET_LEN = 8


def _redact_string(s: str, secrets: dict[str, str]) -> str:
    out = s
    for field, value in secrets.items():
        if not isinstance(value, str) or len(value) < MIN_SECRET_LEN:
            continue
        out = out.replace(value, f"<redacted:valves.{field}>")
    return out


def _redact_value(v: Any, secrets: dict[str, str]) -> Any:
    if isinstance(v, str):
        return _redact_string(v, secrets)
    if isinstance(v, dict):
        return {k: _redact_value(vv, secrets) for k, vv in v.items()}
    if isinstance(v, list):
        return [_redact_value(item, secrets) for item in v]
    return v


def redact_events(events: list[dict], secrets: dict[str, str]) -> list[dict]:
    """Best-effort redaction of an SSE event list.

    Returns a new list with all detected secret substrings replaced by
    ``<redacted:valves.<field>>``. Input list is not mutated.
    """
    if not secrets:
        return events
    return [_redact_value(e, secrets) for e in events]


def redact_payload(payload: dict, secrets: dict[str, str]) -> dict:
    """Same as :func:`redact_events` but for a single dict payload."""
    if not secrets:
        return payload
    return _redact_value(payload, secrets)


def collect_secret_values(values: dict, schema: dict) -> dict[str, str]:
    """Pick out the plaintext values of fields tagged secret in the
    Valves schema.

    Detection accepts either ``json_schema_extra.secret == True`` (the
    canonical pydantic way) or a top-level ``x-secret`` flag (legacy /
    hand-written schemas). Non-string values are skipped because they
    can't be substring-matched anyway.
    """
    out: dict[str, str] = {}
    for field, meta in (schema.get("properties") or {}).items():
        is_secret = (
            (meta.get("json_schema_extra") or {}).get("secret")
            or meta.get("x-secret")
        )
        if not is_secret:
            continue
        v = values.get(field)
        if isinstance(v, str) and v:
            out[field] = v
    return out
