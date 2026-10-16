"""Fenced test lifecycle: synthetic SQLite rows, no provider/credential access."""

import asyncio
import hashlib
import threading
import time
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, event, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.quick_builder import QuickRun, QuickVersion
from app.services import quick_test_lifecycle as lifecycle
from tests.test_quick_builder_review import _assistant, review_rows  # noqa: F401


@pytest.fixture
def prepared(db, request):
    rows = request.getfixturevalue("review_rows")
    _, version, _ = _assistant(db, rows)
    return db, rows, version


def _claim(prepared, *, nonce=None, message="合成提問", user=None, commit=True):
    db, rows, version = prepared
    user = user or rows[0]
    row = QuickRun(
        version_id=version.id,
        user_id=user.id,
        status="running",
        result={
            "status": "running",
            "output": "",
            "steps": [],
        },
    )
    db.add(row)
    lease = lifecycle.claim_new(
        db,
        row,
        request_id=nonce,
        message=message,
        actor_department_id=user.department_id,
    )
    if commit:
        db.commit()
    return row, lease


def _expire(db, lease):
    db.execute(
        update(QuickRun)
        .where(QuickRun.id == lease.run_id)
        .values(
            lease_expires_at=lifecycle._now(db) - timedelta(seconds=2),
        )
        .execution_options(synchronize_session=False)
    )
    db.commit()


def _fresh(db, row):
    db.expire_all()
    return db.get(QuickRun, row.id)


def test_claim_flushes_private_canonical_columns_without_committing(prepared):
    db, rows, _ = prepared
    nonce = str(uuid4())
    message = "  保留原始繁中\n提問  "
    row, lease = _claim(prepared, nonce=nonce.upper(), message=message, commit=False)
    assert row.id == lease.run_id and row.user_id == lease.user_id
    assert row.is_test is True and row.test_request_id == nonce
    assert row.test_request_hash == hashlib.sha256(message.encode()).hexdigest()
    assert row.result["test_tracking"] == {
        "testing": True,
        "request_id": nonce,
        "request_hash": row.test_request_hash,
        "actor_department_id": rows[0].department_id,
    }
    assert row.execution_owner_token != nonce
    assert row.execution_owner_token not in repr(lease)
    assert row.lease_expires_at - row.lease_heartbeat_at == timedelta(seconds=30)
    identifier = row.id
    db.rollback()
    assert db.get(QuickRun, identifier) is None


@pytest.mark.parametrize("status", ["completed", "failed", "cancelled", "interrupted"])
def test_nonce_unique_for_user_across_versions_and_all_terminal_statuses(
    prepared, status
):
    db, rows, original = prepared
    nonce = str(uuid4())
    row, lease = _claim(prepared, nonce=nonce)
    if status == "interrupted":
        _expire(db, lease)
        assert lifecycle.recover_in_session(db, user_id=lease.user_id) == [row.id]
    else:
        assert lifecycle.finish_in_session(
            db, lease, status=status, result={"output": "好"}
        )
    db.commit()
    other_version = QuickVersion(
        assistant_id=original.assistant_id,
        revision=2,
        payload=original.payload,
        payload_hash="b" * 64,
        status="draft",
        created_by=rows[0].id,
    )
    db.add(other_version)
    db.commit()
    with pytest.raises(IntegrityError):
        _claim((db, rows, other_version), nonce=nonce)
    db.rollback()
    assert _fresh(db, row).status == status


def test_nonce_other_user_and_null_nonce_remain_distinct(prepared):
    db, rows, _ = prepared
    nonce = str(uuid4())
    first, _ = _claim(prepared, nonce=nonce)
    second, _ = _claim(prepared, nonce=nonce, user=rows[1])
    third, _ = _claim(prepared)
    fourth, _ = _claim(prepared)
    assert len({row.id for row in (first, second, third, fourth)}) == 4


@pytest.mark.parametrize("expired", [False, True])
def test_claim_never_reissues_existing_authority(prepared, expired):
    db, rows, _ = prepared
    row, lease = _claim(prepared)
    if expired:
        _expire(db, lease)
    with pytest.raises(ValueError, match="ALREADY_CLAIMED"):
        lifecycle.claim_new(
            db,
            row,
            request_id=None,
            message="same",
            actor_department_id=rows[0].department_id,
        )
    db.rollback()


def test_check_renew_and_finish_use_short_session_and_clear_authority(prepared):
    db, _, _ = prepared
    row, lease = _claim(prepared)
    bind = db.get_bind()
    assert lifecycle.check_lease(bind, lease)
    assert lifecycle.renew_lease(bind, lease, ttl_seconds=60)
    row = _fresh(db, row)
    assert row.lease_expires_at - row.lease_heartbeat_at == timedelta(seconds=60)
    db.rollback()
    assert lifecycle.finish_in_session(
        db,
        lease,
        status="completed",
        result={
            "output": "好",
            "test_tracking": {"request_id": "injected"},
        },
    )
    db.commit()
    row = _fresh(db, row)
    assert row.result["test_tracking"]["request_id"] is None
    assert row.execution_owner_token is None and row.lease_expires_at is None
    db.rollback()
    assert not lifecycle.check_lease(bind, lease)
    assert not lifecycle.renew_lease(bind, lease)
    assert not lifecycle.cancel_lease(bind, lease)
    assert not lifecycle.finish_in_session(db, lease, status="failed", result={})
    db.rollback()
    assert _fresh(db, row).status == "completed"


def test_expiry_recovery_is_unconfirmed_and_late_writes_never_replace_it(prepared):
    db, _, _ = prepared
    nonce = str(uuid4())
    row, lease = _claim(prepared, nonce=nonce)
    row.result = {
        **row.result,
        "output": "已安全儲存的部分文字",
        "artifacts": [{"filename": "private.txt"}],
    }
    db.commit()
    _expire(db, lease)
    bind = db.get_bind()
    assert not lifecycle.renew_lease(bind, lease)
    assert not lifecycle.check_lease(bind, lease)
    assert not lifecycle.cancel_lease(bind, lease)
    assert lifecycle.recover_expired(bind, user_id=lease.user_id) == [lease.run_id]
    assert lifecycle.recover_expired(bind, user_id=lease.user_id) == []
    row = _fresh(db, row)
    assert row.status == row.result["status"] == "interrupted"
    assert row.result["error"] == lifecycle.INTERRUPTED_ERROR
    assert row.result["answer_complete"] is False
    assert row.result["artifacts"] == []
    assert row.result["output"] == "已安全儲存的部分文字"
    assert row.test_request_id == nonce
    assert row.execution_owner_token is None
    db.rollback()
    assert not lifecycle.finish_in_session(
        db, lease, status="completed", result={"output": "late"}
    )
    db.rollback()
    with pytest.raises(IntegrityError):
        _claim(prepared, nonce=nonce)
    db.rollback()


def test_wrong_fence_cannot_check_finish_or_cancel_another_owner(prepared):
    db, _, _ = prepared
    row, lease = _claim(prepared)
    wrong = lifecycle.RunLease(lease.run_id, lease.user_id, str(uuid4()))
    assert not lifecycle.check_lease(db.get_bind(), wrong)
    assert not lifecycle.renew_lease(db.get_bind(), wrong)
    assert not lifecycle.finish_in_session(db, wrong, status="completed", result={})
    db.rollback()
    assert _fresh(db, row).status == "running"


def test_recovery_does_not_revoke_renewed_live_run(prepared):
    db, _, _ = prepared
    _, lease = _claim(prepared)
    assert lifecycle.renew_lease(db.get_bind(), lease, ttl_seconds=90)
    assert lifecycle.recover_expired(db.get_bind(), user_id=lease.user_id) == []
    assert lifecycle.check_lease(db.get_bind(), lease)


def test_owner_scoped_cancel_does_not_need_public_token_and_is_terminal(prepared):
    db, rows, _ = prepared
    row, lease = _claim(prepared)
    assert not lifecycle.cancel_run_in_session(db, user_id=rows[1].id, run_id=row.id)
    db.rollback()
    assert lifecycle.cancel_run_in_session(db, user_id=lease.user_id, run_id=row.id)
    db.commit()
    row = _fresh(db, row)
    assert row.status == "cancelled" and row.result["answer_complete"] is False
    db.rollback()
    assert not lifecycle.finish_in_session(db, lease, status="completed", result={})
    db.rollback()
    assert lifecycle.recover_expired(db.get_bind(), user_id=lease.user_id) == []


def test_legacy_marked_orphan_recovers_but_non_test_and_other_user_do_not(prepared):
    db, rows, version = prepared
    row, lease = _claim(prepared)
    row.execution_owner_token = None
    row.lease_expires_at = None
    legacy = QuickRun(
        version_id=version.id, user_id=rows[0].id, status="running", result={}
    )
    db.add(legacy)
    db.commit()
    assert lifecycle.recover_expired(db.get_bind(), user_id=rows[1].id) == []
    assert lifecycle.recover_expired(db.get_bind(), user_id=rows[0].id) == [
        lease.run_id
    ]
    assert _fresh(db, legacy).status == "running"


def test_finish_and_recover_are_rollbackable_with_callers_business_transaction(
    prepared,
):
    db, _, _ = prepared
    row, lease = _claim(prepared)
    assert lifecycle.finish_in_session(db, lease, status="completed", result={})
    db.rollback()
    assert _fresh(db, row).status == "running"
    db.rollback()
    _expire(db, lease)
    assert lifecycle.recover_in_session(db, user_id=lease.user_id) == [row.id]
    db.rollback()
    assert _fresh(db, row).status == "running"


def test_short_helpers_release_connection_even_on_error(prepared):
    db, _, _ = prepared
    _, lease = _claim(prepared)
    bind = db.get_bind()
    active = []

    def checkout(*_):
        active.append(True)

    def checkin(*_):
        active.pop()

    event.listen(bind, "checkout", checkout)
    event.listen(bind, "checkin", checkin)
    try:
        assert lifecycle.check_lease(bind, lease)
        assert active == []
        with pytest.raises(ValueError):
            lifecycle.renew_lease(bind, lease, ttl_seconds=0)
        assert active == []
        with pytest.raises(TypeError, match="REQUIRES_ENGINE"):
            lifecycle.check_lease(db, lease)
    finally:
        event.remove(bind, "checkout", checkout)
        event.remove(bind, "checkin", checkin)


def test_checkpoints_commit_safe_steps_preserve_private_data_and_reject_late_write(
    prepared,
):
    db, _, _ = prepared
    row, lease = _claim(prepared)
    row.result = {**row.result, "execution_context": {"model_id": 7}}
    db.commit()
    steps = [{"node_id": "model", "status": "completed", "output": "安全文字"}]
    assert lifecycle.checkpoint_in_session(db, lease, steps=steps)
    db.commit()
    row = _fresh(db, row)
    assert row.result["steps"] == steps
    assert row.result["execution_context"] == {"model_id": 7}
    assert row.result["test_tracking"]["testing"] is True
    db.rollback()
    assert lifecycle.cancel_lease(db.get_bind(), lease)
    assert not lifecycle.checkpoint_in_session(db, lease, steps=[{"output": "late"}])
    db.rollback()
    row = _fresh(db, row)
    assert row.result["steps"] == steps and row.status == "cancelled"


def test_check_and_renew_do_not_load_result_blob(prepared):
    db, _, _ = prepared
    _, lease = _claim(prepared)
    statements = []

    def record(_connection, _cursor, statement, *_args):
        statements.append(statement)

    bind = db.get_bind()
    event.listen(bind, "before_cursor_execute", record)
    try:
        assert lifecycle.check_lease(bind, lease)
        assert lifecycle.renew_lease(bind, lease)
    finally:
        event.remove(bind, "before_cursor_execute", record)
    selects = [
        statement
        for statement in statements
        if statement.lstrip().upper().startswith("SELECT")
    ]
    assert selects and all(
        "quick_runs.result" not in statement for statement in selects
    )


def test_bounded_recovery_preserves_other_run_and_requires_scope(prepared):
    db, _, _ = prepared
    _, first = _claim(prepared)
    _, second = _claim(prepared)
    _expire(db, first)
    _expire(db, second)
    assert lifecycle.recover_expired(
        db.get_bind(), user_id=first.user_id, run_id=second.run_id, limit=1
    ) == [second.run_id]
    assert lifecycle.recover_expired(db.get_bind(), user_id=first.user_id, limit=1) == [
        first.run_id
    ]
    with pytest.raises(ValueError, match="SCOPE"):
        lifecycle.recover_expired(db.get_bind(), user_id=None)
    with pytest.raises(ValueError, match="LIMIT"):
        lifecycle.recover_expired(db.get_bind(), user_id=first.user_id, limit=101)


def test_wrong_user_handle_cannot_use_valid_owner_token(prepared):
    db, rows, _ = prepared
    _, lease = _claim(prepared)
    other = lifecycle.RunLease(lease.run_id, rows[1].id, lease.owner_token)
    assert not lifecycle.check_lease(db.get_bind(), other)
    assert not lifecycle.finish_in_session(db, other, status="completed", result={})
    db.rollback()


def test_short_worker_phase_does_not_block_loop_waiting_for_single_pool_connection(
    tmp_path,
):
    bind = create_engine(
        "sqlite:///" + str(tmp_path / "lifecycle-pool.db"),
        connect_args={"check_same_thread": False},
        pool_size=1,
        max_overflow=0,
        pool_timeout=0.25,
    )
    QuickRun.__table__.create(bind)
    with Session(bind=bind) as db:
        row = QuickRun(version_id=1, user_id=1, status="running", result={})
        db.add(row)
        lease = lifecycle.claim_new(
            db, row, request_id=None, message="synthetic", actor_department_id=None
        )
        db.commit()
    owner_thread = threading.get_ident()
    execution_threads = []

    def record(*_):
        execution_threads.append(threading.get_ident())

    event.listen(bind, "before_cursor_execute", record)
    held = bind.connect()
    released = []

    async def scenario():
        loop = asyncio.get_running_loop()
        started = time.monotonic()

        def release():
            released.append(time.monotonic() - started)
            held.close()

        loop.call_later(0.025, release)
        assert await asyncio.to_thread(lifecycle.check_lease, bind, lease)

    try:
        asyncio.run(scenario())
        assert released and released[0] < 0.2
        assert execution_threads and owner_thread not in execution_threads
        assert bind.pool.checkedout() == 0
    finally:
        held.close()
        event.remove(bind, "before_cursor_execute", record)
        bind.dispose()


@pytest.mark.parametrize("token", [None, "", "bad", True, 7])
def test_handle_requires_nonempty_canonical_private_token(token):
    with pytest.raises(ValueError):
        lifecycle.RunLease(1, 1, token)


@pytest.mark.parametrize(
    "change",
    [
        {"testing": 1},
        {"request_id": "not-a-uuid"},
        {"request_hash": "A" * 64},
        {"actor_department_id": True},
        {"actor_department_id": 0},
        {"extra": "untrusted"},
    ],
)
def test_legacy_backfill_rejects_malformed_or_noncanonical_marker(change):
    marker = {
        "testing": True,
        "request_id": str(uuid4()),
        "request_hash": "a" * 64,
        "actor_department_id": 1,
    }
    marker.update(change)
    assert lifecycle.legacy_tracking_columns({"test_tracking": marker}) is None


@pytest.mark.parametrize("nonce", [None, str(uuid4())])
def test_legacy_backfill_has_no_content_and_does_not_create_lease(nonce):
    marker = {
        "testing": True,
        "request_id": nonce,
        "request_hash": "a" * 64,
        "actor_department_id": None,
    }
    result = lifecycle.legacy_tracking_columns(
        {"test_tracking": marker, "output": "ignored"}
    )
    assert result == {
        "is_test": True,
        "test_request_id": nonce,
        "test_request_hash": "a" * 64,
        "test_actor_department_id": None,
    }
    assert "execution_owner_token" not in result


@pytest.mark.parametrize("duration", [True, 0, -1, 4, 301, "30"])
def test_invalid_ttl_never_creates_authority(prepared, duration):
    db, rows, version = prepared
    row = QuickRun(
        version_id=version.id, user_id=rows[0].id, status="running", result={}
    )
    db.add(row)
    with pytest.raises(ValueError, match="DURATION"):
        lifecycle.claim_new(
            db,
            row,
            request_id=None,
            message="x",
            actor_department_id=None,
            ttl_seconds=duration,
        )
    assert row.execution_owner_token is None
    db.rollback()
