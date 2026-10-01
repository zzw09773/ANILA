"""額度計數的 Redis。連不上就回 None，呼叫端放行。

pytest 不連真的 Redis。測試用 ``set_quota_redis`` 換上一份記憶體實作。

累加與缺鍵重建都在同一支 script 裡做完，避免先讀再 SET 蓋掉別人剛加上的值。
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

_UNSET = object()
_override = _UNSET
_client = None

# 鍵不存在就不要 INCRBY，否則會寫進一個缺了前段的計數，重建就不會發生。
INCR_IF_EXISTS = """
local current = redis.call('GET', KEYS[1])
if current == false then
  return nil
end
return redis.call('INCRBY', KEYS[1], ARGV[1])
"""

# 現有數字才累加。缺鍵或失效標記回 nil，讓呼叫端改走重建。
INCR_IF_PRESENT = """
-- incr-if-present
local current = redis.call('GET', KEYS[1])
if current == false or current == 'invalid' then
  return nil
end
local nextv = redis.call('INCRBY', KEYS[1], ARGV[1])
if ARGV[2] ~= nil and ARGV[2] ~= '' then
  redis.call('EXPIRE', KEYS[1], tonumber(ARGV[2]))
end
return nextv
"""

# 缺鍵時寫入「已含本批」的絕對值，不再把 delta 加一次。
# 若同時已經有人寫進更高的值，保留那個值，不加、也不覆蓋。
APPLY_MISSING = """
-- apply-missing
local current = redis.call('GET', KEYS[1])
local absolute = tonumber(ARGV[1])
local delta = tonumber(ARGV[2])
local ttl = tonumber(ARGV[3])
if current == false or current == 'invalid' then
  redis.call('SET', KEYS[1], absolute, 'EX', ttl)
  return {absolute, delta}
end
local cur = tonumber(current)
if cur == nil then
  redis.call('SET', KEYS[1], absolute, 'EX', ttl)
  return {absolute, delta}
end
if cur >= absolute then
  redis.call('EXPIRE', KEYS[1], ttl)
  return {cur, 0}
else
  local nextv = redis.call('INCRBY', KEYS[1], delta)
  redis.call('EXPIRE', KEYS[1], ttl)
  return {nextv, delta}
end
"""

# 讀取路徑只在缺鍵或失效時寫入絕對值，不覆蓋活著的數字。
INIT_IF_ABSENT = """
-- init-if-absent
local current = redis.call('GET', KEYS[1])
local absolute = tonumber(ARGV[1])
local ttl = tonumber(ARGV[2])
if current == false or current == 'invalid' then
  redis.call('SET', KEYS[1], absolute, 'EX', ttl)
  return absolute
end
local cur = tonumber(current)
if cur == nil then
  redis.call('SET', KEYS[1], absolute, 'EX', ttl)
  return absolute
end
return cur
"""


class QuotaRedisDown(Exception):
    pass


def _script_name(script) -> str:
    text = script if isinstance(script, str) else ""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("-- "):
            return stripped[3:].strip()
    return ""


class MemoryQuotaRedis:
    """測試用。script 行為跟上面三支 Lua 對齊。"""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    def get(self, key: str):
        return self.store.get(key)

    def set(self, key: str, value, ex: int | None = None):
        if value == "invalid":
            self.store[str(key)] = "invalid"
            return True
        self.store[str(key)] = str(int(value))
        return True

    def delete(self, key: str):
        self.store.pop(key, None)

    def eval(self, script, numkeys, key, *args):
        name = _script_name(script)
        if name == "init-if-absent":
            absolute = int(args[0])
            current = self.store.get(key)
            if current is None or current == "invalid":
                self.store[key] = str(absolute)
                return absolute
            try:
                return int(current)
            except (TypeError, ValueError):
                self.store[key] = str(absolute)
                return absolute
        if name == "apply-missing":
            absolute = int(args[0])
            delta = int(args[1])
            current = self.store.get(key)
            if current is None or current == "invalid":
                self.store[key] = str(absolute)
                return [absolute, delta]
            try:
                cur = int(current)
            except (TypeError, ValueError):
                self.store[key] = str(absolute)
                return [absolute, delta]
            if cur >= absolute:
                return [cur, 0]
            nextv = cur + delta
            self.store[key] = str(nextv)
            return [nextv, delta]
        if name == "incr-if-present":
            current = self.store.get(key)
            if current is None or current == "invalid":
                return None
            try:
                cur = int(current)
            except (TypeError, ValueError):
                return None
            nextv = cur + int(args[0])
            self.store[key] = str(nextv)
            return nextv
        if key not in self.store or self.store[key] == "invalid":
            return None
        current = int(self.store[key]) + int(args[0])
        self.store[key] = str(current)
        return current


class DownQuotaRedis:
    def get(self, key):
        raise RuntimeError("redis down")

    def set(self, key, value, ex=None):
        raise RuntimeError("redis down")

    def eval(self, script, numkeys, key, *args):
        raise RuntimeError("redis down")


def set_quota_redis(client) -> None:
    global _override
    _override = client


def reset_quota_redis() -> None:
    global _override
    _override = _UNSET


def get_quota_redis():
    if _override is not _UNSET:
        return _override
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return None
    url = os.environ.get("REDIS_URL")
    if not url:
        return None
    global _client
    if _client is not None:
        return _client
    try:
        import redis

        _client = redis.Redis.from_url(
            url,
            decode_responses=True,
            socket_timeout=0.3,
            socket_connect_timeout=0.3,
        )
        return _client
    except Exception:
        logger.warning("額度計數連不上 Redis，本次不擋", exc_info=True)
        return None
