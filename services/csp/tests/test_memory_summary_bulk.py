"""對話摘要批次刪除與全部刪除。

靜態路徑必須先於 ``/summaries/{summary_id}``。批次要嘛整批刪除，
要嘛一筆都不動，而且每一筆都走既有的墓碑。
"""
from __future__ import annotations

import pytest

from app.middleware.cookies import CSRF_COOKIE_NAME
from app.models.conversation import Conversation
from app.models.message import Message
from app.models.user_memory import (
    ConversationMemoryChunk,
    ConversationSummary,
    MemoryTombstone,
    UserFact,
)
from app.services import memory_service
from app.services.auth_service import create_tokens
from app.services.memory_service import REPLY_STYLE_KEY
from tests.conftest import login, make_user
from tests.test_memory_review_fixes import (
    _Scripted,
    _conv,
    _install_model,
    _pair,
    _summary_role,
)

_BULK = "/api/memory/summaries/bulk-delete"
_CLEAR = "/api/memory/summaries"
_MISSING = {"detail": "摘要不存在"}
_KEPT_SUMMARY = "這則摘要要留著"


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _save_summary(db, user, conv, text: str, covered: int | None):
    row = memory_service.save_conversation_summary(
        db,
        user_id=user.id,
        conversation_id=conv.id,
        summary=text,
        covered_message_id=covered,
        embedding=None,
        source_model=None,
        native_dim=None,
        is_encrypted=False,
    )
    db.commit()
    db.refresh(row)
    return row


def _add_fact(db, fact_id: int, user, conv, key: str, value: str):
    db.add(
        UserFact(
            id=fact_id,
            user_id=user.id,
            key=key,
            value=value,
            confidence=1,
            source_conversation_id=conv.id,
        )
    )


def _add_chunk(db, chunk_id: int, user, conv, content: str):
    db.add(
        ConversationMemoryChunk(
            id=chunk_id,
            user_id=user.id,
            conversation_id=conv.id,
            message_id=None,
            role="user",
            content=content,
            embedding="[0.1,0.2]",
            embedding_source_model="embed-test",
            embedding_native_dim=2,
            is_encrypted=False,
        )
    )


def _reload(db):
    db.expire_all()


def _summary_text(db, summary_id: int) -> str | None:
    """用欄位查詢。別的 session 刪掉列之後，db.get 會碰到已刪除的 identity map。"""
    return (
        db.query(ConversationSummary.summary)
        .filter(ConversationSummary.id == summary_id)
        .scalar()
    )


def _summary_ids(db, user_id: int) -> list[int]:
    rows = (
        db.query(ConversationSummary.id)
        .filter(ConversationSummary.user_id == user_id)
        .order_by(ConversationSummary.id)
        .all()
    )
    return [int(row[0]) for row in rows]


def _tombstones(db, user_id: int) -> list[MemoryTombstone]:
    return (
        db.query(MemoryTombstone)
        .filter(MemoryTombstone.user_id == user_id)
        .order_by(MemoryTombstone.id)
        .all()
    )


def test_static_summary_routes_precede_the_summary_id_path():
    """批次與全部刪除是靜態路徑，不能被 summary_id 先吃掉。"""
    from app.api.memory import router

    labeled: list[tuple[str, str]] = []
    for route in router.routes:
        path = getattr(route, "path", "") or ""
        for method in getattr(route, "methods", None) or ():
            labeled.append((method, path))
    bulk = [path for method, path in labeled if method == "POST" and path.endswith("/summaries/bulk-delete")]
    single = [
        path
        for method, path in labeled
        if method == "DELETE" and path.endswith("/summaries/{summary_id}")
    ]
    clear = [
        path
        for method, path in labeled
        if method == "DELETE" and path.rstrip("/").endswith("/summaries") and "{" not in path
    ]
    assert bulk == ["/api/memory/summaries/bulk-delete"], labeled
    assert clear == ["/api/memory/summaries"], labeled
    assert single == ["/api/memory/summaries/{summary_id}"], labeled
    flat = [f"{method} {path}" for method, path in labeled]
    assert flat.index("DELETE /api/memory/summaries") < flat.index(
        "DELETE /api/memory/summaries/{summary_id}"
    )
    assert not any(
        method == "POST" and "{summary_id}" in path for method, path in labeled
    )


def test_bulk_delete_counts_distinct_ids_and_keeps_other_memory(client, db):
    """重複 id 只算一次。沒點名的摘要、別人的資料、事實、片段、偏好與原對話都留著。"""
    owner = make_user(db, username="bulk-keep-owner")
    other = make_user(db, username="bulk-keep-other")
    token = login(client, "bulk-keep-owner")
    client.cookies.clear()
    headers = _bearer(token)

    own_a = _conv(db, owner, "own-a")
    _, asst_a = _pair(db, own_a, "我在雷達組，請用條列", "好的，之後用條列。")
    own_b = _conv(db, owner, "own-b")
    _, asst_b = _pair(db, own_b, "延續上次報告", "好。")
    own_kept = _conv(db, owner, "own-kept")
    _, asst_kept = _pair(db, own_kept, "這段不要刪", "好。")
    other_conv = _conv(db, other, "other")
    _, other_asst = _pair(db, other_conv, "別人的對話", "好。")

    summary_a = _save_summary(db, owner, own_a, "使用者在雷達組，希望條列。", asst_a.id)
    summary_b = _save_summary(db, owner, own_b, "使用者要延續上次報告。", asst_b.id)
    kept = _save_summary(db, owner, own_kept, _KEPT_SUMMARY, asst_kept.id)
    other_summary = _save_summary(db, other, other_conv, "別人的摘要要留著", other_asst.id)
    a_id = int(summary_a.id)
    b_id = int(summary_b.id)
    kept_id = int(kept.id)
    other_id = int(other_summary.id)
    own_a_id = int(own_a.id)
    own_b_id = int(own_b.id)
    asst_a_id = int(asst_a.id)
    asst_b_id = int(asst_b.id)
    _add_fact(db, 7101, owner, own_a, "unit", "雷達組")
    _add_fact(db, 7102, owner, own_a, REPLY_STYLE_KEY, "請用條列")
    _add_chunk(db, 7103, owner, own_a, "原始片段要留著")
    db.commit()
    conversations_before = db.query(Conversation).count()
    messages_before = db.query(Message).count()

    duplicated = [a_id, a_id, b_id, b_id]
    deleted = client.post(_BULK, headers=headers, json={"ids": duplicated})
    assert deleted.status_code == 200, deleted.text
    assert deleted.json() == {"deleted": 2}

    _reload(db)
    assert _summary_ids(db, owner.id) == [kept_id]
    assert _summary_text(db, kept_id) == _KEPT_SUMMARY
    assert _summary_text(db, other_id) == "別人的摘要要留著"
    assert db.get(UserFact, 7101).value == "雷達組"
    assert db.get(UserFact, 7102).value == "請用條列"
    assert db.get(ConversationMemoryChunk, 7103).content == "原始片段要留著"
    assert db.query(Conversation).count() == conversations_before
    assert db.query(Message).count() == messages_before
    assert db.get(Conversation, own_a_id).title == "own-a"
    stones = _tombstones(db, owner.id)
    assert [(stone.kind, stone.conversation_id, stone.covered_message_id) for stone in stones] == [
        ("summary", own_a_id, asst_a_id),
        ("summary", own_b_id, asst_b_id),
    ]
    assert _tombstones(db, other.id) == []

    repeated = client.post(
        _BULK,
        headers=headers,
        json={"ids": [a_id] * 200},
    )
    assert repeated.status_code == 404, repeated.text
    assert repeated.json() == _MISSING
    _reload(db)
    assert _summary_ids(db, owner.id) == [kept_id]
    assert len(_tombstones(db, owner.id)) == 2


def test_bulk_delete_rejects_empty_non_integer_negative_and_oversize(client, db):
    owner = make_user(db, username="bulk-invalid")
    token = login(client, "bulk-invalid")
    client.cookies.clear()
    headers = _bearer(token)
    conv = _conv(db, owner, "invalid")
    _, asst = _pair(db, conv, "留下這則", "好。")
    summary = _save_summary(db, owner, conv, "不該被非法請求刪掉", asst.id)

    cases = [
        {},
        {"ids": None},
        {"ids": []},
        {"ids": [0]},
        {"ids": [-1]},
        {"ids": [1.5]},
        {"ids": [1.0]},
        {"ids": ["1"]},
        {"ids": [True]},
        {"ids": [summary.id, "2"]},
        {"ids": [summary.id] * 201},
        {"ids": list(range(1, 202))},
    ]
    for payload in cases:
        response = client.post(_BULK, headers=headers, json=payload)
        assert response.status_code == 422, (payload, response.status_code, response.text)
        assert "summary_id" not in response.text

    missing = client.post(_BULK, headers=headers, json={"ids": list(range(10000, 10200))})
    assert missing.status_code == 404, missing.text
    assert missing.json() == _MISSING

    _reload(db)
    assert _summary_text(db, summary.id) == "不該被非法請求刪掉"
    assert _tombstones(db, owner.id) == []


def test_bulk_delete_foreign_or_missing_id_leaves_the_whole_batch(client, db):
    """別人的 id、不存在的 id，跟自己的 id 混在同一批時，整批不刪、不寫墓碑。"""
    owner = make_user(db, username="bulk-atomic-owner")
    other = make_user(db, username="bulk-atomic-other")
    token = login(client, "bulk-atomic-owner")
    client.cookies.clear()
    headers = _bearer(token)
    own_conv = _conv(db, owner, "atomic-own")
    _, own_asst = _pair(db, own_conv, "自己的", "好。")
    other_conv = _conv(db, other, "atomic-other")
    _, other_asst = _pair(db, other_conv, "別人的", "好。")
    own = _save_summary(db, owner, own_conv, "自己的摘要", own_asst.id)
    foreign = _save_summary(db, other, other_conv, "別人的摘要", other_asst.id)
    missing_id = own.id + foreign.id + 1000

    batches = [
        [own.id, foreign.id],
        [own.id, missing_id],
        [foreign.id],
        [own.id, own.id, foreign.id, missing_id],
    ]
    for ids in batches:
        response = client.post(_BULK, headers=headers, json={"ids": ids})
        assert response.status_code == 404, (ids, response.status_code, response.text)
        assert response.json() == _MISSING
        _reload(db)
        assert _summary_text(db, own.id) == "自己的摘要"
        assert _summary_text(db, foreign.id) == "別人的摘要"
        assert _tombstones(db, owner.id) == []
        assert _tombstones(db, other.id) == []


def test_clear_summaries_removes_only_the_owner_and_empty_returns_zero(client, db):
    owner = make_user(db, username="bulk-clear-owner")
    other = make_user(db, username="bulk-clear-other")
    empty_user = make_user(db, username="bulk-clear-empty")
    owner_token = login(client, "bulk-clear-owner")
    client.cookies.clear()
    other_token = create_tokens(other)["access_token"]
    empty_token = create_tokens(empty_user)["access_token"]

    own_a = _conv(db, owner, "clear-a")
    _, asst_a = _pair(db, own_a, "第一段", "好。")
    own_b = _conv(db, owner, "clear-b")
    _, asst_b = _pair(db, own_b, "第二段", "好。")
    other_conv = _conv(db, other, "clear-other")
    _, other_asst = _pair(db, other_conv, "別人", "好。")
    _save_summary(db, owner, own_a, "第一則摘要", asst_a.id)
    _save_summary(db, owner, own_b, "第二則摘要", asst_b.id)
    other_summary = _save_summary(db, other, other_conv, "別人的摘要", other_asst.id)
    other_id = int(other_summary.id)
    other_conv_id = int(other_conv.id)
    own_a_id = int(own_a.id)
    own_b_id = int(own_b.id)
    asst_a_id = int(asst_a.id)
    asst_b_id = int(asst_b.id)
    _add_fact(db, 7201, owner, own_a, "unit", "雷達組")
    _add_fact(db, 7202, owner, own_a, REPLY_STYLE_KEY, "請用短句")
    _add_chunk(db, 7203, owner, own_a, "清摘要時片段要留著")
    _add_fact(db, 7204, other, other_conv, "unit", "通訊組")
    db.commit()
    message_count = db.query(Message).count()

    empty = client.delete(_CLEAR, headers=_bearer(empty_token))
    assert empty.status_code == 200, empty.text
    assert empty.json() == {"deleted": 0}

    other_cleared = client.delete(_CLEAR, headers=_bearer(other_token))
    assert other_cleared.status_code == 200, other_cleared.text
    assert other_cleared.json() == {"deleted": 1}

    _reload(db)
    assert len(_summary_ids(db, owner.id)) == 2
    assert _summary_text(db, other_id) is None
    assert [stone.conversation_id for stone in _tombstones(db, other.id)] == [other_conv_id]
    assert _tombstones(db, owner.id) == []

    cleared = client.delete(_CLEAR, headers=_bearer(owner_token))
    assert cleared.status_code == 200, cleared.text
    assert cleared.json() == {"deleted": 2}
    _reload(db)
    assert _summary_ids(db, owner.id) == []
    assert {(stone.conversation_id, stone.covered_message_id) for stone in _tombstones(db, owner.id)} == {
        (own_a_id, asst_a_id),
        (own_b_id, asst_b_id),
    }
    assert db.get(UserFact, 7201).value == "雷達組"
    assert db.get(UserFact, 7202).value == "請用短句"
    assert db.get(ConversationMemoryChunk, 7203).content == "清摘要時片段要留著"
    assert db.get(UserFact, 7204).value == "通訊組"
    assert db.get(Conversation, own_a_id) is not None
    assert db.get(Conversation, other_conv_id) is not None
    assert db.query(Message).count() == message_count

    again = client.delete(_CLEAR, headers=_bearer(owner_token))
    assert again.status_code == 200, again.text
    assert again.json() == {"deleted": 0}
    _reload(db)
    assert len(_tombstones(db, owner.id)) == 2


def test_cookie_csrf_and_bearer_follow_existing_memory_auth(client, db):
    """Cookie 變更要 CSRF。Bearer 沿用既有豁免，而且只動權杖上的那一位。"""
    cookie_user = make_user(db, username="bulk-cookie")
    bearer_user = make_user(db, username="bulk-bearer")
    anon_bulk = client.post(_BULK, json={"ids": [1]})
    anon_clear = client.delete(_CLEAR)
    assert anon_bulk.status_code == 401, anon_bulk.text
    assert anon_clear.status_code == 401, anon_clear.text

    login(client, "bulk-cookie")
    csrf = client.cookies.get(CSRF_COOKIE_NAME)
    bearer = create_tokens(bearer_user)["access_token"]
    cookie_conv = _conv(db, cookie_user, "cookie")
    _, cookie_asst = _pair(db, cookie_conv, "cookie 這位", "好。")
    cookie_other = _conv(db, cookie_user, "cookie-other")
    _, cookie_other_asst = _pair(db, cookie_other, "cookie 另一則", "好。")
    bearer_conv = _conv(db, bearer_user, "bearer")
    _, bearer_asst = _pair(db, bearer_conv, "bearer 這位", "好。")
    bearer_other = _conv(db, bearer_user, "bearer-other")
    _, bearer_other_asst = _pair(db, bearer_other, "bearer 另一則", "好。")
    cookie_summary = _save_summary(db, cookie_user, cookie_conv, "cookie 摘要", cookie_asst.id)
    cookie_rest = _save_summary(
        db, cookie_user, cookie_other, "cookie 剩下的", cookie_other_asst.id
    )
    bearer_summary = _save_summary(db, bearer_user, bearer_conv, "bearer 摘要", bearer_asst.id)
    bearer_rest = _save_summary(
        db, bearer_user, bearer_other, "bearer 剩下的", bearer_other_asst.id
    )
    cookie_id = int(cookie_summary.id)
    cookie_rest_id = int(cookie_rest.id)
    bearer_id = int(bearer_summary.id)
    bearer_rest_id = int(bearer_rest.id)

    denied_bulk = client.post(_BULK, json={"ids": [cookie_id]})
    denied_clear = client.delete(_CLEAR)
    assert denied_bulk.status_code == 403, denied_bulk.text
    assert denied_clear.status_code == 403, denied_clear.text
    assert "CSRF" in denied_bulk.json()["detail"]
    assert "CSRF" in denied_clear.json()["detail"]
    _reload(db)
    assert _summary_text(db, cookie_id) is not None
    assert _summary_text(db, bearer_id) is not None

    by_bearer = client.post(
        _BULK,
        headers=_bearer(bearer),
        json={"ids": [bearer_id, bearer_id]},
    )
    assert by_bearer.status_code == 200, by_bearer.text
    assert by_bearer.json() == {"deleted": 1}
    _reload(db)
    assert _summary_text(db, bearer_id) is None
    assert _summary_text(db, cookie_id) is not None

    by_cookie = client.post(
        _BULK,
        headers={"X-CSRF-Token": csrf},
        json={"ids": [cookie_id, cookie_id]},
    )
    assert by_cookie.status_code == 200, by_cookie.text
    assert by_cookie.json() == {"deleted": 1}
    _reload(db)
    assert _summary_text(db, cookie_id) is None
    assert _summary_text(db, bearer_rest_id) is not None

    bearer_cleared = client.delete(_CLEAR, headers=_bearer(bearer))
    assert bearer_cleared.status_code == 200, bearer_cleared.text
    assert bearer_cleared.json() == {"deleted": 1}
    _reload(db)
    assert _summary_text(db, bearer_rest_id) is None
    assert _summary_text(db, cookie_rest_id) is not None

    cookie_cleared = client.delete(_CLEAR, headers={"X-CSRF-Token": csrf})
    assert cookie_cleared.status_code == 200, cookie_cleared.text
    assert cookie_cleared.json() == {"deleted": 1}
    _reload(db)
    assert _summary_ids(db, cookie_user.id) == []
    assert _summary_ids(db, bearer_user.id) == []
    assert len(_tombstones(db, cookie_user.id)) == 2
    assert len(_tombstones(db, bearer_user.id)) == 2


def test_single_summary_delete_still_writes_one_tombstone(client, db):
    owner = make_user(db, username="bulk-single")
    token = login(client, "bulk-single")
    client.cookies.clear()
    conv = _conv(db, owner, "single")
    _, asst = _pair(db, conv, "單筆仍可刪", "好。")
    other = _conv(db, owner, "single-kept")
    _, other_asst = _pair(db, other, "單筆別刪", "好。")
    summary = _save_summary(db, owner, conv, "單筆摘要", asst.id)
    kept = _save_summary(db, owner, other, "留下的單筆", other_asst.id)
    summary_id = int(summary.id)
    kept_id = int(kept.id)
    conv_id = int(conv.id)
    asst_id = int(asst.id)

    deleted = client.delete(
        f"/api/memory/summaries/{summary_id}",
        headers=_bearer(token),
    )
    assert deleted.status_code == 200, deleted.text
    assert deleted.json() == {"deleted": 1}
    _reload(db)
    assert _summary_text(db, summary_id) is None
    assert _summary_text(db, kept_id) == "留下的單筆"
    stones = _tombstones(db, owner.id)
    assert len(stones) == 1
    assert stones[0].kind == "summary"
    assert stones[0].conversation_id == conv_id
    assert stones[0].covered_message_id == asst_id


@pytest.mark.asyncio
async def test_bulk_deleted_summaries_are_not_rebuilt_from_the_same_transcript(
    client, db, monkeypatch
):
    user = make_user(db, username="bulk-tomb-rebuild")
    other = make_user(db, username="bulk-tomb-other")
    token = login(client, "bulk-tomb-rebuild")
    client.cookies.clear()
    conv_a = _conv(db, user, "rebuild-a")
    conv_b = _conv(db, user, "rebuild-b")
    _pair(db, conv_a, "我在雷達組，請用條列", "好的，之後用條列。")
    _pair(db, conv_b, "我在雷達組，請用條列", "好的，之後用條列。")
    other_conv = _conv(db, other, "rebuild-other")
    _, other_asst = _pair(db, other_conv, "別人仍在", "好。")
    other_summary = _save_summary(db, other, other_conv, "別人的摘要還在", other_asst.id)
    _add_fact(db, 7301, user, conv_a, "unit", "雷達組")
    db.commit()
    _summary_role(db, "summary-bulk-tomb")
    payload = '{"summary":"使用者在雷達組，希望條列。","facts":[]}'
    script = _Scripted([payload, payload, payload, payload])
    _install_model(monkeypatch, script)

    await memory_service.refresh_conversation(conv_a.id, db=db)
    await memory_service.refresh_conversation(conv_b.id, db=db)
    listed = client.get(_CLEAR, headers=_bearer(token))
    assert listed.status_code == 200, listed.text
    ids = [item["id"] for item in listed.json()["items"]]
    assert len(ids) == 2
    calls_after_create = script.calls

    deleted = client.post(
        _BULK,
        headers=_bearer(token),
        json={"ids": [ids[0], ids[0], ids[1]]},
    )
    assert deleted.status_code == 200, deleted.text
    assert deleted.json() == {"deleted": 2}
    _reload(db)
    stones = _tombstones(db, user.id)
    assert len(stones) == 2
    assert {stone.kind for stone in stones} == {"summary"}
    assert {stone.conversation_id for stone in stones} == {conv_a.id, conv_b.id}

    await memory_service.refresh_conversation(conv_a.id, db=db)
    await memory_service.refresh_conversation(conv_b.id, db=db)
    _reload(db)
    assert script.calls == calls_after_create
    assert _summary_ids(db, user.id) == []
    assert _summary_text(db, other_summary.id) == "別人的摘要還在"
    assert db.get(UserFact, 7301).value == "雷達組"


@pytest.mark.asyncio
async def test_clear_does_not_rebuild_the_same_transcript(client, db, monkeypatch):
    user = make_user(db, username="bulk-clear-rebuild")
    token = login(client, "bulk-clear-rebuild")
    client.cookies.clear()
    conv = _conv(db, user)
    _pair(db, conv, "我在雷達組，請用條列", "好的，之後用條列。")
    _summary_role(db, "summary-clear-tomb")
    payload = '{"summary":"使用者在雷達組，希望條列。","facts":[]}'
    script = _Scripted([payload, '{"summary":"不該復活的摘要","facts":[]}'])
    _install_model(monkeypatch, script)

    await memory_service.refresh_conversation(conv.id, db=db)
    listed = client.get(_CLEAR, headers=_bearer(token))
    assert listed.json()["total"] == 1
    calls_after_create = script.calls

    cleared = client.delete(_CLEAR, headers=_bearer(token))
    assert cleared.status_code == 200, cleared.text
    assert cleared.json() == {"deleted": 1}
    _reload(db)
    assert len(_tombstones(db, user.id)) == 1
    assert _tombstones(db, user.id)[0].kind == "summary"

    await memory_service.refresh_conversation(conv.id, db=db)
    _reload(db)
    assert script.calls == calls_after_create
    assert db.query(ConversationSummary).count() == 0
