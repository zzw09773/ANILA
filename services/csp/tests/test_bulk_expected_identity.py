"""同一瀏覽器換成另一個帳號後，舊畫面的批次不能動到新帳號。

第二個分頁登入之後，請求帶的是新 cookie，但批次開始時畫面上的
user.id 仍放在 X-ANILA-Expected-User-ID。對不上就 409，而且對話、
資料夾、摘要、墓碑都保持原樣。沒有這個 header 的客戶端維持原行為。
這裡只測正常的身分切換，不測注入。
"""
from __future__ import annotations

import pytest

from app.middleware.cookies import CSRF_COOKIE_NAME
from app.models.conversation import Conversation, ConversationUserMeta
from tests.conftest import login, make_user
from tests.test_memory_review_fixes import _conv, _pair
from tests.test_memory_summary_bulk import (
    _save_summary,
    _summary_ids,
    _tombstones,
)

_HEADER = "X-ANILA-Expected-User-ID"
_MISMATCH = "登入帳號已變更，請重新整理後再操作"
_BULK = "/api/memory/summaries/bulk-delete"
_CLEAR = "/api/memory/summaries"


def _reload(db):
    db.expire_all()


def _seed(db, user, title: str):
    conv = _conv(db, user, title)
    _, assistant = _pair(db, conv, f"{title} 的問題", "好。")
    summary = _save_summary(db, user, conv, f"{title} 的摘要", assistant.id)
    db.add(
        ConversationUserMeta(
            user_id=user.id,
            conversation_id=conv.id,
            starred=False,
            folder="kept",
            user_tags=[],
        )
    )
    db.commit()
    db.refresh(conv)
    db.refresh(summary)
    return conv, summary


def _snapshot(db, user_id: int) -> dict:
    _reload(db)
    conversations = (
        db.query(Conversation.id, Conversation.title)
        .filter(Conversation.user_id == user_id)
        .order_by(Conversation.id)
        .all()
    )
    folders = (
        db.query(ConversationUserMeta.conversation_id, ConversationUserMeta.folder)
        .filter(ConversationUserMeta.user_id == user_id)
        .order_by(ConversationUserMeta.conversation_id)
        .all()
    )
    return {
        "conversations": [(int(row[0]), row[1]) for row in conversations],
        "folders": [(int(row[0]), row[1]) for row in folders],
        "summaries": _summary_ids(db, user_id),
        "tombstones": [
            (int(stone.conversation_id), stone.kind) for stone in _tombstones(db, user_id)
        ],
    }


def _cookie_headers(client, expected: int) -> dict[str, str]:
    return {
        "X-CSRF-Token": client.cookies.get(CSRF_COOKIE_NAME),
        _HEADER: str(expected),
    }


def _bearer(token: str, expected: str | None = None) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {token}"}
    if expected is not None:
        headers[_HEADER] = expected
    return headers


def _call(client, kind: str, headers: dict, *, conv_id: int, summary_id: int, folder: str):
    if kind == "delete-conversation":
        return client.delete(f"/api/conversations/{conv_id}", headers=headers)
    if kind == "put-folder":
        return client.put(
            f"/api/conversations/{conv_id}",
            headers=headers,
            json={"folder": folder},
        )
    if kind == "bulk-delete":
        return client.post(_BULK, headers=headers, json={"ids": [summary_id]})
    if kind == "clear-summaries":
        return client.delete(_CLEAR, headers=headers)
    raise AssertionError(kind)


def _switch_to_second_login(client, first: str, second: str) -> None:
    """同一個 client 先登入畫面帳號，再登入現在的帳號，cookie 換成後者。"""
    login(client, first)
    login(client, second)
    me = client.get("/api/auth/me")
    assert me.status_code == 200, me.text
    assert me.json()["username"] == second


@pytest.mark.parametrize(
    "kind",
    ["delete-conversation", "put-folder", "bulk-delete", "clear-summaries"],
)
def test_mismatched_expected_user_is_409_and_changes_nothing(client, db, kind):
    """畫面仍是第一個帳號，cookie 已是第二個。四條批次都不寫入。"""
    screen = make_user(db, username=f"expect-screen-{kind}")
    current = make_user(db, username=f"expect-current-{kind}")
    screen_conv, screen_summary = _seed(db, screen, "畫面")
    current_conv, current_summary = _seed(db, current, "目前")
    _switch_to_second_login(client, screen.username, current.username)
    before = {
        "screen": _snapshot(db, screen.id),
        "current": _snapshot(db, current.id),
    }

    response = _call(
        client,
        kind,
        _cookie_headers(client, screen.id),
        conv_id=screen_conv.id,
        summary_id=screen_summary.id,
        folder="moved",
    )

    assert response.status_code == 409, response.text
    assert response.json()["detail"] == _MISMATCH
    _reload(db)
    assert _snapshot(db, screen.id) == before["screen"]
    assert _snapshot(db, current.id) == before["current"]
    assert current_conv.id and current_summary.id


def test_matched_header_stays_inside_the_logged_in_user(client, db):
    """header 與登入相同時，四條都成功，而且只動這位使用者。"""
    screen = make_user(db, username="expect-match-other")
    current = make_user(db, username="expect-match-self")
    screen_conv, screen_summary = _seed(db, screen, "別人")
    current_conv, current_summary = _seed(db, current, "自己")
    extra_conv, extra_summary = _seed(db, current, "自己另一則")
    screen_conv_id = int(screen_conv.id)
    screen_summary_id = int(screen_summary.id)
    current_conv_id = int(current_conv.id)
    current_summary_id = int(current_summary.id)
    extra_conv_id = int(extra_conv.id)
    extra_summary_id = int(extra_summary.id)
    token = login(client, current.username)
    client.cookies.clear()
    headers = _bearer(token, str(current.id))

    foreign_delete = _call(
        client,
        "delete-conversation",
        headers,
        conv_id=screen_conv_id,
        summary_id=screen_summary_id,
        folder="moved",
    )
    foreign_folder = _call(
        client,
        "put-folder",
        headers,
        conv_id=screen_conv_id,
        summary_id=screen_summary_id,
        folder="moved",
    )
    foreign_bulk = _call(
        client,
        "bulk-delete",
        headers,
        conv_id=screen_conv_id,
        summary_id=screen_summary_id,
        folder="moved",
    )
    assert foreign_delete.status_code == 404, foreign_delete.text
    assert foreign_folder.status_code == 404, foreign_folder.text
    assert foreign_bulk.status_code == 404, foreign_bulk.text
    _reload(db)
    assert _snapshot(db, screen.id)["conversations"]
    assert _snapshot(db, screen.id)["summaries"] == [screen_summary_id]
    assert _snapshot(db, screen.id)["tombstones"] == []

    moved = _call(
        client,
        "put-folder",
        headers,
        conv_id=current_conv_id,
        summary_id=current_summary_id,
        folder="filed",
    )
    assert moved.status_code == 200, moved.text
    assert moved.json()["folder"] == "filed"
    removed = _call(
        client,
        "bulk-delete",
        headers,
        conv_id=current_conv_id,
        summary_id=current_summary_id,
        folder="filed",
    )
    assert removed.status_code == 200, removed.text
    assert removed.json() == {"deleted": 1}
    cleared = _call(
        client,
        "clear-summaries",
        headers,
        conv_id=current_conv_id,
        summary_id=extra_summary_id,
        folder="filed",
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json() == {"deleted": 1}
    deleted = _call(
        client,
        "delete-conversation",
        headers,
        conv_id=current_conv_id,
        summary_id=current_summary_id,
        folder="filed",
    )
    assert deleted.status_code == 204, deleted.text

    _reload(db)
    own = _snapshot(db, current.id)
    other = _snapshot(db, screen.id)
    assert own["conversations"] == [(extra_conv_id, "自己另一則")]
    assert own["folders"] == [(extra_conv_id, "kept")]
    assert own["summaries"] == []
    assert {conversation_id for conversation_id, kind in own["tombstones"]} == {
        current_conv_id,
        extra_conv_id,
    }
    assert all(kind == "summary" for _, kind in own["tombstones"])
    assert other["conversations"] == [(screen_conv_id, "別人")]
    assert other["folders"] == [(screen_conv_id, "kept")]
    assert other["summaries"] == [screen_summary_id]
    assert other["tombstones"] == []


def test_missing_header_keeps_the_existing_owner_scope(client, db):
    """沒有 header 時，舊客戶端仍只動目前登入者。"""
    other = make_user(db, username="expect-plain-other")
    owner = make_user(db, username="expect-plain-owner")
    other_conv, other_summary = _seed(db, other, "別人")
    own_conv, own_summary = _seed(db, owner, "自己")
    extra_conv, extra_summary = _seed(db, owner, "自己另一則")
    token = login(client, owner.username)
    client.cookies.clear()
    headers = _bearer(token)

    moved = _call(
        client,
        "put-folder",
        headers,
        conv_id=own_conv.id,
        summary_id=own_summary.id,
        folder="filed",
    )
    assert moved.status_code == 200, moved.text
    assert moved.json()["folder"] == "filed"
    removed = _call(
        client,
        "bulk-delete",
        headers,
        conv_id=own_conv.id,
        summary_id=own_summary.id,
        folder="filed",
    )
    assert removed.status_code == 200, removed.text
    assert removed.json() == {"deleted": 1}
    cleared = _call(
        client,
        "clear-summaries",
        headers,
        conv_id=own_conv.id,
        summary_id=extra_summary.id,
        folder="filed",
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json() == {"deleted": 1}
    deleted = _call(
        client,
        "delete-conversation",
        headers,
        conv_id=own_conv.id,
        summary_id=own_summary.id,
        folder="filed",
    )
    assert deleted.status_code == 204, deleted.text

    _reload(db)
    assert _snapshot(db, other.id) == {
        "conversations": [(int(other_conv.id), "別人")],
        "folders": [(int(other_conv.id), "kept")],
        "summaries": [int(other_summary.id)],
        "tombstones": [],
    }
    own = _snapshot(db, owner.id)
    assert own["conversations"] == [(int(extra_conv.id), "自己另一則")]
    assert own["summaries"] == []
    assert len(own["tombstones"]) == 2


@pytest.mark.parametrize("raw", ["0", "-1", "abc"])
def test_illegal_expected_user_header_is_422_and_changes_nothing(client, db, raw):
    owner = make_user(db, username=f"expect-bad-{raw}".replace("-", "m"))
    conv, summary = _seed(db, owner, "自己")
    token = login(client, owner.username)
    client.cookies.clear()
    headers = _bearer(token, raw)
    before = _snapshot(db, owner.id)

    for kind in ("delete-conversation", "put-folder", "bulk-delete", "clear-summaries"):
        response = _call(
            client,
            kind,
            headers,
            conv_id=conv.id,
            summary_id=summary.id,
            folder="moved",
        )
        assert response.status_code == 422, (kind, raw, response.text)
        _reload(db)
        assert _snapshot(db, owner.id) == before
