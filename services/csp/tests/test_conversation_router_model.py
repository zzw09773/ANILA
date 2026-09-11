"""Conversation persists Router model choice with compare-and-swap."""
from sqlalchemy.orm import Session

from app.models.conversation import Conversation
from app.models.router_model_grant import RouterModelGrant
from tests.conftest import login, make_model, make_user


def _open_router_llm(db: Session, name: str, *, primary: bool = False):
    model = make_model(db, name=name)
    model.router_enabled = True
    model.is_router_primary = primary
    db.add(RouterModelGrant(model_id=model.id, scope_type="all"))
    db.commit()
    db.refresh(model)
    return model


def test_create_conversation_persists_campus_default(client, db: Session):
    make_user(db, username="alice")
    glm = _open_router_llm(db, "glm-default", primary=True)
    token = login(client, "alice")
    resp = client.post(
        "/api/conversations",
        headers={"Authorization": f"Bearer {token}"},
        json={"title": "t", "origin": "anila-ui"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["router_model_id"] == glm.id
    assert body["router_model_name"] == "glm-default"
    assert body["router_selection_version"] == 1


def test_put_router_model_cas_and_foreign_404(client, db: Session):
    owner = make_user(db, username="owner")
    make_user(db, username="intruder")
    glm = _open_router_llm(db, "glm-cas", primary=True)
    qwen = _open_router_llm(db, "qwen-cas")
    token = login(client, "owner")
    created = client.post(
        "/api/conversations",
        headers={"Authorization": f"Bearer {token}"},
        json={"title": "t", "origin": "anila-ui"},
    ).json()
    conv_id = created["id"]
    stale = client.put(
        f"/api/conversations/{conv_id}/router-model",
        headers={"Authorization": f"Bearer {token}"},
        json={"router_model_id": qwen.id, "expected_version": 0},
    )
    assert stale.status_code == 409, stale.text
    ok = client.put(
        f"/api/conversations/{conv_id}/router-model",
        headers={"Authorization": f"Bearer {token}"},
        json={"router_model_id": qwen.id, "expected_version": created["router_selection_version"]},
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["router_model_id"] == qwen.id
    other = login(client, "intruder")
    denied = client.put(
        f"/api/conversations/{conv_id}/router-model",
        headers={"Authorization": f"Bearer {other}"},
        json={"router_model_id": glm.id, "expected_version": ok.json()["router_selection_version"]},
    )
    assert denied.status_code == 404


def test_create_with_illegal_model_does_not_leave_row(client, db):
    make_user(db, username="alice-bad")
    glm = make_model(db, name="glm-illegal")
    token = login(client, "alice-bad")
    before = db.query(Conversation).count()
    resp = client.post(
        "/api/conversations",
        headers={"Authorization": f"Bearer {token}"},
        json={"title": "t", "origin": "anila-ui", "router_model_id": glm.id},
    )
    assert resp.status_code in (403, 409), resp.text
    assert db.query(Conversation).count() == before


def test_cas_version_predicate_rejects_stale_second_write(client, db):
    from sqlalchemy import update
    from app.models.conversation import Conversation as Conv
    owner = make_user(db, username="cas-owner2")
    glm = _open_router_llm(db, "glm-cas2", primary=True)
    qwen = _open_router_llm(db, "qwen-cas2")
    token = login(client, "cas-owner2")
    created = client.post(
        "/api/conversations",
        headers={"Authorization": f"Bearer {token}"},
        json={"title": "t", "origin": "anila-ui"},
    ).json()
    conv_id = created["id"]
    v = created["router_selection_version"]
    first = client.put(
        f"/api/conversations/{conv_id}/router-model",
        headers={"Authorization": f"Bearer {token}"},
        json={"router_model_id": qwen.id, "expected_version": v},
    )
    assert first.status_code == 200, first.text
    second = client.put(
        f"/api/conversations/{conv_id}/router-model",
        headers={"Authorization": f"Bearer {token}"},
        json={"router_model_id": glm.id, "expected_version": v},
    )
    assert second.status_code == 409
    row = db.get(Conv, conv_id)
    assert row.router_model_id == qwen.id


def test_ineligible_set_primary_keeps_default(client, db):
    admin = make_user(db, username="adm-primary", role="admin")
    glm = _open_router_llm(db, "glm-keep", primary=True)
    other = make_model(db, name="other-llm")
    token = login(client, "adm-primary")
    resp = client.post(f"/api/models/{other.id}/set-router-primary", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 400, resp.text
    db.refresh(glm)
    assert glm.is_router_primary is True
