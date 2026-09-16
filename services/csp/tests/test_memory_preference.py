"""F 期：使用者自寫回覆偏好，萃取不得覆寫。"""
from __future__ import annotations

from tests.conftest import login, make_user

from app.models.user_memory import UserFact
from app.services.memory_service import (
    REPLY_STYLE_KEY,
    REPLY_STYLE_MAX_CHARS,
    facts_safe_for_extraction,
)


def _headers(client, db, username="pref_alice"):
    make_user(db, username=username)
    return {"Authorization": f"Bearer {login(client, username=username)}"}


def test_facts_safe_for_extraction_drops_reply_style():
    kept = facts_safe_for_extraction(
        [
            {"key": REPLY_STYLE_KEY, "value": "請簡潔", "confidence": 0.9},
            {"key": "preference.tone", "value": "正式", "confidence": 0.8},
            {"key": "role", "value": "工程師", "confidence": 0.7},
        ]
    )
    assert [f["key"] for f in kept] == ["preference.tone", "role"]


def test_get_preference_empty(client, db):
    headers = _headers(client, db, username="pref_empty")
    resp = client.get("/api/memory/preference", headers=headers)
    assert resp.status_code == 200
    assert resp.json() == {"text": "", "updated_at": None}


def test_put_get_and_clear_preference(client, db):
    headers = _headers(client, db, username="pref_write")
    put = client.put(
        "/api/memory/preference",
        headers=headers,
        json={"text": "  請用繁體中文，先給結論  "},
    )
    assert put.status_code == 200
    body = put.json()
    assert body["text"] == "請用繁體中文，先給結論"
    assert body["updated_at"]

    got = client.get("/api/memory/preference", headers=headers)
    assert got.status_code == 200
    assert got.json()["text"] == "請用繁體中文，先給結論"

    facts = client.get("/api/memory/facts", headers=headers)
    assert facts.status_code == 200
    keys = [f["key"] for f in facts.json()["facts"]]
    assert REPLY_STYLE_KEY in keys

    cleared = client.put("/api/memory/preference", headers=headers, json={"text": "   "})
    assert cleared.status_code == 200
    assert cleared.json()["text"] == ""
    facts2 = client.get("/api/memory/facts", headers=headers)
    assert REPLY_STYLE_KEY not in [f["key"] for f in facts2.json()["facts"]]


def test_preference_is_per_user(client, db):
    a = _headers(client, db, username="pref_a")
    b = _headers(client, db, username="pref_b")
    client.put("/api/memory/preference", headers=a, json={"text": "只要 A"})
    client.put("/api/memory/preference", headers=b, json={"text": "只要 B"})
    assert client.get("/api/memory/preference", headers=a).json()["text"] == "只要 A"
    assert client.get("/api/memory/preference", headers=b).json()["text"] == "只要 B"


def test_preference_rejects_overlong(client, db):
    headers = _headers(client, db, username="pref_long")
    resp = client.put(
        "/api/memory/preference",
        headers=headers,
        json={"text": "x" * (REPLY_STYLE_MAX_CHARS + 1)},
    )
    assert resp.status_code == 422


def test_clear_facts_keeps_reply_style(client, db):
    headers = _headers(client, db, username="pref_keep")
    client.put("/api/memory/preference", headers=headers, json={"text": "保持這段"})
    db.expire_all()
    pref = db.query(UserFact).filter(UserFact.key == REPLY_STYLE_KEY).one()
    db.add(
        UserFact(
            id=int(pref.id) + 1,
            user_id=pref.user_id,
            key="role",
            value="工程師",
            confidence=0.8,
        )
    )
    db.commit()
    cleared = client.delete("/api/memory/facts", headers=headers)
    assert cleared.status_code == 200
    assert cleared.json()["deleted"] == 1
    assert client.get("/api/memory/preference", headers=headers).json()["text"] == "保持這段"


def test_preference_survives_as_user_fact_row(client, db):
    headers = _headers(client, db, username="pref_row")
    client.put("/api/memory/preference", headers=headers, json={"text": "條列回答"})
    user = db.query(UserFact).filter(UserFact.key == REPLY_STYLE_KEY).one()
    assert user.value == "條列回答"
    assert user.confidence == 1.0
    assert user.source_conversation_id is None
