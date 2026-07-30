"""Invariant: apply_classification must not persist a classification DOWNGRADE.

Under expire_on_commit=False, db.get returns the identity-map instance loaded
earlier in the session. A concurrent writer can latch the row higher; a stale
max() then writes back a LOWER level. The fix re-reads with FOR UPDATE +
populate_existing before computing effective.

SQLite note
===========
``conftest`` uses ``sqlite://`` + ``StaticPool``: every "session" shares ONE
connection, and ``with_for_update()`` is a silent no-op. Tests in THIS file
therefore only cover the ``populate_existing`` / identity-map half. The
``FOR UPDATE`` half lives in ``test_classification_latch_pg.py`` (skipped
unless ``ANILA_TEST_PG_DSN`` is set) — a naive parallel-start harness is
insufficient; the barrier between SELECT and UPDATE is load-bearing.
"""

from __future__ import annotations

import os

from sqlalchemy.orm import sessionmaker

os.environ.setdefault("ANILA_ALLOW_DEV_SECRET", "1")

from app.models.conversation import Conversation
from app.modules.policy import apply_classification, effective_level
from app.schemas.contracts.classification import ClassificationLevel
from tests.conftest import make_user


def _make_conversation(db, user) -> Conversation:
    conv = Conversation(user_id=user.id, title="latch-race")
    db.add(conv)
    db.commit()
    db.refresh(conv)
    return conv


def test_stale_identity_map_cannot_downgrade_committed_level(db_engine):
    """Session A holds a stale lower snapshot; Session B raises; A must not win.

    Revert the populate_existing path in ``_resolve_resource`` (bare db.get)
    and this test fails: A writes 營業秘密 over the committed 機密.
    Does NOT prove FOR UPDATE — see test_classification_latch_pg.py.
    """
    Session = sessionmaker(bind=db_engine, expire_on_commit=False)

    seed = Session()
    try:
        user = make_user(seed, username="latch-race-user")
        conv = _make_conversation(seed, user)
        conv_id = conv.id
        actor_id = str(user.id)
    finally:
        seed.close()

    # Session A: load the row at 無機密 into the identity map, then stop.
    sess_a = Session()
    stale = (
        sess_a.query(Conversation).filter(Conversation.id == conv_id).one()
    )
    assert stale.classification_level == ClassificationLevel.UNCLASSIFIED.value

    # Session B: raise to 機密 and commit.
    sess_b = Session()
    try:
        ev = apply_classification(
            sess_b,
            resource_type="conversation",
            resource_id=str(conv_id),
            new_level="機密",
            actor_type="user",
            actor_id=actor_id,
            reason="manual_admin",
        )
        assert ev is not None
        assert (
            effective_level(
                sess_b, resource_type="conversation", resource_id=str(conv_id)
            )
            == ClassificationLevel.SECRET
        )
    finally:
        sess_b.close()

    # Session A still sees the stale in-memory level if it used bare db.get.
    assert stale.classification_level == ClassificationLevel.UNCLASSIFIED.value

    # A attempts a mid-level latch from its stale snapshot. Must NOT overwrite
    # the committed 機密 with 營業秘密.
    apply_classification(
        sess_a,
        resource_type="conversation",
        resource_id=str(conv_id),
        new_level="營業秘密",
        actor_type="user",
        actor_id=actor_id,
        reason="manual_admin",
    )
    sess_a.close()

    verify = Session()
    try:
        level = effective_level(
            verify, resource_type="conversation", resource_id=str(conv_id)
        )
        assert level == ClassificationLevel.SECRET, (
            f"stale writer persisted a downgrade to {level.value!r}"
        )
    finally:
        verify.close()


def test_noop_apply_classification_releases_transaction(db_engine):
    """No-op branch must end the transaction (release FOR UPDATE).

    Revert the ``db.commit()`` on the ``effective == current`` branch and
    ``session.in_transaction()`` stays True after return.
    """
    Session = sessionmaker(bind=db_engine, expire_on_commit=False)

    seed = Session()
    try:
        user = make_user(seed, username="latch-noop-user")
        conv = _make_conversation(seed, user)
        # Raise once so the steady-state no-op path is exercised.
        apply_classification(
            seed,
            resource_type="conversation",
            resource_id=str(conv.id),
            new_level="機密",
            actor_type="user",
            actor_id=str(user.id),
            reason="manual_admin",
        )
        conv_id = conv.id
        actor_id = str(user.id)
    finally:
        seed.close()

    db = Session()
    try:
        ev = apply_classification(
            db,
            resource_type="conversation",
            resource_id=str(conv_id),
            new_level="機密",
            actor_type="user",
            actor_id=actor_id,
            reason="manual_admin",
        )
        assert ev is None
        assert not db.in_transaction(), (
            "no-op apply_classification left the session in a transaction "
            "(FOR UPDATE would survive across the caller's SSE stream)"
        )
    finally:
        db.close()
