"""Durable job store for the anila-studio artifact pipelines (Slice 8b).

Background
==========

The five artifact pipelines (slides / report / mindmap / infographic /
datatable) each kept their job state in a process-local ``_jobs`` dict.
That works for a single uvicorn process that never restarts, but doc 02's
failure model is explicit: *"Studio restart → job 不應丟失；重構後由
persisted job store 恢復"*. A restart wiped every in-flight and recently
completed job, and a status poll for a pre-restart job 404'd.

This module is the durable backend. Per doc 02 §1 the target topology
lists **Redis** as ``queue + revocation + jobs`` — so we reuse the SAME
Redis instance the revocation cache already connects to (``REDIS_URL``),
keyed under ``anila-studio:jobs:{job_id}`` with a generous TTL (default
7 days). Values are JSON. The in-memory ``_jobs`` dict in each pipeline
becomes a read-through cache; this store is the source of truth that
survives a restart.

Design invariants
=================

* **Best-effort, never fatal.** ``put_quietly`` swallows + logs any Redis
  error — a store hiccup must not break generation, exactly like the CSP
  reporter. ``put`` / ``get`` are the strict variants tests assert on.
* **No-op until started.** ``get_job_store()`` returns a singleton whose
  ``_redis`` is ``None`` until ``start()`` runs, so code paths that never
  boot the store (unit tests of a single pipeline) see graceful no-ops.
* **Does NOT import anila_core.** Studio is an independent service.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field, fields
from typing import Any

from redis.asyncio import Redis as AsyncRedis
from redis.asyncio import from_url as _from_url

from app.config import settings


logger = logging.getLogger(__name__)


# Module-level singleton (lazy). Mirrors ``revocation_cache.get_revocation_cache``
# so the lifespan callbacks and the request graph share one instance.
_singleton: "JobStore | None" = None


@dataclass(frozen=True)
class PersistedJob:
    """The unified ``ArtifactJob`` projection persisted per job (doc 02).

    ``status_view`` is the verbatim JSON of the owning pipeline's
    ``to_status()`` model. Persisting it means a restarted studio can
    answer a status query by returning ``status_view`` directly — no
    per-artifact-type reconstruction, no shape drift between the live
    record and the persisted one.

    The remaining fields carry the cross-cutting inheritance the CSP
    control plane needs (task_id / source_snapshot_id / trace_id /
    classification_level / artifact_id) plus enough routing metadata
    (artifact_type / owner_user_id / storage_ref) to rebuild an
    ArtifactJob row.
    """

    job_id: str
    artifact_type: str  # slides | report | mindmap | infographic | datatable
    owner_user_id: int
    state: str
    status_view: dict[str, Any]  # verbatim to_status() JSON
    collection_id: int | None = None
    requester: str | None = None  # employee id (card) or user id, stringified
    task_id: str | None = None
    source_snapshot_id: str | None = None
    trace_id: str | None = None
    classification_level: str | None = None
    artifact_id: str | None = None
    storage_ref: str | None = None
    content_hash: str | None = None
    result_metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str | None = None
    updated_at: str | None = None

    def to_json(self) -> str:
        # ``default=str`` is a belt-and-braces guard for any stray
        # datetime that slipped into result_metadata / status_view.
        return json.dumps(asdict(self), ensure_ascii=False, default=str)

    @classmethod
    def from_json(cls, raw: str | bytes) -> "PersistedJob":
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("persisted job payload is not a JSON object")
        # Forward-compat: ignore unknown keys a newer studio may have added.
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})


class JobStore:
    """Redis-backed durable job store.

    Lifecycle mirrors ``RevocationCache``: ``await store.start(app)`` on
    lifespan, ``await store.stop(app)`` on shutdown. Unlike the revocation
    cache, a store outage is NOT fail-closed — jobs degrade to in-memory
    only (the pipeline still runs; only restart-survival is lost), so
    ``start`` logs but does not raise on a bad ping.
    """

    def __init__(self) -> None:
        self._redis: AsyncRedis | None = None

    @property
    def ready(self) -> bool:
        return self._redis is not None

    async def start(self, app: Any = None) -> None:
        # ``decode_responses=True`` so ``get`` returns ``str`` (JSON text).
        # Delegated to the module-level ``_from_url`` alias so tests can
        # patch it the same way the revocation cache tests do.
        self._redis = _from_url(settings.REDIS_URL, decode_responses=True)
        try:
            await self._redis.ping()
        except Exception as exc:  # noqa: BLE001 — store is best-effort
            logger.warning("job store: Redis ping failed at start: %s", exc)

    async def stop(self, app: Any = None) -> None:
        if self._redis is not None:
            try:
                await self._redis.aclose()
            except Exception:  # noqa: BLE001
                logger.debug("job store: redis aclose failed", exc_info=True)
            self._redis = None

    def _key(self, job_id: str) -> str:
        return f"{settings.JOB_STORE_KEY_PREFIX}{job_id}"

    async def put(self, job: PersistedJob) -> None:
        """Strict write — raises on Redis error. Used by tests."""
        if self._redis is None:
            return
        await self._redis.set(
            self._key(job.job_id),
            job.to_json(),
            ex=settings.JOB_STORE_TTL_SECONDS,
        )

    async def get(self, job_id: str) -> PersistedJob | None:
        """Strict read — returns None on miss, raises on Redis error."""
        if self._redis is None:
            return None
        raw = await self._redis.get(self._key(job_id))
        if raw is None:
            return None
        try:
            return PersistedJob.from_json(raw)
        except (ValueError, TypeError):
            logger.warning("job store: corrupt record for %s, ignoring", job_id)
            return None

    async def put_quietly(self, job: PersistedJob) -> None:
        """Best-effort write — swallow + log any error.

        This is the variant the pipelines call on every state transition:
        a Redis blip must never surface as a failed job.
        """
        try:
            await self.put(job)
        except Exception as exc:  # noqa: BLE001
            logger.warning("job store: put failed for %s: %s", job.job_id, exc)


def get_job_store() -> JobStore:
    """Module-level singleton accessor.

    The lifespan callbacks (``start`` / ``stop``) and the per-request
    read-through path share the same instance through this factory.
    """
    global _singleton
    if _singleton is None:
        _singleton = JobStore()
    return _singleton
