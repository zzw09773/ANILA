"""綁定的對話模型不能用時，resolve 要回機器可讀原因，停用也不改綁。"""

from sqlalchemy.orm import Session

from app.models.conversation import Conversation
from app.models.model_registry import ModelRegistry
from app.models.router_model_grant import RouterModelGrant
from tests.conftest import login, make_model, make_user


def _open(db: Session, name: str, *, primary: bool = False, display_name: str | None = None):
    model = make_model(db, name=name)
    model.router_enabled = True
    model.is_active = True
    model.model_type = "llm"
    model.is_router_primary = primary
    if display_name:
        model.display_name = display_name
    db.add(RouterModelGrant(model_id=model.id, scope_type="all"))
    db.commit()
    db.refresh(model)
    return model


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _bind(client, token: str, title: str = "t") -> dict:
    resp = client.post(
        "/api/conversations",
        headers=_auth(token),
        json={"title": title, "origin": "anila-ui"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _detail(resp):
    body = resp.json()
    detail = body.get("detail")
    assert isinstance(detail, dict), body
    return detail


def test_resolve_inactive_bound_model_names_the_reason(client, db: Session):
    make_user(db, username="inactive-user")
    model = _open(db, "glm-5.3-flash", primary=True, display_name="GLM 快閃")
    token = login(client, "inactive-user")
    conv = _bind(client, token)
    model.is_active = False
    db.commit()

    resp = client.post(
        "/api/router-models/resolve",
        headers=_auth(token),
        json={"conversation_id": conv["id"], "router_model": "glm-5.3-flash"},
    )
    assert resp.status_code == 409, resp.text
    detail = _detail(resp)
    assert detail["code"] == "model_unavailable"
    assert detail["reason"] == "inactive"
    assert detail["display_name"] == "GLM 快閃"
    assert detail["name"] == "glm-5.3-flash"
    # 停用仍有全院授權時，不能誤報成沒有授權。
    assert detail["reason"] != "not_granted"


def test_resolve_missing_bound_model(client, db: Session):
    make_user(db, username="missing-user")
    model = _open(db, "gone-model", primary=True, display_name="已刪")
    token = login(client, "missing-user")
    conv = _bind(client, token)
    db.query(RouterModelGrant).filter(RouterModelGrant.model_id == model.id).delete()
    db.query(ModelRegistry).filter(ModelRegistry.id == model.id).delete()
    db.commit()

    resp = client.post(
        "/api/router-models/resolve",
        headers=_auth(token),
        json={"conversation_id": conv["id"]},
    )
    assert resp.status_code == 409, resp.text
    detail = _detail(resp)
    assert detail["code"] == "model_unavailable"
    assert detail["reason"] == "missing"


def test_resolve_requested_name_missing_without_conversation(client, db: Session):
    make_user(db, username="name-missing")
    token = login(client, "name-missing")
    resp = client.post(
        "/api/router-models/resolve",
        headers=_auth(token),
        json={"router_model": "no-such-model"},
    )
    detail = _detail(resp)
    assert detail["code"] == "model_unavailable"
    assert detail["reason"] == "missing"
    assert detail["display_name"] == "no-such-model"
    assert detail["name"] == "no-such-model"


def test_resolve_not_router_enabled_is_not_a_grant_denial(client, db: Session):
    make_user(db, username="menu-off")
    model = _open(db, "menu-off-model", primary=True, display_name="選單已關")
    token = login(client, "menu-off")
    conv = _bind(client, token)
    model.router_enabled = False
    model.is_router_primary = False
    db.commit()

    resp = client.post(
        "/api/router-models/resolve",
        headers=_auth(token),
        json={"conversation_id": conv["id"]},
    )
    detail = _detail(resp)
    assert detail["code"] == "model_unavailable"
    assert detail["reason"] == "not_router_enabled"
    assert detail["display_name"] == "選單已關"


def test_resolve_not_granted_after_grant_removed(client, db: Session):
    make_user(db, username="grant-off")
    kept = _open(db, "still-default", primary=True)
    model = _open(db, "private-model", display_name="限閱模型")
    token = login(client, "grant-off")
    created = _bind(client, token)
    switched = client.put(
        f"/api/conversations/{created['id']}/router-model",
        headers=_auth(token),
        json={
            "router_model_id": model.id,
            "expected_version": created["router_selection_version"],
        },
    )
    assert switched.status_code == 200, switched.text
    db.query(RouterModelGrant).filter(RouterModelGrant.model_id == model.id).delete()
    db.commit()

    resp = client.post(
        "/api/router-models/resolve",
        headers=_auth(token),
        json={"conversation_id": created["id"]},
    )
    assert resp.status_code == 403, resp.text
    detail = _detail(resp)
    assert detail["code"] == "model_unavailable"
    assert detail["reason"] == "not_granted"
    assert detail["display_name"] == "限閱模型"
    assert kept.id != model.id


def test_conversation_api_reports_unavailable_model_before_send(client, db: Session):
    make_user(db, username="open-user")
    model = _open(db, "glm-5.3-flash", primary=True, display_name="glm-5.3-flash")
    token = login(client, "open-user")
    conv = _bind(client, token, title="舊對話")
    model.is_active = False
    db.commit()

    detail = client.get(f"/api/conversations/{conv['id']}", headers=_auth(token))
    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert body["router_model_unavailable_reason"] == "inactive"
    assert body["router_model_display_name"] == "glm-5.3-flash"
    assert body["router_model_id"] == model.id

    listed = client.get("/api/conversations", headers=_auth(token))
    assert listed.status_code == 200, listed.text
    row = next(item for item in listed.json() if item["id"] == conv["id"])
    assert row["router_model_unavailable_reason"] == "inactive"
    assert row["router_model_display_name"] == "glm-5.3-flash"


def test_admin_model_list_counts_bound_conversations_and_deactivate_does_not_migrate(client, db: Session):
    make_user(db, username="chatter")
    make_user(db, username="model-admin", role="admin")
    model = _open(db, "glm-5.3-flash", primary=True, display_name="glm-5.3-flash")
    other = _open(db, "qwen-stay")
    token = login(client, "chatter")
    first = _bind(client, token, title="一")
    second = _bind(client, token, title="二")
    admin = login(client, "model-admin")

    listed = client.get("/api/models", headers=_auth(admin))
    assert listed.status_code == 200, listed.text
    row = next(item for item in listed.json() if item["id"] == model.id)
    assert row["router_conversation_count"] == 2
    other_row = next(item for item in listed.json() if item["id"] == other.id)
    assert other_row["router_conversation_count"] == 0

    removed = client.delete(f"/api/models/{model.id}", headers=_auth(admin))
    assert removed.status_code == 200, removed.text

    db.expire_all()
    for conv_id in (first["id"], second["id"]):
        stored = db.get(Conversation, conv_id)
        assert stored.router_model_id == model.id
    assert db.get(ModelRegistry, model.id).is_active is False
