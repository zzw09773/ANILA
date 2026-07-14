"""Redis-backed durable queue for all anila-studio artifact jobs.

The queue is deliberately part of the request acceptance boundary: a create
endpoint may return 202 only after :meth:`JobStore.enqueue` succeeds.  Redis
errors therefore fail closed and make Studio unready; there is no in-memory or
best-effort fallback.

Claim, heartbeat, completion, retry and expired-lease recovery are Lua-fenced
so two Studio replicas cannot own the same job and a stale worker cannot
overwrite a newer attempt.  Only validated request data is persisted.  Browser
credentials are explicitly rejected before serialization.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from typing import Any

from redis.asyncio import Redis as AsyncRedis
from redis.asyncio import from_url as _from_url
from redis.exceptions import WatchError

from app.config import settings


logger = logging.getLogger(__name__)

_singleton: "JobStore | None" = None

_TERMINAL_STATES = frozenset({"done", "failed", "cancelled", "dead_letter"})
_CLAIMABLE_STATES = frozenset({"pending", "retry_wait"})
_FORBIDDEN_REQUEST_KEYS = frozenset(
    {
        "authorization",
        "bearer",
        "cookie",
        "password",
        "secret",
        "token",
        "access_token",
        "refresh_token",
        "api_key",
        "apikey",
    }
)
_INTEGRITY_FIELDS = (
    "schema_version",
    "job_id",
    "artifact_type",
    "owner_user_id",
    "collection_id",
    "requester",
    "task_id",
    "source_snapshot_id",
    "trace_id",
    "classification_level",
    "request_spec",
    "max_attempts",
    "created_at",
)
_INTEGRITY_FIELD = "_envelope_hmac_sha256"


def _envelope_signature(data: dict[str, Any]) -> str:
    key = settings.STUDIO_JOB_ENVELOPE_HMAC_KEY.encode("utf-8")
    canonical = json.dumps(
        {name: data.get(name) for name in _INTEGRITY_FIELDS},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hmac.new(key, canonical, hashlib.sha256).hexdigest()


class JobStoreError(RuntimeError):
    """Base class for durable queue failures."""


class JobStoreUnavailable(JobStoreError):
    """Redis is unavailable; callers must reject new work and readiness."""


class DuplicateJob(JobStoreError):
    """The caller attempted to enqueue an existing job id."""


class LeaseLost(JobStoreError):
    """A worker attempted to mutate a job it no longer owns."""


class UnsafeJobPayload(JobStoreError):
    """A durable request envelope contains credential-like data."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso_now() -> str:
    return _utcnow().isoformat()


def _assert_no_credentials(value: Any, *, path: str = "request_spec") -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            normalized = str(key).strip().lower().replace("-", "_")
            if normalized in _FORBIDDEN_REQUEST_KEYS or normalized.endswith("_token"):
                raise UnsafeJobPayload(f"{path}.{key} 不得持久化 credential")
            _assert_no_credentials(nested, path=f"{path}.{key}")
        return
    if isinstance(value, (list, tuple)):
        for index, nested in enumerate(value):
            _assert_no_credentials(nested, path=f"{path}[{index}]")
        return
    if isinstance(value, str) and value.lstrip().lower().startswith("bearer "):
        raise UnsafeJobPayload(f"{path} 不得持久化 bearer credential")


@dataclass(frozen=True)
class PersistedJob:
    """Versioned durable envelope shared by all five artifact pipelines."""

    job_id: str
    artifact_type: str
    owner_user_id: int
    state: str
    status_view: dict[str, Any]
    collection_id: int | None = None
    requester: str | None = None
    task_id: str | None = None
    source_snapshot_id: str | None = None
    trace_id: str | None = None
    classification_level: str | None = None
    request_spec: dict[str, Any] = field(default_factory=dict)
    stage: str | None = None
    checkpoint: dict[str, Any] = field(default_factory=dict)
    attempt_count: int = 0
    max_attempts: int = 3
    restart_count: int = 0
    lease_token: str | None = None
    lease_owner: str | None = None
    lease_expires_at_epoch: float | None = None
    heartbeat_at: str | None = None
    not_before_epoch: float = 0.0
    artifact_id: int | None = None
    download_url: str | None = None
    storage_ref: str | None = None
    content_hash: str | None = None
    result_metadata: dict[str, Any] = field(default_factory=dict)
    last_error: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.artifact_type not in {
            "slides",
            "report",
            "mindmap",
            "infographic",
            "datatable",
        }:
            raise ValueError(f"unsupported artifact_type: {self.artifact_type}")
        if self.state not in _CLAIMABLE_STATES | _TERMINAL_STATES | {"running"}:
            raise ValueError(f"unsupported durable job state: {self.state}")
        if self.owner_user_id < 1:
            raise ValueError("owner_user_id must be positive")
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        if self.attempt_count < 0 or self.restart_count < 0:
            raise ValueError("attempt counters cannot be negative")
        _assert_no_credentials(self.request_spec)

    def to_json(self) -> str:
        _assert_no_credentials(self.request_spec)
        payload = asdict(self)
        payload[_INTEGRITY_FIELD] = _envelope_signature(payload)
        return json.dumps(
            payload,
            ensure_ascii=False,
            default=str,
            separators=(",", ":"),
        )

    @classmethod
    def from_json(cls, raw: str | bytes) -> "PersistedJob":
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("persisted job payload is not a JSON object")
        supplied = data.pop(_INTEGRITY_FIELD, None)
        expected = _envelope_signature(data)
        if not isinstance(supplied, str) or not hmac.compare_digest(supplied, expected):
            raise UnsafeJobPayload("durable job envelope integrity check failed")
        known = {item.name for item in fields(cls)}
        return cls(**{key: value for key, value in data.items() if key in known})


_ENQUEUE_LUA = """
if redis.call('EXISTS', KEYS[1]) == 1 then return 0 end
redis.call('SET', KEYS[1], ARGV[1])
redis.call('SADD', KEYS[2], ARGV[3])
redis.call('ZADD', KEYS[3], ARGV[4], ARGV[3])
return 1
"""

_PUT_HISTORY_LUA = """
local raw = redis.call('GET', KEYS[1])
if raw then
  local current = cjson.decode(raw)
  if current['state'] == 'pending' or current['state'] == 'running' or current['state'] == 'retry_wait' then
    return -1
  end
end
redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[2])
return 1
"""

_CLAIM_LUA = """
local raw = redis.call('GET', KEYS[1])
if not raw then
  redis.call('ZREM', KEYS[2], ARGV[7])
  redis.call('ZREM', KEYS[3], ARGV[7])
  redis.call('SREM', KEYS[4], ARGV[7])
  return nil
end
local job = cjson.decode(raw)
if job['state'] ~= 'pending' and job['state'] ~= 'retry_wait' then
  redis.call('ZREM', KEYS[2], ARGV[7])
  return nil
end
local not_before = tonumber(job['not_before_epoch'] or 0)
if not_before > tonumber(ARGV[1]) then return nil end
job['state'] = 'running'
job['attempt_count'] = tonumber(job['attempt_count'] or 0) + 1
job['lease_token'] = ARGV[2]
job['lease_owner'] = ARGV[3]
job['lease_expires_at_epoch'] = tonumber(ARGV[4])
job['heartbeat_at'] = ARGV[5]
job['updated_at'] = ARGV[5]
local encoded = cjson.encode(job)
redis.call('SET', KEYS[1], encoded)
redis.call('ZREM', KEYS[2], ARGV[7])
redis.call('ZADD', KEYS[3], ARGV[4], ARGV[7])
return encoded
"""

_HEARTBEAT_LUA = """
local raw = redis.call('GET', KEYS[1])
if not raw then return -1 end
local job = cjson.decode(raw)
if job['state'] ~= 'running' or job['lease_token'] ~= ARGV[1] then return -1 end
if tonumber(job['lease_expires_at_epoch'] or 0) < tonumber(ARGV[2]) then return -2 end
job['lease_expires_at_epoch'] = tonumber(ARGV[3])
job['heartbeat_at'] = ARGV[4]
job['updated_at'] = ARGV[4]
redis.call('SET', KEYS[1], cjson.encode(job))
redis.call('ZADD', KEYS[2], ARGV[3], ARGV[6])
return 1
"""

_FENCED_REPLACE_LUA = """
local raw = redis.call('GET', KEYS[1])
if not raw then return -1 end
local current = cjson.decode(raw)
if current['state'] ~= 'running' or current['lease_token'] ~= ARGV[1] then return -1 end
if tonumber(current['lease_expires_at_epoch'] or 0) < tonumber(ARGV[2]) then return -2 end
local replacement = cjson.decode(ARGV[3])
if replacement['job_id'] ~= current['job_id'] then return -3 end
if replacement['lease_token'] ~= ARGV[1] then return -3 end
redis.call('SET', KEYS[1], ARGV[3])
return 1
"""

_COMPLETE_LUA = """
local raw = redis.call('GET', KEYS[1])
if not raw then return -1 end
local job = cjson.decode(raw)
if job['state'] ~= 'running' or job['lease_token'] ~= ARGV[1] then return -1 end
if tonumber(job['lease_expires_at_epoch'] or 0) < tonumber(ARGV[2]) then return -2 end
local replacement = cjson.decode(ARGV[3])
if replacement['state'] == 'done' then
  if replacement['artifact_id'] == cjson.null or replacement['artifact_id'] == nil then return -4 end
  if replacement['download_url'] == cjson.null or replacement['download_url'] == nil or replacement['download_url'] == '' then return -4 end
end
replacement['lease_token'] = cjson.null
replacement['lease_owner'] = cjson.null
replacement['lease_expires_at_epoch'] = cjson.null
redis.call('SET', KEYS[1], cjson.encode(replacement), 'EX', ARGV[4])
redis.call('ZREM', KEYS[2], ARGV[5])
redis.call('ZREM', KEYS[3], ARGV[5])
redis.call('SREM', KEYS[4], ARGV[5])
return 1
"""

_RETRY_LUA = """
local raw = redis.call('GET', KEYS[1])
if not raw then return -1 end
local job = cjson.decode(raw)
if job['state'] ~= 'running' or job['lease_token'] ~= ARGV[1] then return -1 end
if tonumber(job['lease_expires_at_epoch'] or 0) < tonumber(ARGV[2]) then return -2 end
job['last_error'] = ARGV[3]
job['updated_at'] = ARGV[4]
if job['status_view'] ~= nil and job['status_view'] ~= cjson.null then
  job['status_view']['error'] = ARGV[3]
end
job['lease_token'] = cjson.null
job['lease_owner'] = cjson.null
job['lease_expires_at_epoch'] = cjson.null
if tonumber(job['attempt_count'] or 0) >= tonumber(job['max_attempts'] or 1) then
  job['state'] = 'dead_letter'
  if job['status_view'] ~= nil and job['status_view'] ~= cjson.null then job['status_view']['state'] = 'failed' end
  redis.call('ZREM', KEYS[2], ARGV[5])
  redis.call('SREM', KEYS[4], ARGV[5])
else
  job['state'] = 'retry_wait'
  if job['status_view'] ~= nil and job['status_view'] ~= cjson.null then job['status_view']['state'] = 'pending' end
  job['not_before_epoch'] = tonumber(ARGV[6])
  redis.call('ZADD', KEYS[2], ARGV[6], ARGV[5])
end
redis.call('ZREM', KEYS[3], ARGV[5])
if job['state'] == 'dead_letter' then
  redis.call('SET', KEYS[1], cjson.encode(job), 'EX', ARGV[7])
else
  redis.call('SET', KEYS[1], cjson.encode(job))
end
return job['state']
"""

_DEFER_TERMINAL_LUA = """
local raw = redis.call('GET', KEYS[1])
if not raw then return -1 end
local job = cjson.decode(raw)
if job['state'] ~= 'running' or job['lease_token'] ~= ARGV[1] then return -1 end
job['state'] = 'retry_wait'
job['stage'] = 'terminal_report_pending'
job['last_error'] = ARGV[2]
job['updated_at'] = ARGV[3]
job['not_before_epoch'] = tonumber(ARGV[5])
job['lease_token'] = cjson.null
job['lease_owner'] = cjson.null
job['lease_expires_at_epoch'] = cjson.null
if job['status_view'] ~= nil and job['status_view'] ~= cjson.null then
  job['status_view']['state'] = 'pending'
  job['status_view']['error'] = ARGV[2]
end
redis.call('ZREM', KEYS[3], ARGV[4])
redis.call('ZADD', KEYS[2], ARGV[5], ARGV[4])
redis.call('SADD', KEYS[4], ARGV[4])
redis.call('SET', KEYS[1], cjson.encode(job))
return job['state']
"""

_REAP_LUA = """
local raw = redis.call('GET', KEYS[1])
if not raw then
  redis.call('ZREM', KEYS[3], ARGV[1])
  redis.call('SREM', KEYS[4], ARGV[1])
  return 0
end
local job = cjson.decode(raw)
if job['state'] ~= 'running' or tonumber(job['lease_expires_at_epoch'] or 0) > tonumber(ARGV[2]) then
  redis.call('ZREM', KEYS[3], ARGV[1])
  return 0
end
job['restart_count'] = tonumber(job['restart_count'] or 0) + 1
job['last_error'] = 'lease_expired'
job['updated_at'] = ARGV[3]
if job['status_view'] ~= nil and job['status_view'] ~= cjson.null then
  job['status_view']['error'] = 'lease_expired'
end
job['lease_token'] = cjson.null
job['lease_owner'] = cjson.null
job['lease_expires_at_epoch'] = cjson.null
if tonumber(job['attempt_count'] or 0) >= tonumber(job['max_attempts'] or 1) then
  job['state'] = 'dead_letter'
  if job['status_view'] ~= nil and job['status_view'] ~= cjson.null then job['status_view']['state'] = 'failed' end
  redis.call('SREM', KEYS[4], ARGV[1])
else
  job['state'] = 'retry_wait'
  if job['status_view'] ~= nil and job['status_view'] ~= cjson.null then job['status_view']['state'] = 'pending' end
  job['not_before_epoch'] = tonumber(ARGV[2])
  redis.call('ZADD', KEYS[2], ARGV[2], ARGV[1])
end
redis.call('ZREM', KEYS[3], ARGV[1])
if job['state'] == 'dead_letter' then
  redis.call('SET', KEYS[1], cjson.encode(job), 'EX', ARGV[4])
else
  redis.call('SET', KEYS[1], cjson.encode(job))
end
return job['state']
"""


class JobStore:
    """Strict Redis durable queue with lease fencing."""

    def __init__(self) -> None:
        self._redis: AsyncRedis | None = None
        self._ready = False
        self._last_error: str | None = None

    @property
    def ready(self) -> bool:
        return self._ready and self._redis is not None

    @property
    def last_error(self) -> str | None:
        return self._last_error

    def _job_key(self, job_id: str) -> str:
        return f"{settings.JOB_STORE_KEY_PREFIX}{job_id}"

    @property
    def _index_prefix(self) -> str:
        return settings.JOB_STORE_KEY_PREFIX.rstrip(":")

    @property
    def _all_key(self) -> str:
        return f"{self._index_prefix}:all"

    @property
    def _ready_key(self) -> str:
        return f"{self._index_prefix}:ready"

    @property
    def _leases_key(self) -> str:
        return f"{self._index_prefix}:leases"

    def _require_redis(self) -> AsyncRedis:
        if not self.ready or self._redis is None:
            raise JobStoreUnavailable(self._last_error or "durable job store not ready")
        return self._redis

    def _failed(self, exc: BaseException) -> JobStoreUnavailable:
        self._ready = False
        self._last_error = f"{type(exc).__name__}: {exc}"
        logger.error("durable job store unavailable: %s", self._last_error)
        return JobStoreUnavailable(self._last_error)

    async def start(self, app: Any = None) -> None:
        del app
        await self._connect()
        if self.ready:
            await self.reconcile_indexes()

    async def _connect(self) -> bool:
        redis = _from_url(settings.REDIS_URL, decode_responses=True)
        try:
            await redis.ping()
        except Exception as exc:  # noqa: BLE001 - translated to fail-closed error
            try:
                await redis.aclose()
            finally:
                self._redis = None
            self._failed(exc)
            return False
        old, self._redis = self._redis, redis
        if old is not None and old is not redis:
            try:
                await old.aclose()
            except Exception:  # noqa: BLE001
                logger.debug("closing replaced Redis client failed", exc_info=True)
        self._ready = True
        self._last_error = None
        return True

    async def probe(self) -> bool:
        if self._redis is None:
            if not await self._connect():
                return False
            try:
                await self.reconcile_indexes()
            except JobStoreError:
                return False
            return True
        try:
            await self._redis.ping()
        except Exception as exc:  # noqa: BLE001
            self._failed(exc)
            return False
        self._ready = True
        self._last_error = None
        return True

    async def stop(self, app: Any = None) -> None:
        del app
        redis, self._redis = self._redis, None
        self._ready = False
        if redis is not None:
            try:
                await redis.aclose()
            except Exception:  # noqa: BLE001
                logger.debug("durable job store close failed", exc_info=True)

    async def _call(self, operation: Any) -> Any:
        try:
            return await operation
        except JobStoreError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise self._failed(exc) from exc

    async def enqueue(self, job: PersistedJob, *, now_epoch: float | None = None) -> None:
        if job.state != "pending":
            raise ValueError("new durable job must be pending")
        redis = self._require_redis()
        now_epoch = _utcnow().timestamp() if now_epoch is None else now_epoch
        created = await self._call(
            redis.eval(
                _ENQUEUE_LUA,
                3,
                self._job_key(job.job_id),
                self._all_key,
                self._ready_key,
                job.to_json(),
                settings.JOB_STORE_TTL_SECONDS,
                job.job_id,
                now_epoch,
            )
        )
        if int(created) != 1:
            raise DuplicateJob(job.job_id)

    async def put(self, job: PersistedJob) -> None:
        """Write terminal history only; active jobs require lease fencing."""
        if job.state not in _TERMINAL_STATES:
            raise LeaseLost(f"unfenced active write rejected for {job.job_id}")
        redis = self._require_redis()
        key = self._job_key(job.job_id)
        # WATCH is used here (rather than the queue Lua scripts) because this
        # compatibility method is also exercised by fakeredis.  It remains an
        # atomic guard: an active envelope can never be overwritten by a stale
        # unfenced projection.
        while True:
            async with redis.pipeline(transaction=True) as pipe:
                try:
                    await pipe.watch(key)
                    raw = await pipe.get(key)
                    if raw is not None:
                        current = PersistedJob.from_json(raw)
                        if current.state not in _TERMINAL_STATES:
                            await pipe.unwatch()
                            raise LeaseLost(
                                f"active durable job {job.job_id} requires lease"
                            )
                    pipe.multi()
                    pipe.set(
                        key,
                        job.to_json(),
                        ex=settings.JOB_STORE_TTL_SECONDS,
                    )
                    await pipe.execute()
                    return
                except WatchError:
                    continue

    async def get(self, job_id: str) -> PersistedJob | None:
        redis = self._require_redis()
        raw = await self._call(redis.get(self._job_key(job_id)))
        if raw is None:
            return None
        try:
            return PersistedJob.from_json(raw)
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            raise JobStoreError(f"corrupt durable job {job_id}: {exc}") from exc

    async def claim(
        self,
        *,
        worker_id: str,
        lease_seconds: float,
        now_epoch: float | None = None,
    ) -> PersistedJob | None:
        redis = self._require_redis()
        now_epoch = _utcnow().timestamp() if now_epoch is None else now_epoch
        while True:
            candidates = await self._call(
                redis.zrangebyscore(
                    self._ready_key, "-inf", now_epoch, start=0, num=128
                )
            )
            if not candidates:
                return None
            for job_id in candidates:
                token = secrets.token_urlsafe(32)
                heartbeat = _iso_now()
                expires = now_epoch + lease_seconds
                raw = await self._call(
                    redis.eval(
                        _CLAIM_LUA,
                        4,
                        self._job_key(job_id),
                        self._ready_key,
                        self._leases_key,
                        self._all_key,
                        now_epoch,
                        token,
                        worker_id,
                        expires,
                        heartbeat,
                        settings.JOB_STORE_TTL_SECONDS,
                        job_id,
                    )
                )
                if raw:
                    return PersistedJob.from_json(raw)
            # Lua removes stale candidates. Re-read so >128 stale members
            # cannot permanently starve valid work behind them.

    async def claim_job(
        self,
        job_id: str,
        *,
        worker_id: str,
        lease_seconds: float,
        now_epoch: float | None = None,
    ) -> PersistedJob | None:
        """Atomically claim one known job (used by the accepting process)."""
        redis = self._require_redis()
        now_epoch = _utcnow().timestamp() if now_epoch is None else now_epoch
        token = secrets.token_urlsafe(32)
        heartbeat = _iso_now()
        expires = now_epoch + lease_seconds
        raw = await self._call(
            redis.eval(
                _CLAIM_LUA,
                4,
                self._job_key(job_id),
                self._ready_key,
                self._leases_key,
                self._all_key,
                now_epoch,
                token,
                worker_id,
                expires,
                heartbeat,
                settings.JOB_STORE_TTL_SECONDS,
                job_id,
            )
        )
        return PersistedJob.from_json(raw) if raw else None

    async def heartbeat(
        self,
        job_id: str,
        lease_token: str,
        *,
        lease_seconds: float,
        now_epoch: float | None = None,
    ) -> None:
        redis = self._require_redis()
        now_epoch = _utcnow().timestamp() if now_epoch is None else now_epoch
        result = await self._call(
            redis.eval(
                _HEARTBEAT_LUA,
                2,
                self._job_key(job_id),
                self._leases_key,
                lease_token,
                now_epoch,
                now_epoch + lease_seconds,
                _iso_now(),
                settings.JOB_STORE_TTL_SECONDS,
                job_id,
            )
        )
        if int(result) != 1:
            raise LeaseLost(job_id)

    async def update_claimed(
        self,
        job: PersistedJob,
        lease_token: str,
        *,
        now_epoch: float | None = None,
    ) -> None:
        redis = self._require_redis()
        now_epoch = _utcnow().timestamp() if now_epoch is None else now_epoch
        if job.lease_token != lease_token or job.state != "running":
            raise LeaseLost(job.job_id)
        result = await self._call(
            redis.eval(
                _FENCED_REPLACE_LUA,
                1,
                self._job_key(job.job_id),
                lease_token,
                now_epoch,
                job.to_json(),
                settings.JOB_STORE_TTL_SECONDS,
            )
        )
        if int(result) != 1:
            raise LeaseLost(job.job_id)

    async def complete(
        self,
        job: PersistedJob,
        lease_token: str,
        *,
        now_epoch: float | None = None,
    ) -> None:
        if job.state not in _TERMINAL_STATES:
            raise ValueError("complete requires a terminal job state")
        if job.state == "done" and (job.artifact_id is None or not job.download_url):
            raise JobStoreError("done requires CSP artifact_id and download_url")
        redis = self._require_redis()
        now_epoch = _utcnow().timestamp() if now_epoch is None else now_epoch
        result = await self._call(
            redis.eval(
                _COMPLETE_LUA,
                4,
                self._job_key(job.job_id),
                self._leases_key,
                self._ready_key,
                self._all_key,
                lease_token,
                now_epoch,
                job.to_json(),
                settings.JOB_STORE_TTL_SECONDS,
                job.job_id,
            )
        )
        if int(result) == -4:
            raise JobStoreError("done requires CSP artifact_id and download_url")
        if int(result) != 1:
            raise LeaseLost(job.job_id)

    async def retry_or_dead_letter(
        self,
        job_id: str,
        lease_token: str,
        *,
        error: str,
        retry_delay_seconds: float,
        now_epoch: float | None = None,
    ) -> str:
        redis = self._require_redis()
        now_epoch = _utcnow().timestamp() if now_epoch is None else now_epoch
        result = await self._call(
            redis.eval(
                _RETRY_LUA,
                4,
                self._job_key(job_id),
                self._ready_key,
                self._leases_key,
                self._all_key,
                lease_token,
                now_epoch,
                error[:1000],
                _iso_now(),
                job_id,
                now_epoch + retry_delay_seconds,
                settings.JOB_STORE_TTL_SECONDS,
            )
        )
        if result in {"retry_wait", "dead_letter"}:
            return str(result)
        raise LeaseLost(job_id)

    async def defer_terminal_report(
        self,
        job_id: str,
        lease_token: str,
        *,
        error: str,
        retry_delay_seconds: float,
        now_epoch: float | None = None,
    ) -> None:
        """Release a lease while retaining claimable terminal-report work."""
        redis = self._require_redis()
        now_epoch = _utcnow().timestamp() if now_epoch is None else now_epoch
        result = await self._call(
            redis.eval(
                _DEFER_TERMINAL_LUA,
                4,
                self._job_key(job_id),
                self._ready_key,
                self._leases_key,
                self._all_key,
                lease_token,
                error[:1000],
                _iso_now(),
                job_id,
                now_epoch + retry_delay_seconds,
            )
        )
        if result != "retry_wait":
            raise LeaseLost(job_id)

    async def reap_expired(self, *, now_epoch: float | None = None) -> list[str]:
        redis = self._require_redis()
        now_epoch = _utcnow().timestamp() if now_epoch is None else now_epoch
        expired = await self._call(
            redis.zrangebyscore(self._leases_key, "-inf", now_epoch)
        )
        recovered: list[str] = []
        for job_id in expired:
            result = await self._call(
                redis.eval(
                    _REAP_LUA,
                    4,
                    self._job_key(job_id),
                    self._ready_key,
                    self._leases_key,
                    self._all_key,
                    job_id,
                    now_epoch,
                    _iso_now(),
                    settings.JOB_STORE_TTL_SECONDS,
                )
            )
            if result in {"retry_wait", "dead_letter"}:
                recovered.append(job_id)
        return recovered

    async def reconcile_indexes(self) -> dict[str, int]:
        """Rebuild active indexes from durable envelopes after a restart."""
        redis = self._require_redis()
        prefix = settings.JOB_STORE_KEY_PREFIX
        reserved = {self._all_key, self._ready_key, self._leases_key}
        active: list[PersistedJob] = []
        async for key in redis.scan_iter(match=f"{prefix}*"):
            key_text = key.decode() if isinstance(key, bytes) else str(key)
            if key_text in reserved:
                continue
            raw = await self._call(redis.get(key_text))
            if raw is None:
                continue
            try:
                job = PersistedJob.from_json(raw)
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                raise JobStoreError(f"corrupt durable envelope {key_text}: {exc}") from exc
            if job.state not in _TERMINAL_STATES:
                active.append(job)

        pipe = redis.pipeline(transaction=True)
        pipe.delete(self._all_key, self._ready_key, self._leases_key)
        for job in active:
            pipe.sadd(self._all_key, job.job_id)
            pipe.persist(self._job_key(job.job_id))
            if job.state in _CLAIMABLE_STATES:
                pipe.zadd(self._ready_key, {job.job_id: job.not_before_epoch})
            elif job.state == "running" and job.lease_expires_at_epoch is not None:
                pipe.zadd(
                    self._leases_key,
                    {job.job_id: job.lease_expires_at_epoch},
                )
        await self._call(pipe.execute())
        return {
            "active": len(active),
            "claimable": sum(job.state in _CLAIMABLE_STATES for job in active),
            "leased": sum(job.state == "running" for job in active),
        }

    async def put_quietly(self, job: PersistedJob) -> None:
        """Compatibility alias; intentionally strict (no swallowed writes)."""
        await self.put(job)


def get_job_store() -> JobStore:
    global _singleton
    if _singleton is None:
        _singleton = JobStore()
    return _singleton
