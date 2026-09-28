"""Action invoke limits are one counter across CSP workers."""

from fastapi import HTTPException

from app.services import message_action_service as mod


class _Counter:
    def __init__(self) -> None:
        self.values: dict[str, int] = {}
        self.ttl: dict[str, int] = {}

    def incr(self, key: str) -> int:
        self.values[key] = self.values.get(key, 0) + 1
        return self.values[key]

    def expire(self, key: str, seconds: int) -> bool:
        self.ttl[key] = seconds
        self.expire_calls = getattr(self, "expire_calls", 0) + 1
        return True


class _Down:
    def incr(self, key: str) -> int:
        raise ConnectionError("redis down")


def test_two_callers_share_one_counter(monkeypatch):
    monkeypatch.setattr(mod, "_invoke_limit", lambda db: 2)
    shared = _Counter()
    mod._check_rate_limit(None, 7, client=shared)
    mod._check_rate_limit(None, 7, client=shared)
    try:
        mod._check_rate_limit(None, 7, client=shared)
        raised = None
    except HTTPException as exc:
        raised = exc
    assert raised is not None and raised.status_code == 429
    mod._check_rate_limit(None, 8, client=shared)
    assert any(key.endswith(":7") or ":7:" in key for key in shared.values)
    assert all(seconds == 120 for seconds in shared.ttl.values())


def test_every_increment_sets_the_ttl(monkeypatch):
    monkeypatch.setattr(mod, "_invoke_limit", lambda db: 20)
    shared = _Counter()
    mod._check_rate_limit(None, 9, client=shared)
    mod._check_rate_limit(None, 9, client=shared)
    assert shared.expire_calls == 2
    assert list(shared.ttl.values()) == [120]


def test_redis_error_fails_closed(monkeypatch):
    monkeypatch.setattr(mod, "_invoke_limit", lambda db: 20)
    try:
        mod._check_rate_limit(None, 1, client=_Down())
        raised = None
    except HTTPException as exc:
        raised = exc
    assert raised is not None and raised.status_code == 503
