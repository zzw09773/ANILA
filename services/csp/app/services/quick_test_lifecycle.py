"""Private, fenced execution authority for Builder tests.

Only ``claim_new`` can issue authority, in the new-run creation transaction.
Expiry revokes authority; it never grants permission to retry a request UUID.
Own-session functions are synchronous short DB phases intended for to_thread;
pass an Engine, never a caller's Session/Connection across threads. They do not
perform authorization: the API must retain its caller/actor/source checks.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import timedelta, timezone
from uuid import UUID, uuid4

from sqlalchemy import func, inspect, or_, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.models.quick_builder import QuickRun

DEFAULT_LEASE_SECONDS = 30
MAX_RECOVERY_BATCH = 100
INTERRUPTED_ERROR = {
    "type": "RESULT_UNCONFIRMED",
    "message": "執行權已失效，結果尚未確認；系統未重新執行此試跑。",
}
_HASH = re.compile(r"[0-9a-f]{64}\Z")


@dataclass(frozen=True)
class RunLease:
    run_id: int
    user_id: int
    owner_token: str = field(repr=False)

    def __post_init__(self):
        if not _positive(self.run_id) or not _positive(self.user_id):
            raise ValueError("INVALID_RUN_LEASE")
        if (
            not isinstance(self.owner_token, str)
            or canonical_request_id(self.owner_token) != self.owner_token
        ):
            raise ValueError("INVALID_RUN_LEASE")


def _positive(value):
    return type(value) is int and value > 0


def canonical_request_id(value):
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("INVALID_TEST_REQUEST_ID")
    try:
        return str(UUID(value))
    except (ValueError, AttributeError) as exc:
        raise ValueError("INVALID_TEST_REQUEST_ID") from exc


def request_hash(message):
    if not isinstance(message, str):
        raise ValueError("INVALID_TEST_MESSAGE")
    return hashlib.sha256(message.encode("utf-8")).hexdigest()


def legacy_tracking_columns(result):
    """Validate an old private marker for migration; never infer from content.

    Duplicate non-null user/UUID pairs must be resolved fail-closed BEFORE adding
    the unique constraint. Backfill must not create leases for old producers.
    """
    marker = result.get("test_tracking") if isinstance(result, dict) else None
    keys = {"testing", "request_id", "request_hash", "actor_department_id"}
    if not isinstance(marker, dict) or set(marker) != keys:
        return None
    if marker["testing"] is not True:
        return None
    try:
        nonce = canonical_request_id(marker["request_id"])
    except ValueError:
        return None
    digest = marker["request_hash"]
    department = marker["actor_department_id"]
    if (
        nonce != marker["request_id"]
        or not isinstance(digest, str)
        or not _HASH.fullmatch(digest)
        or (department is not None and not _positive(department))
    ):
        return None
    return {
        "is_test": True,
        "test_request_id": nonce,
        "test_request_hash": digest,
        "test_actor_department_id": department,
    }


def _marker(row):
    return {
        "testing": True,
        "request_id": row.test_request_id,
        "request_hash": row.test_request_hash,
        "actor_department_id": row.test_actor_department_id,
    }


def _clock(db):
    return (
        func.clock_timestamp()
        if db.get_bind().dialect.name == "postgresql"
        else func.current_timestamp()
    )


def _now(db):
    value = db.scalar(select(_clock(db)))
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _ttl(seconds):
    if type(seconds) is not int or not 5 <= seconds <= 300:
        raise ValueError("INVALID_TEST_LEASE_DURATION")
    return timedelta(seconds=seconds)


def claim_new(
    db,
    row,
    *,
    request_id,
    message,
    actor_department_id,
    ttl_seconds=DEFAULT_LEASE_SECONDS,
):
    """Flush a fresh pending run with its sole lease; caller commits/rolls back.

    Caller must hold the existing User mutex and authorize the test first. No
    persistent (including expired) row can be claimed through this operation.
    """
    duration = _ttl(ttl_seconds)
    nonce = canonical_request_id(request_id)
    if actor_department_id is not None and not _positive(actor_department_id):
        raise ValueError("INVALID_TEST_ACTOR_DEPARTMENT")
    state = inspect(row)
    if (
        not isinstance(row, QuickRun)
        or not state.pending
        or state.session is not db
        or row.id is not None
        or row.status != "running"
        or not _positive(row.user_id)
        or not _positive(row.version_id)
        or row.execution_owner_token is not None
        or row.lease_expires_at is not None
    ):
        raise ValueError("TEST_RUN_ALREADY_CLAIMED")
    # no_autoflush is required: a pending run must remain new until initialized.
    with db.no_autoflush:
        current = _now(db)
    row.is_test = True
    row.test_request_id = nonce
    row.test_request_hash = request_hash(message)
    row.test_actor_department_id = actor_department_id
    row.execution_owner_token = str(uuid4())
    row.lease_heartbeat_at = current
    row.lease_expires_at = current + duration
    row.result = {**(row.result or {}), "test_tracking": _marker(row)}
    db.flush()
    return RunLease(row.id, row.user_id, row.execution_owner_token)


def _identity(lease):
    if not isinstance(lease, RunLease):
        raise ValueError("INVALID_RUN_LEASE")
    return (
        QuickRun.id == lease.run_id,
        QuickRun.user_id == lease.user_id,
        QuickRun.is_test.is_(True),
        QuickRun.status == "running",
        QuickRun.execution_owner_token == lease.owner_token,
    )


def _lock_live(db, lease, *, with_result=True):
    # NOWAIT prevents a competing transaction from holding up the producer.
    # Clock is evaluated again in the CAS after acquiring the row lock.
    return db.scalar(
        select(QuickRun if with_result else QuickRun.id)
        .where(*_identity(lease), QuickRun.lease_expires_at > _clock(db))
        .with_for_update(nowait=True, of=QuickRun)
        .execution_options(populate_existing=True)
    )


def _cas(db, lease, **values):
    return (
        db.execute(
            update(QuickRun)
            .where(*_identity(lease), QuickRun.lease_expires_at > _clock(db))
            .values(**values)
            .execution_options(synchronize_session=False)
        ).rowcount
        == 1
    )


def check_in_session(db, lease):
    """Fenced check, without extending authority. Caller must end transaction."""
    if _lock_live(db, lease, with_result=False) is None:
        return False
    return _cas(db, lease, lease_heartbeat_at=_clock(db))


def renew_in_session(db, lease, *, ttl_seconds=DEFAULT_LEASE_SECONDS):
    duration = _ttl(ttl_seconds)
    if _lock_live(db, lease, with_result=False) is None:
        return False
    current = _now(db)
    return _cas(
        db, lease, lease_heartbeat_at=current, lease_expires_at=current + duration
    )


def checkpoint_in_session(db, lease, *, steps):
    """Persist execution-service-masked steps before publishing their events.

    Keep all existing private keys. Caller controls the existing flow step/byte
    bounds, supplies a detached list, and commits before queue/network awaits.
    """
    if not isinstance(steps, list) or any(not isinstance(step, dict) for step in steps):
        raise ValueError("INVALID_TEST_CHECKPOINT")
    row = _lock_live(db, lease)
    if row is None:
        return False
    value = {
        **(row.result or {}),
        "steps": steps,
        "status": "running",
        "test_tracking": _marker(row),
    }
    return _cas(db, lease, result=value)


def finish_in_session(db, lease, *, status, result):
    """Terminal CAS, no commit; audit/artifact changes belong in same transaction.

    Result must already be masked by the execution service. A false return means
    lost authority: discard late artifacts/results and do not publish completion.
    """
    if status not in {"completed", "failed", "cancelled"} or not isinstance(
        result, dict
    ):
        raise ValueError("INVALID_TEST_TERMINAL_RESULT")
    row = _lock_live(db, lease)
    if row is None:
        return False
    stored = {**result, "status": status, "test_tracking": _marker(row)}
    return _cas(
        db,
        lease,
        status=status,
        result=stored,
        execution_owner_token=None,
        lease_expires_at=None,
        lease_heartbeat_at=_clock(db),
    )


def cancel_run_in_session(db, *, user_id, run_id):
    """Explicit server cancel after fresh API authorization; token stays private.

    An expired run cannot be relabelled cancelled. Recover it as unconfirmed.
    Caller must invalidate artifacts/audit before committing a true result.
    """
    if not _positive(user_id) or not _positive(run_id):
        raise ValueError("INVALID_CANCELLATION_SCOPE")
    row = db.scalar(
        select(QuickRun)
        .where(
            QuickRun.id == run_id,
            QuickRun.user_id == user_id,
            QuickRun.is_test.is_(True),
            QuickRun.status == "running",
            QuickRun.lease_expires_at > _clock(db),
            QuickRun.execution_owner_token.is_not(None),
        )
        .with_for_update(nowait=True, of=QuickRun)
        .execution_options(populate_existing=True)
    )
    if row is None:
        return False
    lease = RunLease(row.id, row.user_id, row.execution_owner_token)
    return _cancel_owned(db, lease)


def _cancel_owned(db, lease):
    row = _lock_live(db, lease)
    if row is None:
        return False
    result = {
        **(row.result if isinstance(row.result, dict) else {}),
        "answer_complete": False,
        "usage_complete": False,
        "artifacts": [],
        "error": {"type": "CANCELLED", "message": "執行已停止，結果尚未完成。"},
    }
    return finish_in_session(db, lease, status="cancelled", result=result)


def recover_in_session(db, *, user_id, run_id=None, limit=20):
    """Revoke a bounded caller-scoped set; no retries and no implicit commit.

    Caller handles artifact invalidation/audit in this same transaction using
    returned IDs. Only existing safe result data is retained; links are removed.
    Old marked rows with no lease are orphans, not eligible for a new producer.
    """
    if not _positive(user_id) or (run_id is not None and not _positive(run_id)):
        raise ValueError("INVALID_RECOVERY_SCOPE")
    if type(limit) is not int or not 1 <= limit <= MAX_RECOVERY_BATCH:
        raise ValueError("INVALID_RECOVERY_LIMIT")
    expired = or_(
        QuickRun.execution_owner_token.is_(None),
        QuickRun.lease_expires_at.is_(None),
        QuickRun.lease_expires_at <= _clock(db),
    )
    conditions = [
        QuickRun.user_id == user_id,
        QuickRun.is_test.is_(True),
        QuickRun.status == "running",
        expired,
    ]
    if run_id is not None:
        conditions.append(QuickRun.id == run_id)
    rows = db.scalars(
        select(QuickRun)
        .where(*conditions)
        .order_by(QuickRun.id)
        .limit(limit)
        .with_for_update(skip_locked=True, of=QuickRun)
        .execution_options(populate_existing=True)
    ).all()
    recovered = []
    for row in rows:
        value = {
            **(row.result if isinstance(row.result, dict) else {}),
            "status": "interrupted",
            "answer_complete": False,
            "usage_complete": False,
            "error": dict(INTERRUPTED_ERROR),
            "artifacts": [],
            "test_tracking": _marker(row),
        }
        changed = db.execute(
            update(QuickRun)
            .where(
                *conditions,
                QuickRun.id == row.id,
                QuickRun.execution_owner_token == row.execution_owner_token,
            )
            .values(
                status="interrupted",
                result=value,
                execution_owner_token=None,
                lease_expires_at=None,
            )
            .execution_options(synchronize_session=False)
        ).rowcount
        if changed == 1:
            recovered.append(row.id)
    return recovered


def _short(bind, operation, *args, **kwargs):
    if not isinstance(bind, Engine):
        raise TypeError("TEST_LEASE_REQUIRES_ENGINE")
    with Session(bind=bind, expire_on_commit=False) as db:
        with db.begin():
            return operation(db, *args, **kwargs)


def check_lease(bind, lease):
    return _short(bind, check_in_session, lease)


def renew_lease(bind, lease, *, ttl_seconds=DEFAULT_LEASE_SECONDS):
    return _short(bind, renew_in_session, lease, ttl_seconds=ttl_seconds)


def cancel_lease(bind, lease):
    """Producer cancellation without artifact side effects; keeps safe progress.

    Artifact-producing callers use finish_in_session/cancel_run_in_session and
    invalidate artifacts in that same transaction.
    """
    # Server producer cancellation only. Browser stop-wait never calls this.
    return _short(bind, _cancel_owned, lease)


def recover_expired(bind, *, user_id, run_id=None, limit=20):
    """Short recovery for runs without artifact side effects.

    Artifact-producing integrations MUST use recover_in_session plus their
    existing invalidation/audit phase atomically, rather than this convenience.
    """
    return _short(bind, recover_in_session, user_id=user_id, run_id=run_id, limit=limit)
