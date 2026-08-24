"""Collection caption intent: create/read/update; unknown model 422; omit = NULL."""
from __future__ import annotations

import os

os.environ.setdefault("ANILA_ALLOW_DEV_SECRET", "1")

from app.models.ingestion import IngestionCollection
from app.models.model_registry import ModelRegistry
from tests.conftest import login, make_user


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _payload(name: str, **extra) -> dict:
    body = {
        "name": name,
        "chunking_config": {"strategy": "fixed", "params": {"size": 256}},
        "classification_level": "無機密",
        "origin": "csp",
    }
    body.update(extra)
    return body


def _register(db, name: str) -> None:
    db.add(
        ModelRegistry(
            name=name,
            display_name=name,
            model_type="llm",
            endpoint_url="https://models.example.test/v1",
            is_active=True,
        )
    )
    db.commit()


def test_omit_caption_fields_stores_null(client, db):
    user = make_user(db, username="cap_omit", role="developer")
    token = login(client, user.username)
    resp = client.post(
        "/api/ingestion/collections",
        headers=_auth(token),
        json=_payload("cap-omit"),
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["caption_enabled"] is None
    assert body["caption_model"] is None
    row = db.get(IngestionCollection, body["id"])
    assert row.caption_enabled is None
    assert row.caption_model is None


def test_create_stores_intent_and_echoes_it(client, db):
    _register(db, "gemma4")
    user = make_user(db, username="cap_on", role="developer")
    token = login(client, user.username)
    resp = client.post(
        "/api/ingestion/collections",
        headers=_auth(token),
        json=_payload("cap-on", caption_enabled=True, caption_model="gemma4"),
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["caption_enabled"] is True
    assert body["caption_model"] == "gemma4"


def test_unknown_caption_model_is_422(client, db):
    user = make_user(db, username="cap_bad", role="developer")
    token = login(client, user.username)
    resp = client.post(
        "/api/ingestion/collections",
        headers=_auth(token),
        json=_payload("cap-bad", caption_enabled=True, caption_model="does-not-exist-vlm"),
    )
    assert resp.status_code == 422, resp.text
    assert "不在模型清單" in resp.text


def test_patch_can_set_and_clear_intent(client, db):
    _register(db, "gemma4")
    user = make_user(db, username="cap_patch", role="developer")
    token = login(client, user.username)
    created = client.post(
        "/api/ingestion/collections",
        headers=_auth(token),
        json=_payload("cap-patch"),
    ).json()
    cid = created["id"]
    on = client.patch(
        f"/api/ingestion/collections/{cid}",
        headers=_auth(token),
        json={"caption_enabled": True, "caption_model": "gemma4"},
    )
    assert on.status_code == 200, on.text
    assert on.json()["caption_enabled"] is True
    assert on.json()["caption_model"] == "gemma4"
    off = client.patch(
        f"/api/ingestion/collections/{cid}",
        headers=_auth(token),
        json={"caption_enabled": False, "caption_model": ""},
    )
    assert off.status_code == 200, off.text
    assert off.json()["caption_enabled"] is False
    assert off.json()["caption_model"] is None
