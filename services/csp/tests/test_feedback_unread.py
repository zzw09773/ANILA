"""新的使用者回饋要通知當下這位管理員；已讀各看各的，不連坐。

橫幅讀的是既有警報摘要上的 unread_feedback_count（同一支請求）。
只有「全部標為已讀」會推進水位，而且只推到這一頁實際載到的最新 rated_at。
清單預設 7 天、100 列，未讀筆數不是；打開頁面本身不標已讀。
評分沒有獨立時間戳的舊列不算新回饋，避免上線當下把歷史整批喊出來。
"""

from __future__ import annotations

import os

os.environ.setdefault("ANILA_ALLOW_DEV_SECRET", "1")

import threading
from datetime import datetime, timedelta, timezone

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.orm import Session as OrmSession

from app.database import Base
from app.models.conversation import Conversation
from app.models.feedback_read import FeedbackReadState
from app.models.message import Message
from app.services.auth_service import create_tokens
from app.services.feedback_notice import mark_feedback_read
from tests.conftest import make_user
from tests.test_conversation_compact import CSP_ROOT

SUMMARY_URL = "/api/alerts/summary"
MARK_READ_URL = "/api/admin/feedback/read"
FEEDBACK_URL = "/api/admin/feedback"


def _bearer(user) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_tokens(user)['access_token']}"}


def _seed_assistant(db: Session, owner, *, content: str = "assistant reply") -> tuple[Conversation, Message]:
    conv = Conversation(user_id=owner.id, title="unread-seed")
    db.add(conv)
    db.flush()
    msg = Message(
        conversation_id=conv.id,
        role="assistant",
        content=content,
        created_at=datetime.now(timezone.utc),
    )
    db.add(msg)
    db.commit()
    db.refresh(msg)
    return conv, msg


def _rate(client, user, conv_id: int, message_id: int, body: dict):
    return client.put(
        f"/api/conversations/{conv_id}/messages/{message_id}/rating",
        headers=_bearer(user),
        json=body,
    )


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _mark(client, viewer, read_at: datetime):
    return client.post(
        MARK_READ_URL,
        headers=_bearer(viewer),
        json={"read_at": _aware(read_at).isoformat()},
    )


def _unread(client, viewer) -> int:
    resp = client.get(SUMMARY_URL, headers=_bearer(viewer))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "答非所問" not in resp.text
    assert "CLASSIFIED-BODY" not in resp.text
    return body["unread_feedback_count"]


def test_new_rating_is_unread_for_each_admin_until_that_admin_marks_it(client, db: Session):
    """差評（含原因）與好評都算一筆新回饋；甲標已讀不會幫乙清掉。"""
    user = make_user(db, username="unread-rater", role="user")
    admin = make_user(db, username="unread-admin", role="admin")
    owner = make_user(db, username="unread-owner", role="owner")
    down_conv, down_msg = _seed_assistant(db, user, content="CLASSIFIED-BODY-MUST-NOT-LEAK")
    up_conv, up_msg = _seed_assistant(db, user)

    down = _rate(
        client,
        user,
        down_conv.id,
        down_msg.id,
        {"rating": "down", "rating_score": 2, "comment": "答非所問", "reasons": ["離題"]},
    )
    assert down.status_code == 200, down.text
    up = _rate(client, user, up_conv.id, up_msg.id, {"rating": "up", "rating_score": 9})
    assert up.status_code == 200, up.text

    assert _unread(client, admin) == 2
    assert _unread(client, owner) == 2

    listed = client.get(FEEDBACK_URL, headers=_bearer(admin))
    assert listed.status_code == 200, listed.text
    assert _unread(client, admin) == 2

    db.refresh(down_msg)
    db.refresh(up_msg)
    newest = max(_aware(down_msg.rated_at), _aware(up_msg.rated_at))
    marked = _mark(client, admin, newest)
    assert marked.status_code == 200, marked.text
    assert marked.json()["count"] == 0
    assert _unread(client, admin) == 0
    assert _unread(client, owner) == 2


def test_rating_again_after_read_counts_as_new_and_clearing_removes_it(client, db: Session):
    """已讀之後又改留言要再通知；把評分清掉就不再算。"""
    user = make_user(db, username="unread-again-user", role="user")
    admin = make_user(db, username="unread-again-admin", role="admin")
    conv, msg = _seed_assistant(db, user)

    first = _rate(client, user, conv.id, msg.id, {"rating": "down", "reasons": ["不正確"]})
    assert first.status_code == 200, first.text
    assert _unread(client, admin) == 1

    db.refresh(msg)
    marked = _mark(client, admin, msg.rated_at)
    assert marked.status_code == 200, marked.text
    assert _unread(client, admin) == 0

    revised = _rate(
        client,
        user,
        conv.id,
        msg.id,
        {"rating": "down", "comment": "補充：數字是錯的"},
    )
    assert revised.status_code == 200, revised.text
    assert _unread(client, admin) == 1

    cleared = _rate(client, user, conv.id, msg.id, {"rating": None})
    assert cleared.status_code == 200, cleared.text
    assert _unread(client, admin) == 0


def test_historical_rating_without_a_timestamp_is_not_announced(client, db: Session):
    """遷移前就在的評分沒有 rated_at，不該在上線當下變成一堆新回饋。"""
    user = make_user(db, username="unread-old-user", role="user")
    admin = make_user(db, username="unread-old-admin", role="admin")
    conv = Conversation(user_id=user.id, title="old-rating")
    db.add(conv)
    db.flush()
    db.add(Message(
        conversation_id=conv.id,
        role="assistant",
        content="old reply",
        rating="down",
        created_at=datetime.now(timezone.utc),
    ))
    db.commit()

    assert _unread(client, admin) == 0


def test_mark_read_stops_at_the_newest_row_the_page_loaded(client, db: Session):
    """7 天窗外、但評分時間更晚的回饋，不能被這一頁的「全部標為已讀」清掉。

    再送一次更早的時間，水位也不能倒退。
    """
    user = make_user(db, username="unread-window-user", role="user")
    admin = make_user(db, username="unread-window-admin", role="admin")
    now = datetime.now(timezone.utc)
    conv = Conversation(user_id=user.id, title="window")
    db.add(conv)
    db.flush()
    on_page = Message(
        conversation_id=conv.id,
        role="assistant",
        content="this week",
        rating="down",
        created_at=now - timedelta(hours=1),
        rated_at=now - timedelta(hours=2),
    )
    off_page = Message(
        conversation_id=conv.id,
        role="assistant",
        content="older message, newer rating",
        rating="down",
        created_at=now - timedelta(days=30),
        rated_at=now,
    )
    db.add_all([on_page, off_page])
    db.commit()

    assert _unread(client, admin) == 2
    listed = client.get(FEEDBACK_URL, headers=_bearer(admin))
    assert listed.status_code == 200, listed.text
    items = listed.json()["items"]
    assert [item["message_id"] for item in items] == [on_page.id]
    loaded_at = items[0]["rated_at"]
    assert loaded_at

    marked = client.post(
        MARK_READ_URL,
        headers=_bearer(admin),
        json={"read_at": loaded_at},
    )
    assert marked.status_code == 200, marked.text
    assert marked.json()["count"] == 1
    assert _unread(client, admin) == 1

    caught_up = _mark(client, admin, off_page.rated_at)
    assert caught_up.status_code == 200, caught_up.text
    assert _unread(client, admin) == 0

    rewind = client.post(
        MARK_READ_URL,
        headers=_bearer(admin),
        json={"read_at": loaded_at},
    )
    assert rewind.status_code == 200, rewind.text
    assert rewind.json()["count"] == 0
    assert _unread(client, admin) == 0


def test_missing_read_at_is_rejected(client, db: Session):
    admin = make_user(db, username="unread-nobody", role="admin")
    resp = client.post(MARK_READ_URL, headers=_bearer(admin), json={})
    assert resp.status_code == 422, resp.text


def test_first_read_state_upsert_keeps_the_later_timestamp(tmp_path, monkeypatch):
    """兩個人同時第一次標已讀，不能撞主鍵，水位留比較晚的那個。"""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'read-race.db'}",
        connect_args={"check_same_thread": False, "timeout": 5},
    )
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    setup = SessionLocal()
    admin = make_user(setup, username="unread-race-admin", role="admin")
    admin_id = admin.id
    setup.close()

    earlier = datetime(2026, 9, 1, 8, 0, tzinfo=timezone.utc)
    later = datetime(2026, 9, 28, 8, 0, tzinfo=timezone.utc)
    real_get = OrmSession.get
    seen_missing = {"n": 0}
    both_missing = threading.Event()

    def racing_get(self, entity, ident, **kwargs):
        result = real_get(self, entity, ident, **kwargs)
        if entity is FeedbackReadState and result is None:
            seen_missing["n"] += 1
            if seen_missing["n"] >= 2:
                both_missing.set()
            both_missing.wait(timeout=5)
        return result

    monkeypatch.setattr(OrmSession, "get", racing_get)

    errors: list[BaseException] = []

    def run(read_at: datetime) -> None:
        session = SessionLocal()
        try:
            user = session.get(type(admin), admin_id)
            mark_feedback_read(session, user, read_at)
            session.commit()
        except BaseException as exc:
            errors.append(exc)
            session.rollback()
        finally:
            session.close()

    threads = [
        threading.Thread(target=run, args=(earlier,)),
        threading.Thread(target=run, args=(later,)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert [thread.is_alive() for thread in threads] == [False, False]
    assert errors == []
    check = SessionLocal()
    try:
        row = check.get(FeedbackReadState, admin_id)
        assert row is not None
        assert _aware(row.read_at) == later
    finally:
        check.close()
        engine.dispose()


def test_mark_read_requires_an_admin(client, db: Session):
    user = make_user(db, username="unread-plain", role="user")
    developer = make_user(db, username="unread-dev", role="developer")

    assert client.post(MARK_READ_URL).status_code == 401
    assert client.get(SUMMARY_URL).status_code == 401
    assert client.post(MARK_READ_URL, headers=_bearer(user)).status_code == 403
    assert client.post(MARK_READ_URL, headers=_bearer(developer)).status_code == 403
    assert client.get(SUMMARY_URL, headers=_bearer(user)).status_code == 403


def test_feedback_read_migration_follows_r1_0059():
    path = CSP_ROOT / "migrations" / "versions" / "r1_0060_feedback_read_state.py"
    text = path.read_text(encoding="utf-8")
    assert 'revision: str = "r1_0060"' in text
    assert 'down_revision: Union[str, None] = "r1_0059"' in text
    assert "feedback_read_states" in text
    assert "rated_at" in text
    assert "UPDATE messages" not in text

    cfg = Config(str(CSP_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(CSP_ROOT / "migrations"))
    heads = list(ScriptDirectory.from_config(cfg).get_heads())
    assert heads == ["r1_0060"], heads
