"""Rate-limit and quota enforcement service.

Two layers of enforcement:
  1. In-memory sliding-window counters for per-minute / per-hour request limits.
     These are fast (no DB) but reset on restart — acceptable for MVP.
  2. Database-backed token quota checks against `token_usage` for daily / monthly caps.

Resolution order for policy lookup:
  api_key.quota_policy  →  api_key.user.quota_policy  →  None (unlimited)
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.api_key import ApiKey
from app.models.quota_policy import QuotaPolicy
from app.models.token_usage import TokenUsage


# ── In-memory sliding windows ─────────────────────────────────────────────────

@dataclass
class _SlidingWindow:
    """Deque of timestamps for a fixed-width window."""
    window_seconds: int
    _ts: deque = field(default_factory=deque)

    def record(self) -> None:
        now = time.monotonic()
        self._ts.append(now)
        cutoff = now - self.window_seconds
        while self._ts and self._ts[0] < cutoff:
            self._ts.popleft()

    def count(self) -> int:
        now = time.monotonic()
        cutoff = now - self.window_seconds
        while self._ts and self._ts[0] < cutoff:
            self._ts.popleft()
        return len(self._ts)


# keyed by api_key.id
_per_minute_windows: dict[int, _SlidingWindow] = defaultdict(lambda: _SlidingWindow(60))
_per_hour_windows:   dict[int, _SlidingWindow] = defaultdict(lambda: _SlidingWindow(3600))


# ── Policy resolution ─────────────────────────────────────────────────────────

def _effective_policy(api_key: ApiKey) -> Optional[QuotaPolicy]:
    """Return the most-specific QuotaPolicy for this key, or None."""
    if api_key.quota_policy_id and api_key.quota_policy:
        return api_key.quota_policy
    user = api_key.user
    if user and user.quota_policy_id and user.quota_policy:
        return user.quota_policy
    return None


# ── Public API ────────────────────────────────────────────────────────────────

@dataclass
class RateLimitResult:
    allowed: bool
    reason: str = ""


def check_and_record(db: Session, api_key: ApiKey) -> RateLimitResult:
    """Check all limits for this request and record it if allowed.

    Call this once per incoming proxy request BEFORE forwarding to upstream.
    """
    policy = _effective_policy(api_key)
    if policy is None:
        _record_request(api_key.id)
        return RateLimitResult(allowed=True)

    key_id = api_key.id

    # ── per-minute check ──────────────────────────────────────────────────────
    if policy.request_limit_per_minute is not None:
        current = _per_minute_windows[key_id].count()
        if current >= policy.request_limit_per_minute:
            return RateLimitResult(
                allowed=False,
                reason=f"每分鐘請求數超限 ({policy.request_limit_per_minute} req/min)",
            )

    # ── per-hour check ────────────────────────────────────────────────────────
    if policy.request_limit_per_hour is not None:
        current = _per_hour_windows[key_id].count()
        if current >= policy.request_limit_per_hour:
            return RateLimitResult(
                allowed=False,
                reason=f"每小時請求數超限 ({policy.request_limit_per_hour} req/hr)",
            )

    # ── daily token check ─────────────────────────────────────────────────────
    if policy.token_limit_per_day is not None:
        used = _tokens_in_window(db, api_key.id, hours=24)
        if used >= policy.token_limit_per_day:
            return RateLimitResult(
                allowed=False,
                reason=f"每日 Token 配額超限 ({policy.token_limit_per_day:,} tokens/day)",
            )

    # ── monthly token check ───────────────────────────────────────────────────
    if policy.token_limit_per_month is not None:
        used = _tokens_this_calendar_month(db, api_key.id)
        if used >= policy.token_limit_per_month:
            return RateLimitResult(
                allowed=False,
                reason=f"每月 Token 配額超限 ({policy.token_limit_per_month:,} tokens/month)",
            )

    _record_request(key_id)
    return RateLimitResult(allowed=True)


def get_usage_summary(db: Session, api_key: ApiKey) -> dict:
    """Return current usage counters for the API key (for status endpoints)."""
    policy = _effective_policy(api_key)
    key_id = api_key.id
    return {
        "policy_name": policy.name if policy else None,
        "requests_last_minute": _per_minute_windows[key_id].count(),
        "requests_last_hour": _per_hour_windows[key_id].count(),
        "tokens_last_24h": _tokens_in_window(db, key_id, hours=24),
        "tokens_this_month": _tokens_this_calendar_month(db, key_id),
        "limits": {
            "request_limit_per_minute": policy.request_limit_per_minute if policy else None,
            "request_limit_per_hour": policy.request_limit_per_hour if policy else None,
            "token_limit_per_day": policy.token_limit_per_day if policy else None,
            "token_limit_per_month": policy.token_limit_per_month if policy else None,
        },
    }


# ── Internal helpers ──────────────────────────────────────────────────────────

def _record_request(key_id: int) -> None:
    _per_minute_windows[key_id].record()
    _per_hour_windows[key_id].record()


def _tokens_in_window(db: Session, api_key_id: int, *, hours: int) -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    result = (
        db.query(func.coalesce(func.sum(TokenUsage.total_tokens), 0))
        .filter(
            TokenUsage.api_key_id == api_key_id,
            TokenUsage.request_timestamp >= cutoff,
        )
        .scalar()
    )
    return int(result or 0)


def _tokens_this_calendar_month(db: Session, api_key_id: int) -> int:
    now = datetime.now(timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    result = (
        db.query(func.coalesce(func.sum(TokenUsage.total_tokens), 0))
        .filter(
            TokenUsage.api_key_id == api_key_id,
            TokenUsage.request_timestamp >= month_start,
        )
        .scalar()
    )
    return int(result or 0)
