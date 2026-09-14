from app.models.department import Department
from app.models.model_access_group import ModelAccessGroup, ModelAccessGroupMember
from app.models.router_model_grant import RouterModelGrant
from app.services.auth_service import create_tokens
from tests.conftest import make_model, make_user


def _bearer(user):
    return {"Authorization": f"Bearer {create_tokens(user)['access_token']}"}


def test_group_members_and_linked_models(client, db):
    admin = make_user(db, username="aud-admin", role="admin")
    member = make_user(db, username="aud-member")
    group = ModelAccessGroup(name="專案甲", is_active=True, created_by=admin.id)
    db.add(group)
    db.flush()
    db.add(ModelAccessGroupMember(group_id=group.id, user_id=member.id))
    model = make_model(db, name="glm-aud")
    model.router_enabled = True
    model.model_type = "llm"
    db.add(RouterModelGrant(model_id=model.id, scope_type="group", group_id=group.id))
    db.commit()

    headers = _bearer(admin)
    mem = client.get(f"/api/model-access-groups/{group.id}/members", headers=headers)
    assert mem.status_code == 200
    assert mem.json()[0]["username"] == "aud-member"

    linked = client.get(f"/api/model-access-groups/{group.id}/models", headers=headers)
    assert linked.status_code == 200
    assert linked.json()[0]["name"] == "glm-aud"

    models = client.get(f"/api/users/{member.id}/router-models", headers=headers)
    assert models.status_code == 200
    assert models.json()[0]["name"] == "glm-aud"
    assert "group" in models.json()[0]["grant_sources"]


def test_directory_include_self(client, db):
    admin = make_user(db, username="dir-self-admin", role="admin")
    headers = _bearer(admin)
    hidden = client.get("/api/directory/users", headers=headers, params={"q": "dir-self-admin"})
    assert hidden.status_code == 200
    assert hidden.json() == []
    shown = client.get(
        "/api/directory/users",
        headers=headers,
        params={"q": "dir-self-admin", "include_self": True},
    )
    assert shown.status_code == 200
    assert shown.json()[0]["username"] == "dir-self-admin"
