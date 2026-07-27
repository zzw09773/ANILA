# -*- coding: utf-8 -*-
"""W2-3 / C3: messages.parent_id + edit semantics (real PostgreSQL).

Throwaway-PG pattern mirrors ``test_w210_governance_timestamptz_pg.py``.
``down_revision`` is read dynamically so retargeting r1_0043 → r1_0042 at
landing does not break this suite.

Run::

    docker run -d --rm --name throwaway-pg -e POSTGRES_PASSWORD=x \\
        -e POSTGRES_DB=csp -p 55432:5432 pgvector/pgvector:pg16
    ANILA_TEST_PG_DSN=postgresql://postgres:x@127.0.0.1:55432/csp \\
        python -m pytest tests/test_w23_message_tree_pg.py
"""
from __future__ import annotations

import os
import time
import uuid
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

_DSN = os.environ.get("ANILA_TEST_PG_DSN") or os.environ.get("TEST_POSTGRES_URL")
pytestmark = pytest.mark.skipif(
    not _DSN,
    reason="ANILA_TEST_PG_DSN / TEST_POSTGRES_URL not set —— 需要可丟棄的真 PostgreSQL",
)

CSP_ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS_DIR = CSP_ROOT / "migrations"
_REVISION = "r1_0043"


def _with_database(name: str) -> str:
    return make_url(_DSN).set(database=name).render_as_string(hide_password=False)


def _alembic_config(url: str) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


def _run_alembic(url: str, verb: str, target: str) -> None:
    previous = os.environ.get("MIGRATION_DATABASE_URL")
    os.environ["MIGRATION_DATABASE_URL"] = url
    try:
        getattr(command, verb)(_alembic_config(url), target)
    finally:
        if previous is None:
            os.environ.pop("MIGRATION_DATABASE_URL", None)
        else:
            os.environ["MIGRATION_DATABASE_URL"] = previous


def _predecessor(url: str) -> str:
    rev = ScriptDirectory.from_config(_alembic_config(url)).get_revision(_REVISION)
    down = rev.down_revision
    assert isinstance(down, str), f"{_REVISION} 應該只有一個 down_revision,實得 {down!r}"
    return down


@pytest.fixture()
def throwaway_db():
    name = f"w23_{uuid.uuid4().hex[:12]}"
    admin = create_engine(_with_database("postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{name}"'))
    try:
        yield _with_database(name)
    finally:
        with admin.connect() as connection:
            connection.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity"
                    " WHERE datname = :d AND pid <> pg_backend_pid()"
                ),
                {"d": name},
            )
            connection.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
        admin.dispose()


def test_alembic_upgrade_downgrade_upgrade(throwaway_db):
    """A1: clean PG upgrade → downgrade → upgrade; single head in worktree."""
    heads = ScriptDirectory.from_config(_alembic_config(throwaway_db)).get_heads()
    assert heads == [_REVISION], f"expected single head {_REVISION}, got {heads}"

    pred = _predecessor(throwaway_db)
    _run_alembic(throwaway_db, "upgrade", _REVISION)
    engine = create_engine(throwaway_db)
    with engine.connect() as conn:
        cols = {
            r[0]
            for r in conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns"
                    " WHERE table_name = 'messages' AND column_name = 'parent_id'"
                    " UNION ALL"
                    " SELECT column_name FROM information_schema.columns"
                    " WHERE table_name = 'conversations'"
                    "   AND column_name IN ('active_leaf_message_id', 'legal_hold')"
                )
            )
        }
        assert "parent_id" in cols
        assert "active_leaf_message_id" in cols
        assert "legal_hold" in cols
    engine.dispose()

    _run_alembic(throwaway_db, "downgrade", pred)
    engine = create_engine(throwaway_db)
    with engine.connect() as conn:
        gone = conn.execute(
            text(
                "SELECT column_name FROM information_schema.columns"
                " WHERE table_name = 'messages' AND column_name = 'parent_id'"
            )
        ).first()
        assert gone is None
    engine.dispose()

    _run_alembic(throwaway_db, "upgrade", _REVISION)


def test_backfill_linear_chain(throwaway_db):
    pred = _predecessor(throwaway_db)
    _run_alembic(throwaway_db, "upgrade", pred)
    engine = create_engine(throwaway_db)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO users (username, hashed_password, role, is_active)"
                " VALUES ('w23-u', 'x', 'user', TRUE)"
            )
        )
        uid = conn.execute(text("SELECT id FROM users WHERE username='w23-u'")).scalar_one()
        conn.execute(
            text(
                "INSERT INTO conversations (user_id, title)"
                " VALUES (:u, 'chain')"
            ),
            {"u": uid},
        )
        cid = conn.execute(text("SELECT id FROM conversations WHERE title='chain'")).scalar_one()
        for i, body in enumerate(["m0", "m1", "m2", "m3"], start=1):
            conn.execute(
                text(
                    "INSERT INTO messages (conversation_id, role, content, created_at)"
                    " VALUES (:c, 'user', :body,"
                    " TIMESTAMP '2026-07-27 00:00:00' + (:i || ' seconds')::interval)"
                ),
                {"c": cid, "body": body, "i": i},
            )
    engine.dispose()

    _run_alembic(throwaway_db, "upgrade", _REVISION)
    engine = create_engine(throwaway_db)
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT id, parent_id, content FROM messages"
                " WHERE conversation_id = :c ORDER BY id"
            ),
            {"c": cid},
        ).fetchall()
        assert rows[0].parent_id is None
        for i in range(1, len(rows)):
            assert rows[i].parent_id == rows[i - 1].id
    # Idempotent: re-run backfill path via downgrade+upgrade of just the column
    # is heavy; instead re-exec the UPDATE pattern and assert unchanged.
    with engine.begin() as conn:
        before = conn.execute(
            text("SELECT id, parent_id FROM messages ORDER BY id")
        ).fetchall()
        conn.execute(
            text(
                """
                UPDATE messages m
                SET parent_id = (
                    SELECT prev.id FROM messages prev
                    WHERE prev.conversation_id = m.conversation_id
                      AND (prev.created_at, prev.id) < (m.created_at, m.id)
                    ORDER BY prev.created_at DESC, prev.id DESC LIMIT 1
                )
                WHERE m.parent_id IS NULL
                  AND EXISTS (
                    SELECT 1 FROM messages prev
                    WHERE prev.conversation_id = m.conversation_id
                      AND (prev.created_at, prev.id) < (m.created_at, m.id)
                  )
                """
            )
        )
        after = conn.execute(
            text("SELECT id, parent_id FROM messages ORDER BY id")
        ).fetchall()
        assert before == after
    engine.dispose()


def test_backfill_second_run_leaves_branched_parent_ids(throwaway_db):
    """C2: second backfill over already-branched data must not rewrite parent_id.

    Shape mirrors the observed bug: root + child + legitimate NULL-root sibling
    ``[(6,None),(7,6),(8,None)]`` must stay byte-identical across a re-run.
    """
    import importlib.util

    from alembic.operations import Operations
    from alembic.runtime.migration import MigrationContext

    spec = importlib.util.spec_from_file_location(
        "r1_0043_messages_parent_id",
        CSP_ROOT / "migrations/versions/r1_0043_messages_parent_id.py",
    )
    mig = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mig)

    _run_alembic(throwaway_db, "upgrade", _REVISION)
    engine = create_engine(throwaway_db)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO users (username, hashed_password, role, is_active)"
                " VALUES ('w23-branch', 'x', 'user', TRUE)"
            )
        )
        uid = conn.execute(
            text("SELECT id FROM users WHERE username='w23-branch'")
        ).scalar_one()
        conn.execute(
            text(
                "INSERT INTO conversations (user_id, title) VALUES (:u, 'branched')"
            ),
            {"u": uid},
        )
        cid = conn.execute(
            text("SELECT id FROM conversations WHERE title='branched'")
        ).scalar_one()
        for i, body in enumerate(["m0", "m1", "m2"], start=1):
            conn.execute(
                text(
                    "INSERT INTO messages (conversation_id, role, content, parent_id, created_at)"
                    " VALUES (:c, 'user', :body, NULL,"
                    " TIMESTAMP '2026-07-27 00:00:00' + (:i || ' seconds')::interval)"
                ),
                {"c": cid, "body": body, "i": i},
            )
        ids = [
            r[0]
            for r in conn.execute(
                text(
                    "SELECT id FROM messages WHERE conversation_id=:c ORDER BY id"
                ),
                {"c": cid},
            ).fetchall()
        ]
        conn.execute(
            text("UPDATE messages SET parent_id = :p WHERE id = :id"),
            {"p": ids[0], "id": ids[1]},
        )
        # Drop completion marker so _backfill_parent_ids re-enters and must
        # rely on the per-conversation guard (not the marker alone).
        conn.execute(text("DROP TABLE IF EXISTS anila_r1_0043_parent_backfill_done"))

    with engine.connect() as conn:
        before = conn.execute(
            text(
                "SELECT id, parent_id FROM messages"
                " WHERE conversation_id = :c ORDER BY id"
            ),
            {"c": cid},
        ).fetchall()
        assert [(r.id, r.parent_id) for r in before] == [
            (ids[0], None),
            (ids[1], ids[0]),
            (ids[2], None),
        ]

    with engine.begin() as conn:
        mc = MigrationContext.configure(conn)
        with Operations.context(mc):
            # ``column_created=True`` = pretend this IS the column-creating run,
            # so the primary guard is bypassed and the per-conversation filter
            # is the thing actually under test (otherwise the call is vacuous).
            mig._backfill_parent_ids(column_created=True)

    with engine.connect() as conn:
        after = conn.execute(
            text(
                "SELECT id, parent_id FROM messages"
                " WHERE conversation_id = :c ORDER BY id"
            ),
            {"c": cid},
        ).fetchall()
        assert before == after
        assert [(r.id, r.parent_id) for r in after] == [
            (ids[0], None),
            (ids[1], ids[0]),
            (ids[2], None),
        ]
    engine.dispose()


def test_replay_upgrade_leaves_all_null_root_siblings_untouched(throwaway_db):
    """C2b: replaying ``upgrade`` on a DB that already has ``parent_id`` never writes.

    Shape: a conversation whose messages are **all** NULL-parent. That is
    reachable through the ordinary API —— editing the very first user message
    before any assistant reply yields two legitimate NULL roots
    ``[(1,None),(2,None)]``. The per-conversation ``HAVING`` filter cannot tell
    it apart from un-backfilled linear history, so the only sound guard is
    "backfill only in the run that CREATES the column".

    The completion marker is dropped first —— that is the state of every
    database upgraded before the marker existed, and the state the reviewer's
    probe reproduced as ``CHANGED conv=2 msg=5 parent None -> 4``.
    """
    pred = _predecessor(throwaway_db)
    _run_alembic(throwaway_db, "upgrade", _REVISION)
    engine = create_engine(throwaway_db)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO users (username, hashed_password, role, is_active)"
                " VALUES ('w23-allnull', 'x', 'user', TRUE)"
            )
        )
        uid = conn.execute(
            text("SELECT id FROM users WHERE username='w23-allnull'")
        ).scalar_one()
        conn.execute(
            text("INSERT INTO conversations (user_id, title) VALUES (:u, 'all-null')"),
            {"u": uid},
        )
        cid = conn.execute(
            text("SELECT id FROM conversations WHERE title='all-null'")
        ).scalar_one()
        # Three legitimate NULL roots (edit-first-message siblings). No row in
        # this conversation carries a non-NULL parent_id.
        for i, body in enumerate(["root-a", "root-b", "root-c"], start=1):
            conn.execute(
                text(
                    "INSERT INTO messages (conversation_id, role, content, parent_id,"
                    " created_at) VALUES (:c, 'user', :body, NULL,"
                    " TIMESTAMP '2026-07-27 00:00:00' + (:i || ' seconds')::interval)"
                ),
                {"c": cid, "body": body, "i": i},
            )
        # Marker absent = any database upgraded before the guard existed.
        conn.execute(text("DROP TABLE IF EXISTS anila_r1_0043_parent_backfill_done"))

    with engine.connect() as conn:
        before = conn.execute(
            text("SELECT id, parent_id FROM messages ORDER BY id")
        ).fetchall()
        assert [r.parent_id for r in before] == [None, None, None]
    engine.dispose()

    # Replay upgrade() with the column already present (alembic version row
    # rewound; no schema touched). This is the "upgraded before the guard"
    # rerun —— it must not write a single parent_id.
    _run_alembic(throwaway_db, "stamp", pred)
    _run_alembic(throwaway_db, "upgrade", _REVISION)

    engine = create_engine(throwaway_db)
    with engine.connect() as conn:
        after = conn.execute(
            text("SELECT id, parent_id FROM messages ORDER BY id")
        ).fetchall()
    engine.dispose()
    assert after == before, f"replay mislinked rows: {before} -> {after}"


def _seed_user_conv_messages(url: str, n: int = 5):
    """Upgrade to head, seed user + n-message linear conversation. Returns ids."""
    _run_alembic(url, "upgrade", _REVISION)
    engine = create_engine(url)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    # Import after upgrade so ORM matches schema.
    from app.models.conversation import Conversation
    from app.models.message import Message
    from app.models.user import User
    from app.utils.security import hash_password

    db = Session()
    try:
        user = User(
            username=f"w23-{uuid.uuid4().hex[:8]}",
            hashed_password=hash_password("x"),
            role="user",
            is_active=True,
            is_approved=True,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        conv = Conversation(user_id=user.id, title="edit-tree")
        db.add(conv)
        db.commit()
        db.refresh(conv)
        prev = None
        msg_ids = []
        for i in range(n):
            role = "user" if i % 2 == 0 else "assistant"
            m = Message(
                conversation_id=conv.id,
                parent_id=prev,
                role=role,
                content=f"msg-{i}",
            )
            db.add(m)
            db.flush()
            msg_ids.append(m.id)
            prev = m.id
        conv.active_leaf_message_id = msg_ids[-1]
        db.commit()
        return engine, Session, user.id, conv.id, msg_ids
    finally:
        db.close()


def test_edit_creates_sibling_preserves_branch_and_audits(throwaway_db):
    """A2: edit message #3 of 5 → old branch kept, sibling + audit row."""
    engine, Session, user_id, conv_id, msg_ids = _seed_user_conv_messages(
        throwaway_db, n=5
    )
    from app.models.audit_log import AuditLog
    from app.models.conversation import Conversation
    from app.models.message import Message
    from app.models.user import User
    from app.services import conversation_service as svc

    db = Session()
    try:
        user = db.get(User, user_id)
        # Message index 2 is the 3rd message (1-based #3).
        target = msg_ids[2]
        before_count = db.query(Message).filter(Message.conversation_id == conv_id).count()
        assert before_count == 5

        sibling = svc.edit_user_message(db, conv_id, target, user, "edited-#3")
        db.refresh(db.get(Conversation, conv_id))

        after = (
            db.query(Message)
            .filter(Message.conversation_id == conv_id)
            .order_by(Message.id)
            .all()
        )
        assert len(after) == 6  # no truncation
        assert sibling.id not in msg_ids
        assert sibling.content == "edited-#3"
        assert sibling.parent_id == db.get(Message, target).parent_id
        # Old subtree still present (messages after #3).
        for mid in msg_ids:
            assert db.get(Message, mid) is not None
        conv = db.get(Conversation, conv_id)
        assert conv.active_leaf_message_id == sibling.id

        audits = (
            db.query(AuditLog)
            .filter(AuditLog.action == "edit_user_message")
            .all()
        )
        assert len(audits) >= 1
        assert str(sibling.id) in {a.resource_id for a in audits}
    finally:
        db.close()
        engine.dispose()


def test_legal_hold_edit_rejected(throwaway_db):
    """A2: legal_hold conversation edit → 4xx."""
    from fastapi import HTTPException

    engine, Session, user_id, conv_id, msg_ids = _seed_user_conv_messages(
        throwaway_db, n=3
    )
    from app.models.conversation import Conversation
    from app.models.user import User
    from app.services import conversation_service as svc

    db = Session()
    try:
        user = db.get(User, user_id)
        conv = db.get(Conversation, conv_id)
        conv.legal_hold = True
        db.commit()
        db.refresh(conv)

        with pytest.raises(HTTPException) as ei:
            svc.edit_user_message(db, conv_id, msg_ids[0], user, "nope")
        assert ei.value.status_code == 403
        assert "法律保全" in str(ei.value.detail)
    finally:
        db.close()
        engine.dispose()


def test_active_path_500_messages_no_n_plus_one(throwaway_db):
    """A performance guard: 500-message path resolves without per-message queries."""
    engine, Session, user_id, conv_id, msg_ids = _seed_user_conv_messages(
        throwaway_db, n=500
    )
    from app.models.conversation import Conversation
    from app.services import conversation_service as svc
    from sqlalchemy import event

    db = Session()
    try:
        conv = db.get(Conversation, conv_id)
        statements: list[str] = []

        def _count(conn, cursor, statement, parameters, context, executemany):
            statements.append(statement)

        event.listen(engine, "before_cursor_execute", _count)
        t0 = time.perf_counter()
        path = svc.resolve_active_path(db, conv)
        elapsed = time.perf_counter() - t0
        event.remove(engine, "before_cursor_execute", _count)

        assert len(path) == 500
        # Recursive CTE + one IN fetch (or equivalent) — not 500+ selects.
        assert len(statements) <= 5, f"too many statements: {len(statements)}"
        assert elapsed < 2.0, f"active-path too slow: {elapsed:.3f}s"
    finally:
        db.close()
        engine.dispose()


def test_explicit_parent_recovers_sibling_after_moved_active_leaf(throwaway_db):
    """reviewer 探針的資料庫版:regenerate 失敗 → 重試 → 再 regenerate。

    樹:u1 → a2。regenerate 串流失敗(什麼都沒寫),所以 active leaf 還停在 a2。

    ① 重試指名 parent_id=u1 → a3 是 a2 的**兄弟**,不是子節點;
       (不指名的話 `append_message` 會掛到 active leaf,也就是 a2 底下)
    ② 再 regenerate 從 a3 fork → a4 仍在同一層。
    """
    _run_alembic(throwaway_db, "upgrade", _REVISION)
    engine = create_engine(throwaway_db)
    Session = sessionmaker(bind=engine, expire_on_commit=False)

    from app.models.conversation import Conversation
    from app.models.message import Message
    from app.models.user import User
    from app.services import conversation_service as svc
    from app.utils.security import hash_password

    db = Session()
    try:
        user = User(
            username=f"w23-{uuid.uuid4().hex[:8]}",
            hashed_password=hash_password("x"),
            role="user",
            is_active=True,
            is_approved=True,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        conv = Conversation(user_id=user.id, title="retry-parent")
        db.add(conv)
        db.commit()
        db.refresh(conv)

        u1 = svc.append_message(db, conv.id, user, role="user", content="問題")
        a2 = svc.append_message(db, conv.id, user, role="assistant", content="答案一")
        assert a2.parent_id == u1.id
        db.refresh(conv)
        assert conv.active_leaf_message_id == a2.id

        # ── 對照組:同樣的情境不指名父節點 —— 缺陷就長在這裡。
        implicit = svc.append_message(
            db, conv.id, user, role="assistant", content="隱式的復原"
        )
        assert implicit.parent_id == a2.id, "隱式 append 掛到 active leaf(= 缺陷本體)"

        # 把 active leaf 撥回 a2,重現「regenerate 失敗後什麼都沒動」的狀態。
        conv.active_leaf_message_id = a2.id
        db.commit()

        # ① 指名父節點的重試 → 兄弟。
        a3 = svc.append_message(
            db, conv.id, user, role="assistant", content="復原的答案", parent_id=u1.id
        )
        assert a3.parent_id == u1.id
        assert a3.parent_id == a2.parent_id
        assert a3.parent_id != a2.id

        # ② 從 a3 再 regenerate(fork)→ 仍在同一層。
        a4 = svc.fork_assistant_message(db, conv.id, a3.id, user, content="第三個版本")
        assert a4.parent_id == u1.id

        siblings = sorted(
            m.id
            for m in db.query(Message)
            .filter(Message.conversation_id == conv.id, Message.parent_id == u1.id)
            .all()
        )
        assert siblings == sorted([a2.id, a3.id, a4.id])
    finally:
        db.close()
        engine.dispose()


def test_explicit_parent_must_belong_to_the_conversation(throwaway_db):
    """跨對話的 parent_id → 400(不能拿別的樹當錨點)。"""
    from fastapi import HTTPException

    engine, Session, user_id, conv_id, msg_ids = _seed_user_conv_messages(
        throwaway_db, n=3
    )
    from app.models.conversation import Conversation
    from app.models.user import User
    from app.services import conversation_service as svc

    db = Session()
    try:
        user = db.get(User, user_id)
        other = Conversation(user_id=user_id, title="other")
        db.add(other)
        db.commit()
        db.refresh(other)

        with pytest.raises(HTTPException) as ei:
            svc.append_message(
                db, other.id, user, role="user", content="x", parent_id=msg_ids[0]
            )
        assert ei.value.status_code == 400
        assert "parent_id" in str(ei.value.detail)
    finally:
        db.close()
        engine.dispose()
