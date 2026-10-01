"""Admin-managed trusted-host allow-list service.

Sits between [`/api/trusted-hosts`](services/csp/app/api/trusted_hosts.py)
and the anila-core SSRF guard. Three jobs:

1. **CRUD** with audit logging (every add / remove writes an
   ``audit_log`` row keyed to the acting owner).
2. **In-memory cache** so the SSRF guard's tight loop doesn't hit the
   DB on every URL validation. TTL refresh + immediate invalidation
   after any mutation in this process.
3. **anila-core provider hook** — ``register_with_url_guard()`` plugs
   the cache into ``anila_core.security.register_trusted_host_provider``
   so the guard sees DB hosts on top of the ``ANILA_TRUSTED_HOSTS``
   env fallback.

開機不再把環境清單寫進這張表。compose 內部名稱（例如 docling、router）
只經環境層信任。撤銷一筆仍走管理介面的 DELETE（``remove_host``）。

The cache is process-local. Other CSP workers / replicas refresh on
their own TTL tick (default 30s) — the eventual-consistency window
is the price of skipping a Redis pub/sub for this small admin table.
A mutation in worker A → at most 30s before worker B picks it up.
Acceptable because admin-driven add / remove isn't a hot path.
"""
from __future__ import annotations

import logging
import threading
import time

from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models.trusted_host import TrustedHost
from app.models.user import User
from app.services.audit_service import log_audit_event

logger = logging.getLogger(__name__)


# Cache state — module-level so the anila-core provider hook can read
# without a request-scoped session.
_cache_lock = threading.RLock()
_cached_hosts: set[str] = set()
_cache_expires_at: float = 0.0

# TTL between automatic refreshes when no mutation has happened in this
# process. Mutations call ``_invalidate_cache()`` for immediate effect.
CACHE_TTL_SECONDS = 30.0


# ── core helpers ──────────────────────────────────────────────────────────────


def _normalize(host: str) -> str:
    return host.strip().lower()


def _load_hosts_from_db(db: Session) -> set[str]:
    rows = db.query(TrustedHost.host).all()
    return {row[0].lower() for row in rows if row[0]}


def _invalidate_cache() -> None:
    """Force the next ``get_cached_hosts()`` call to re-read from DB."""
    with _cache_lock:
        global _cache_expires_at
        _cache_expires_at = 0.0


def get_cached_hosts() -> set[str]:
    """Return the cached trusted-host set. Refreshes from DB if TTL expired.

    Called by the anila-core ``url_guard`` provider hook on every
    ``validate_outbound_url`` call; must stay fast. Errors (DB down,
    table missing) return the last good snapshot rather than raising —
    the env-based fallback in anila-core keeps SSRF guard correct even
    when this provider is degraded.
    """
    global _cache_expires_at, _cached_hosts
    with _cache_lock:
        now = time.monotonic()
        if now < _cache_expires_at:
            return set(_cached_hosts)
    # Refresh outside the lock to avoid blocking other readers on a slow DB.
    try:
        db = SessionLocal()
        try:
            fresh = _load_hosts_from_db(db)
        finally:
            db.close()
    except Exception:  # noqa: BLE001 — security path: fail-safe to last snapshot
        logger.exception("trusted_host_service: DB read failed; keeping last cache")
        with _cache_lock:
            return set(_cached_hosts)
    with _cache_lock:
        _cached_hosts = fresh
        _cache_expires_at = time.monotonic() + CACHE_TTL_SECONDS
        return set(fresh)


# ── CRUD (for the /api/trusted-hosts router) ──────────────────────────────────


def list_hosts(db: Session) -> list[TrustedHost]:
    return (
        db.query(TrustedHost)
        .order_by(TrustedHost.host)
        .all()
    )


def get_host(db: Session, host_id: int) -> TrustedHost | None:
    return db.query(TrustedHost).filter(TrustedHost.id == host_id).first()


def add_host(
    db: Session,
    *,
    host: str,
    note: str | None,
    actor: User,
    commit: bool = True,
) -> TrustedHost:
    """Insert a host. Idempotent on the unique ``host`` column —
    duplicates return the existing row (200-like UX) so backfill from
    env + admin UI both work safely.

    Writes an ``audit_log`` row per add so security review can trace
    who grew the allow-list and why (the ``note`` column is the
    human-readable why).
    """
    normalized = _normalize(host)
    existing = (
        db.query(TrustedHost)
        .filter(TrustedHost.host == normalized)
        .first()
    )
    if existing is not None:
        # Update note if the caller supplied one; idempotent-with-refresh.
        if note is not None and note != existing.note:
            existing.note = note
            if commit:
                db.commit()
                db.refresh(existing)
            else:
                db.flush()
            log_audit_event(
                db,
                actor=actor,
                action="trusted_host.update",
                resource_type="trusted_host",
                resource_id=existing.id,
                detail=f"更新受信任 host「{normalized}」的備註",
                commit=commit,
            )
        return existing

    row = TrustedHost(
        host=normalized,
        note=note,
        created_by_user_id=actor.id,
    )
    db.add(row)
    if commit:
        db.commit()
        db.refresh(row)
    else:
        db.flush()
    log_audit_event(
        db,
        actor=actor,
        action="trusted_host.create",
        resource_type="trusted_host",
        resource_id=row.id,
        detail=f"加入受信任 host「{normalized}」",
        commit=commit,
    )
    if commit:
        _invalidate_cache()
    return row


def remove_host(db: Session, *, host_id: int, actor: User) -> bool:
    row = get_host(db, host_id)
    if row is None:
        return False
    host = row.host
    db.delete(row)
    db.commit()
    log_audit_event(
        db,
        actor=actor,
        action="trusted_host.delete",
        resource_type="trusted_host",
        resource_id=host_id,
        detail=f"移除受信任 host「{host}」",
        commit=True,
    )
    _invalidate_cache()
    return True


# ── anila-core hookup (called once at app startup) ────────────────────────────


def register_with_url_guard() -> None:
    """Wire the cached host provider into anila-core's SSRF guard.

    Call this once at CSP app startup AFTER the DB is ready. The
    provider is a callable that returns the current cached set; the
    guard treats the result as additive on top of the env fallback,
    so an empty / failed provider doesn't weaken validation.
    """
    from anila_core.security import register_trusted_host_provider

    register_trusted_host_provider(get_cached_hosts)
