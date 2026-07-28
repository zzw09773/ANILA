"""True-PostgreSQL race proofs for the Gate 2 classification latch.

Set ``ANILA_GATE2_CLASSIFICATION_PG_URL`` (or the existing
``ANILA_GATE2_LEDGER_PG_URL``) to a migrated disposable CSP database.  The
tests intentionally preload stale ORM rows in two independent sessions, then
use a PostgreSQL row-lock wait as the barrier proof.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os
import threading
import time
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.models.audit_log import AuditLog
from app.models.classification import (
    ClassificationAuthorityAssignment,
    ClassificationEvent,
    DeclassificationRequest,
)
from app.models.conversation import Conversation
from app.models.user import User
from app.modules.policy import (
    apply_classification,
    create_declassification_request,
    decide_declassification,
)


_DSN = os.environ.get("ANILA_GATE2_CLASSIFICATION_PG_URL") or os.environ.get(
    "ANILA_GATE2_LEDGER_PG_URL"
)
pytestmark = pytest.mark.skipif(
    not _DSN,
    reason=(
        "ANILA_GATE2_CLASSIFICATION_PG_URL / "
        "ANILA_GATE2_LEDGER_PG_URL is not set"
    ),
)


def _engine():
    return create_engine(
        _DSN,
        pool_pre_ping=True,
        connect_args={"options": "-c lock_timeout=8000ms"},
    )


def _wait_until_backend_is_lock_blocked(engine, pid: int) -> None:
    """Prove the lower writer reached PostgreSQL and is waiting on the row."""
    deadline = time.monotonic() + 5
    with engine.connect() as connection:
        while time.monotonic() < deadline:
            wait_event_type = connection.execute(
                text(
                    "SELECT wait_event_type FROM pg_stat_activity "
                    "WHERE pid = :pid"
                ),
                {"pid": pid},
            ).scalar_one_or_none()
            if wait_event_type == "Lock":
                return
            time.sleep(0.02)
    raise AssertionError(f"PostgreSQL backend {pid} never entered a lock wait")


def _assert_ledger_is_unused(factory, conversation_id: int) -> None:
    """這兩支測試的斷言是「這個資源恰好有 N 筆分類事件」,而
    ``classification_events`` 自 r1_0041 起 append-only —— 任何測試在共用資料庫
    裡對一個**寫死的** resource_id 插事件,那筆列就永遠留著,並且會在未來某個真
    的拿到同一個 id 的對話身上冒出來,把它偽裝成單向閂鎖漏了一筆 ledger。

    所以在跑競態之前先確認這個對話 id 的帳是空的。這樣「有人污染了這個 id」會
    在這裡當場說清楚,而不是等到最後的事件鏈斷言,讓人去追一個不存在的鎖 bug。
    """
    session = factory()
    try:
        existing = (
            session.query(ClassificationEvent)
            .filter(
                ClassificationEvent.resource_type == "conversation",
                ClassificationEvent.resource_id == str(conversation_id),
            )
            .order_by(ClassificationEvent.id)
            .all()
        )
        assert existing == [], (
            f"conversation#{conversation_id} 在競態開始前就已經有分類事件 "
            f"{[(e.id, e.previous_level, e.new_level, e.reason) for e in existing]} —— "
            "有測試對寫死的 resource_id 插了 append-only 事件,污染了這個 id"
        )
    finally:
        session.close()


def _user(*, username: str, role: str = "user") -> User:
    return User(
        username=username,
        role=role,
        hashed_password="synthetic-not-a-secret",
        is_active=True,
        is_approved=True,
    )


def test_concurrent_high_low_updates_preserve_max_and_event_chain() -> None:
    engine = _engine()
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    suffix = uuid.uuid4().hex[:12]
    seed = factory()
    user_id: int | None = None
    conversation_id: int | None = None
    try:
        user = _user(username=f"gate2-latch-{suffix}")
        seed.add(user)
        seed.flush()
        conversation = Conversation(user_id=user.id, title="PG latch race")
        seed.add(conversation)
        seed.commit()
        user_id = int(user.id)
        conversation_id = int(conversation.id)
        _assert_ledger_is_unused(factory, conversation_id)

        start = threading.Barrier(2)
        high_locked = threading.Event()
        low_entered = threading.Event()
        release_high = threading.Event()
        low_pid: list[int] = []

        def raise_high() -> tuple[str, str]:
            db = factory()
            try:
                cached = db.get(Conversation, conversation_id)
                assert cached.classification_level == "無機密"
                start.wait(timeout=5)
                event = apply_classification(
                    db,
                    resource_type="conversation",
                    resource_id=str(conversation_id),
                    new_level="絕對機密",
                    actor_type="user",
                    actor_id=str(user_id),
                    reason="content_detection",
                    commit=False,
                )
                assert event is not None
                high_locked.set()
                assert release_high.wait(timeout=10)
                db.commit()
                return event.previous_level, event.new_level
            except Exception:
                db.rollback()
                raise
            finally:
                db.close()

        def attempt_lower_update() -> None:
            db = factory()
            try:
                cached = db.get(Conversation, conversation_id)
                assert cached.classification_level == "無機密"
                start.wait(timeout=5)
                assert high_locked.wait(timeout=5)
                low_pid.append(
                    int(db.execute(text("SELECT pg_backend_pid()")).scalar_one())
                )
                low_entered.set()
                event = apply_classification(
                    db,
                    resource_type="conversation",
                    resource_id=str(conversation_id),
                    new_level="機密",
                    actor_type="user",
                    actor_id=str(user_id),
                    reason="manual_admin",
                )
                assert event is None
            except Exception:
                db.rollback()
                raise
            finally:
                db.close()

        with ThreadPoolExecutor(max_workers=2) as executor:
            high_future = executor.submit(raise_high)
            low_future = executor.submit(attempt_lower_update)
            try:
                if not low_entered.wait(timeout=5):
                    release_high.set()
                    high_future.result(timeout=10)
                    low_future.result(timeout=10)
                    raise AssertionError("lower writer never reached PostgreSQL")
                _wait_until_backend_is_lock_blocked(engine, low_pid[0])
            finally:
                release_high.set()
            assert high_future.result(timeout=10) == ("無機密", "絕對機密")
            low_future.result(timeout=10)

        verify = factory()
        try:
            current = verify.get(Conversation, conversation_id)
            assert current.classification_level == "絕對機密"
            events = (
                verify.query(ClassificationEvent)
                .filter(
                    ClassificationEvent.resource_type == "conversation",
                    ClassificationEvent.resource_id == str(conversation_id),
                )
                .order_by(ClassificationEvent.id)
                .all()
            )
            assert [
                (event.previous_level, event.new_level) for event in events
            ] == [("無機密", "絕對機密")]
        finally:
            verify.close()
    finally:
        seed.rollback()
        seed.close()
        if user_id is not None and conversation_id is not None:
            cleanup = factory()
            try:
                # r1_0041 made the three audit ledgers append-only: csp_app has no DELETE
                # on them, and a test that deletes from one is asserting the absence of the
                # property the migration exists to create. Rows here are already scoped to
                # this run's own ids, so leftovers cannot bleed into another test.
                conversation = cleanup.get(Conversation, conversation_id)
                if conversation is not None:
                    cleanup.delete(conversation)
                # r1_0041 leaves audit_logs.actor_user_id without ON DELETE, so the
                # actor reference is nulled in the application layer before the user
                # row goes away — that is how the audit history survives a hard
                # delete (canonical implementation: api/users.py 'manual cleanup #2').
                cleanup.query(AuditLog).filter(AuditLog.actor_user_id == user_id).update(
                    {"actor_user_id": None}, synchronize_session=False
                )
                user = cleanup.get(User, user_id)
                if user is not None:
                    cleanup.delete(user)
                cleanup.commit()
            finally:
                cleanup.close()
        engine.dispose()


def test_concurrent_raise_invalidates_stale_approved_declassification() -> None:
    engine = _engine()
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    suffix = uuid.uuid4().hex[:12]
    seed = factory()
    user_ids: list[int] = []
    conversation_id: int | None = None
    request_id: int | None = None
    try:
        admin = _user(username=f"gate2-declass-admin-{suffix}", role="admin")
        approver = _user(
            username=f"gate2-declass-approver-{suffix}", role="admin"
        )
        seed.add_all([admin, approver])
        seed.flush()
        user_ids = [int(admin.id), int(approver.id)]
        conversation = Conversation(
            user_id=admin.id, title="PG declassification race"
        )
        seed.add(conversation)
        seed.flush()
        authority = ClassificationAuthorityAssignment(
            user_id=approver.id,
            authority_reference=f"synthetic-{suffix}",
        )
        seed.add(authority)
        seed.commit()
        conversation_id = int(conversation.id)
        _assert_ledger_is_unused(factory, conversation_id)

        initial = apply_classification(
            seed,
            resource_type="conversation",
            resource_id=str(conversation_id),
            new_level="機密",
            actor_type="user",
            actor_id=str(admin.id),
            reason="manual_admin",
        )
        assert initial is not None
        request = create_declassification_request(
            seed,
            resource_type="conversation",
            resource_id=str(conversation_id),
            to_level="無機密",
            requested_by_admin_id=int(admin.id),
            reason="synthetic race proof",
        )
        request_id = int(request.id)

        start = threading.Barrier(2)
        high_locked = threading.Event()
        low_entered = threading.Event()
        release_high = threading.Event()
        low_pid: list[int] = []

        def concurrent_raise() -> None:
            db = factory()
            try:
                cached = db.get(Conversation, conversation_id)
                assert cached.classification_level == "機密"
                start.wait(timeout=5)
                event = apply_classification(
                    db,
                    resource_type="conversation",
                    resource_id=str(conversation_id),
                    new_level="絕對機密",
                    actor_type="service",
                    actor_id="detector",
                    reason="content_detection",
                    commit=False,
                )
                assert event is not None
                high_locked.set()
                assert release_high.wait(timeout=10)
                db.commit()
            except Exception:
                db.rollback()
                raise
            finally:
                db.close()

        def attempt_stale_declassification() -> str:
            db = factory()
            try:
                cached = db.get(Conversation, conversation_id)
                assert cached.classification_level == "機密"
                cached_request = db.get(DeclassificationRequest, request_id)
                assert cached_request.status == "pending_supervisor"
                start.wait(timeout=5)
                assert high_locked.wait(timeout=5)
                low_pid.append(
                    int(db.execute(text("SELECT pg_backend_pid()")).scalar_one())
                )
                low_entered.set()
                with pytest.raises(ValueError, match="降級申請後變更"):
                    decide_declassification(
                        db,
                        request_id=request_id,
                        approver_user_id=user_ids[1],
                        approve=True,
                        via="in_system",
                    )
                db.rollback()
                return "stale_rejected"
            finally:
                db.close()

        with ThreadPoolExecutor(max_workers=2) as executor:
            high_future = executor.submit(concurrent_raise)
            low_future = executor.submit(attempt_stale_declassification)
            try:
                if not low_entered.wait(timeout=5):
                    release_high.set()
                    high_future.result(timeout=10)
                    low_future.result(timeout=10)
                    raise AssertionError(
                        "declassification writer never reached PostgreSQL"
                    )
                _wait_until_backend_is_lock_blocked(engine, low_pid[0])
            finally:
                release_high.set()
            high_future.result(timeout=10)
            assert low_future.result(timeout=10) == "stale_rejected"

        verify = factory()
        try:
            current = verify.get(Conversation, conversation_id)
            stale_request = verify.get(DeclassificationRequest, request_id)
            assert current.classification_level == "絕對機密"
            assert stale_request.status == "pending_supervisor"
            events = (
                verify.query(ClassificationEvent)
                .filter(
                    ClassificationEvent.resource_type == "conversation",
                    ClassificationEvent.resource_id == str(conversation_id),
                )
                .order_by(ClassificationEvent.id)
                .all()
            )
            assert [
                (event.previous_level, event.new_level) for event in events
            ] == [("無機密", "機密"), ("機密", "絕對機密")]
            assert all(
                event.reason != "declassification_copy" for event in events
            )
        finally:
            verify.close()
    finally:
        seed.rollback()
        seed.close()
        if user_ids:
            cleanup = factory()
            try:
                if request_id is not None:
                    cleanup.query(DeclassificationRequest).filter(
                        DeclassificationRequest.id == request_id
                    ).delete(synchronize_session=False)
                cleanup.query(ClassificationAuthorityAssignment).filter(
                    ClassificationAuthorityAssignment.user_id.in_(user_ids)
                ).delete(synchronize_session=False)
                if conversation_id is not None:
                    conversation = cleanup.get(Conversation, conversation_id)
                    if conversation is not None:
                        cleanup.delete(conversation)
                for user_id in user_ids:
                    # r1_0041 leaves audit_logs.actor_user_id without ON DELETE, so the
                    # actor reference is nulled in the application layer before the user
                    # row goes away — that is how the audit history survives a hard
                    # delete (canonical implementation: api/users.py 'manual cleanup #2').
                    cleanup.query(AuditLog).filter(AuditLog.actor_user_id == user_id).update(
                        {"actor_user_id": None}, synchronize_session=False
                    )
                    user = cleanup.get(User, user_id)
                    if user is not None:
                        cleanup.delete(user)
                cleanup.commit()
            finally:
                cleanup.close()
        engine.dispose()
