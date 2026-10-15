"""快製平台以隔離資料庫、合成簽章及正式 HTTP API 驗證。"""

import importlib.util
from pathlib import Path

import pytest
import asyncio
import httpx
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization
from sqlalchemy import inspect, text

from app.database import Base, get_db
from app.main import app
from app.models.agent import Agent
from app.models.audit_log import AuditLog
from app.models.department import Department
from app.models.ingestion import IngestionCollection
from app.models.model_registry import ModelRegistry
from app.models.quick_builder import QuickAssistant, QuickVersion
from app.models.unit_admin_assignment import UnitAdminAssignment
from app.models.user import User, UserModelPermission
from app.services.api_key_service import check_agent_permission
from app.utils.security import hash_password


def graph(kind="format", config=None):
    return {
        "nodes": [
            {"id": "in", "kind": "input", "config": {}},
            {"id": "work", "kind": kind, "config": config or {"style": "paragraph"}},
            {"id": "out", "kind": "output", "config": {}},
        ],
        "edges": [
            {"source": "in", "target": "work"},
            {"source": "work", "target": "out"},
        ],
    }


@pytest.fixture
def people(db):
    a, b = Department(name="試辦甲"), Department(name="試辦乙")
    db.add_all([a, b])
    db.flush()
    users = {}
    for name, role, dept in (
        ("creator", "user", a),
        ("supervisor", "user", a),
        ("colleague", "user", a),
        ("outsider", "user", b),
        ("administrator", "admin", b),
    ):
        users[name] = User(
            username=name,
            role=role,
            department_id=dept.id,
            hashed_password=hash_password("Synthetic-pass-2026"),
            is_active=True,
            is_approved=True,
        )
        db.add(users[name])
    model = ModelRegistry(
        name="test-offline-model",
        display_name="測試離線模型",
        endpoint_url="http://model.test",
        is_active=True,
        model_type="llm",
    )
    db.add(model)
    db.flush()
    db.add(
        UnitAdminAssignment(
            user_id=users["supervisor"].id,
            department_id=a.id,
            granted_by=users["administrator"].id,
            source="manual",
        )
    )
    for user in users.values():
        db.add(UserModelPermission(user_id=user.id, model_id=model.id))
    db.commit()
    return users, model, a, b


@pytest.fixture
def http(db, monkeypatch):
    from app.services import jwt_keyring

    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv = private.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    pub = private.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    material = jwt_keyring.SigningMaterial(kid="synthetic-quick-test", private_pem=priv)
    monkeypatch.setattr(
        jwt_keyring, "active_signing_material", lambda db=None: material
    )
    monkeypatch.setattr(jwt_keyring, "active_private_pem", lambda db=None: priv)
    monkeypatch.setattr(jwt_keyring, "active_kid", lambda db=None: material.kid)
    monkeypatch.setattr(
        jwt_keyring,
        "verification_material",
        lambda kid, db=None: (
            jwt_keyring.VerificationMaterial(pub, "active", None, False)
            if kid == material.kid
            else None
        ),
    )

    def dependency():
        yield db

    app.dependency_overrides[get_db] = dependency

    class DirectHTTP:
        cookies = httpx.Cookies()

        def request(self, method, url, **kwargs):
            async def execute():
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://testserver"
                ) as client:
                    return await client.request(method, url, **kwargs)

            return asyncio.run(execute())

        def get(self, url, **kwargs):
            return self.request("GET", url, **kwargs)

        def post(self, url, **kwargs):
            return self.request("POST", url, **kwargs)

        def put(self, url, **kwargs):
            return self.request("PUT", url, **kwargs)

        def close(self):
            pass

    client = DirectHTTP()
    yield client
    client.close()
    app.dependency_overrides.clear()


def login(http, name):
    http.cookies.clear()
    result = http.post(
        "/api/auth/login", json={"username": name, "password": "Synthetic-pass-2026"}
    )
    assert result.status_code == 200, result.text
    token = result.json()["access_token"]
    http.cookies.clear()
    return {"Authorization": "Bearer " + token}


def create(http, people, **extra):
    _, model, _, _ = people
    headers = login(http, "creator")
    body = {
        "name": "規章助手",
        "description": "查詢單位規章",
        "graph": graph(),
        "model_id": model.id,
        **extra,
    }
    response = http.post("/api/quick-builder", json=body, headers=headers)
    assert response.status_code == 201, response.text
    return response.json(), body, headers


def unit_approve(http, item):
    version = item["versions"][0]["id"]
    creator = login(http, "creator")
    assert (
        http.post(
            f"/api/quick-builder/{item['id']}/submit",
            json={"version_id": version},
            headers=creator,
        ).status_code
        == 200
    )
    supervisor = login(http, "supervisor")
    response = http.post(
        f"/api/quick-builder/{item['id']}/review",
        json={"version_id": version, "approve": True},
        headers=supervisor,
    )
    assert response.status_code == 200, response.text
    creator = login(http, "creator")
    response = http.post(
        f"/api/quick-builder/{item['id']}/publish",
        json={"version_id": version},
        headers=creator,
    )
    assert response.status_code == 200, response.text
    return response.json()


def platform_approve(http, item):
    agent = item["versions"][0]["agent_id"]
    response = http.post(
        f"/api/agents/{agent}/approve", headers=login(http, "administrator")
    )
    assert response.status_code == 200, response.text
    return agent


def test_authentication_is_required(http):
    for path in (
        "/api/quick-builder",
        "/api/quick-builder/catalog",
        "/api/quick-builder/templates",
        "/api/quick-builder/mcp/tools",
    ):
        assert http.get(path).status_code == 401


def test_private_assistant_is_invisible_across_all_read_write_paths(http, people):
    item, body, _ = create(http, people)
    outsider = login(http, "outsider")
    assert http.get("/api/quick-builder", headers=outsider).json() == {"assistants": []}
    for suffix in (
        "",
        f"/compare?left={item['versions'][0]['id']}&right={item['versions'][0]['id']}",
    ):
        assert (
            http.get(
                f"/api/quick-builder/{item['id']}" + suffix, headers=outsider
            ).status_code
            == 404
        )
    for suffix, payload in (
        ("versions", {**body, "expected_revision": 1}),
        ("test", {"version_id": item["versions"][0]["id"], "message": "測試"}),
        ("submit", {"version_id": item["versions"][0]["id"]}),
        ("disable", None),
    ):
        assert (
            http.post(
                f"/api/quick-builder/{item['id']}/{suffix}",
                json=payload,
                headers=outsider,
            ).status_code
            == 404
        )


def test_revision_cas_restore_and_immutable_payload(http, people, db):
    item, body, h = create(http, people)
    old = item["versions"][0]
    response = http.post(
        f"/api/quick-builder/{item['id']}/versions",
        json={**body, "name": "修訂規章助手", "expected_revision": 1},
        headers=h,
    )
    assert response.status_code == 201, response.text
    assert response.json()["draft_revision"] == 2
    stale = http.post(
        f"/api/quick-builder/{item['id']}/versions",
        json={**body, "expected_revision": 1},
        headers=h,
    )
    assert stale.status_code == 409
    restored = http.post(
        f"/api/quick-builder/{item['id']}/restore",
        json={"version_id": old["id"], "expected_revision": 2},
        headers=h,
    )
    assert restored.status_code == 200
    assert restored.json()["draft_revision"] == 3
    assert restored.json()["versions"][0]["status"] == "draft"
    version = db.get(QuickVersion, old["id"])
    version.payload = {**version.payload, "name": "非法變更"}
    with pytest.raises(ValueError, match="IMMUTABLE_VERSION"):
        db.flush()
    db.rollback()


def test_two_approval_gates_and_old_version_survives_update(http, people, db):
    item, body, h = create(http, people)
    item = unit_approve(http, item)
    assert db.get(QuickAssistant, item["id"]).published_revision is None
    agent = platform_approve(http, item)
    assert db.get(QuickAssistant, item["id"]).published_revision == 1
    creator = login(http, "creator")
    saved = http.post(
        f"/api/quick-builder/{item['id']}/versions",
        json={**body, "expected_revision": 1, "name": "新版"},
        headers=creator,
    ).json()
    unit_approve(http, saved)
    assert db.get(Agent, agent).approval_status == "approved"
    assert db.get(QuickAssistant, item["id"]).published_revision == 1
    updated = http.get(f"/api/quick-builder/{item['id']}", headers=creator).json()
    new_agent = platform_approve(http, updated)
    assert new_agent != agent
    assert db.get(Agent, agent).approval_status == "disabled"
    assert db.get(QuickAssistant, item["id"]).published_revision == 2


def test_self_review_and_non_supervisor_refused(http, people):
    item, _, h = create(http, people)
    v = item["versions"][0]["id"]
    assert (
        http.post(
            f"/api/quick-builder/{item['id']}/submit", json={"version_id": v}, headers=h
        ).status_code
        == 200
    )
    for name in ("creator", "colleague"):
        assert (
            http.post(
                f"/api/quick-builder/{item['id']}/review",
                json={"version_id": v, "approve": True},
                headers=login(http, name),
            ).status_code
            == 403
        )


def test_submission_snapshot_survives_concurrent_edit(http, people):
    item, body, h = create(http, people)
    v = item["versions"][0]["id"]
    assert (
        http.post(
            f"/api/quick-builder/{item['id']}/submit", json={"version_id": v}, headers=h
        ).status_code
        == 200
    )
    saved = http.post(
        f"/api/quick-builder/{item['id']}/versions",
        json={**body, "name": "未審內容", "expected_revision": 1},
        headers=h,
    )
    assert saved.status_code == 201
    reviewed = http.post(
        f"/api/quick-builder/{item['id']}/review",
        json={"version_id": v, "approve": True},
        headers=login(http, "supervisor"),
    ).json()
    assert reviewed["versions"][0]["status"] == "draft"
    assert reviewed["versions"][1]["status"] == "approved"


def test_public_assistant_does_not_publish_collection_and_revocation_applies(
    http, people, db
):
    users, _, a, _ = people
    coll = IngestionCollection(
        name="非公開文件",
        created_by=users["creator"].id,
        department_id=a.id,
        status="active",
        classification_level="無機密",
        embedding_model="test",
        embedding_dim=3,
        chunking_config={},
    )
    db.add(coll)
    db.commit()
    assert (
        http.put(
            f"/api/quick-builder/collections/{coll.id}/publication",
            json={"scope": "unit"},
            headers=login(http, "administrator"),
        ).status_code
        == 200
    )
    item, _, _ = create(http, people, scope="public", collection_ids=[coll.id])
    item = unit_approve(http, item)
    platform_approve(http, item)
    outsider = login(http, "outsider")
    assert (
        http.get("/api/quick-builder", headers=outsider).json()["assistants"][0][
            "versions"
        ]
        == []
    )
    assert not http.get("/api/quick-builder/catalog", headers=outsider).json()[
        "collections"
    ]
    from app.services.collection_scope import can_read_collection

    assert not can_read_collection(db, users["outsider"], coll)
    body = {"scope": "public", "target_department_ids": []}
    path = f"/api/quick-builder/collections/{coll.id}/publication"
    assert (
        http.put(path, json=body, headers=login(http, "administrator")).status_code
        == 200
    )
    assert can_read_collection(db, users["outsider"], coll)
    assert (
        http.put(
            path, json={"scope": "unit"}, headers=login(http, "administrator")
        ).status_code
        == 200
    )
    assert not can_read_collection(db, users["outsider"], coll)


def test_runtime_and_document_download_are_real_http(http, people):
    item, _, h = create(
        http, people, graph=graph("document", {"template": "主旨：{{text}}"})
    )
    v = item["versions"][0]["id"]
    response = http.post(
        f"/api/quick-builder/{item['id']}/test",
        json={"version_id": v, "message": "測試通知"},
        headers=h,
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["status"] == "completed", data
    assert len(data["steps"]) == 3
    url = data["artifacts"][0]["download_url"]
    download = http.get(url, headers=h)
    assert download.status_code == 200
    assert download.content.startswith(b"{\\rtf1")
    assert http.get(url, headers=login(http, "outsider")).status_code == 404


def test_disabled_and_department_changed_users_cannot_use_old_grants(http, people, db):
    item, _, _ = create(http, people)
    item = unit_approve(http, item)
    agent_id = platform_approve(http, item)
    users, _, _, b = people
    assert check_agent_permission(
        db, user=users["colleague"], api_key_id=None, agent_id=agent_id
    )
    users["colleague"].department_id = b.id
    db.commit()
    assert not check_agent_permission(
        db, user=users["colleague"], api_key_id=None, agent_id=agent_id
    )
    assert (
        http.post(
            f"/api/quick-builder/{item['id']}/disable", headers=login(http, "creator")
        ).status_code
        == 200
    )
    assert db.get(Agent, agent_id).approval_status == "disabled"
    assert not check_agent_permission(
        db, user=users["creator"], api_key_id=None, agent_id=agent_id
    )
    actions = {
        x.action
        for x in db.query(AuditLog).filter_by(resource_type="quick_assistant").all()
    }
    assert {
        "quick_builder.create",
        "quick_builder.save",
        "quick_builder.submit",
        "quick_builder.approve",
        "quick_builder.register",
        "quick_builder.publish",
        "quick_builder.disable",
    } <= actions


def test_migration_roundtrip_preserves_existing_data(db_engine):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    path = (
        Path(__file__).resolve().parents[1]
        / "migrations/versions/r1_0076_quick_builder.py"
    )
    spec = importlib.util.spec_from_file_location("quick_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.down_revision == "r1_0075"
    names = [
        "quick_mcp_tools",
        "quick_mcp_services",
        "quick_collection_publications",
        "quick_templates",
        "quick_runs",
        "quick_versions",
        "quick_assistants",
    ]
    with db_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO departments (id,name,is_active) VALUES (999,'既有資料',1)"
            )
        )
        for name in names:
            Base.metadata.tables[name].drop(conn)
        with Operations.context(MigrationContext.configure(conn)):
            module.upgrade()
            assert set(names) <= set(inspect(conn).get_table_names())
            assert (
                conn.execute(text("SELECT name FROM departments WHERE id=999")).scalar()
                == "既有資料"
            )
            owner_id = conn.execute(
                User.__table__.insert().values(
                    username="migration-synthetic-user",
                    hashed_password="synthetic-not-a-login",
                    role="user",
                    department_id=999,
                )
            ).inserted_primary_key[0]
            agent_ids = []
            for name in ("migration-legacy-agent", "quick-1-v1"):
                agent_ids.append(
                    conn.execute(
                        Agent.__table__.insert().values(
                            name=name,
                            owner_user_id=owner_id,
                            endpoint_url="http://synthetic.invalid",
                            approval_status="approved",
                        )
                    ).inserted_primary_key[0]
                )
            assistant_id = conn.execute(
                QuickAssistant.__table__.insert().values(
                    owner_user_id=owner_id,
                    department_id=999,
                    draft_revision=1,
                )
            ).inserted_primary_key[0]
            conn.execute(
                QuickVersion.__table__.insert().values(
                    assistant_id=assistant_id,
                    revision=1,
                    payload={},
                    payload_hash="a" * 64,
                    created_by=owner_id,
                    agent_id=agent_ids[1],
                    status="published",
                )
            )
            module.downgrade()
            assert not set(names) & set(inspect(conn).get_table_names())
            assert (
                conn.execute(text("SELECT name FROM departments WHERE id=999")).scalar()
                == "既有資料"
            )
            assert (
                conn.execute(
                    text("SELECT approval_status FROM agents WHERE id=:id"),
                    {"id": agent_ids[0]},
                ).scalar()
                == "approved"
            )
            assert (
                conn.execute(
                    text("SELECT approval_status FROM agents WHERE id=:id"),
                    {"id": agent_ids[1]},
                ).scalar()
                == "disabled"
            )
            module.upgrade()


def test_registration_does_not_collide_with_retained_agent_after_schema_restore(
    http, people, db
):
    item, _, _ = create(http, people)
    db.add(
        Agent(
            name=f"quick-{item['id']}-v1",
            owner_user_id=people[0]["creator"].id,
            endpoint_url="anila://quick-builder/retained",
            approval_status="disabled",
        )
    )
    db.commit()
    item = unit_approve(http, item)
    agent = db.get(Agent, item["versions"][0]["agent_id"])
    assert agent.name.startswith(f"quick-{item['id']}-v1-")
    assert agent.approval_status == "registered"


def test_cross_tab_identity_change_refuses_old_draft(http, people):
    users, model, _, _ = people
    headers = login(http, "outsider")
    headers["X-ANILA-Actor-Id"] = str(users["creator"].id)
    response = http.post(
        "/api/quick-builder",
        headers=headers,
        json={"name": "舊帳號草稿", "graph": graph(), "model_id": model.id},
    )
    assert response.status_code == 409
    assert response.json()["detail"]["type"] == "IDENTITY_CHANGED"


def test_agent_metadata_cannot_bypass_workflow_review(http, people):
    item, _, _ = create(http, people)
    item = unit_approve(http, item)
    agent_id = item["versions"][0]["agent_id"]
    response = http.put(
        f"/api/agents/{agent_id}",
        headers=login(http, "creator"),
        json={"description_for_router": "未經審核的行為"},
    )
    assert response.status_code == 409
    assert response.json()["detail"]["type"] == "IMMUTABLE_VERSION"


def test_validation_error_is_plain_chinese_and_does_not_echo_payload(http, people):
    response = http.post(
        "/api/quick-builder",
        headers=login(http, "creator"),
        json={"name": "", "graph": {}, "unexpected": "private-input"},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["type"] == "INVALID_INPUT"
    assert "private-input" not in response.text


def test_global_template_does_not_publish_private_source_ids(http, people):
    response = http.post(
        "/api/quick-builder/templates",
        headers=login(http, "administrator"),
        json={
            "name": "未公開來源範本",
            "description": "",
            "graph": graph("rag", {"collection_ids": [123], "top_k": 5}),
        },
    )
    assert response.status_code == 422
    assert response.json()["detail"]["type"] == "TEMPLATE_PRIVATE_SOURCE"


def test_global_template_is_hidden_when_its_public_source_is_revoked(http, people, db):
    users, _, department, _ = people
    collection = IngestionCollection(
        name="合成公開來源",
        created_by=users["creator"].id,
        department_id=department.id,
        status="active",
        embedding_model="synthetic",
        embedding_dim=3,
        chunking_config={},
    )
    db.add(collection)
    db.commit()
    publication_path = f"/api/quick-builder/collections/{collection.id}/publication"
    admin = login(http, "administrator")
    assert (
        http.put(publication_path, json={"scope": "public"}, headers=admin).status_code
        == 200
    )
    response = http.post(
        "/api/quick-builder/templates",
        headers=admin,
        json={
            "name": "來源可撤回的範本",
            "graph": graph("rag", {"collection_ids": [collection.id]}),
        },
    )
    assert response.status_code == 201, response.text
    template_id = response.json()["id"]
    outsider = login(http, "outsider")
    assert any(
        t["id"] == template_id
        for t in http.get("/api/quick-builder/templates", headers=outsider).json()[
            "templates"
        ]
    )
    assert (
        http.put(publication_path, json={"scope": "unit"}, headers=admin).status_code
        == 200
    )
    assert all(
        t["id"] != template_id
        for t in http.get("/api/quick-builder/templates", headers=outsider).json()[
            "templates"
        ]
    )


def test_plain_tool_response_credentials_are_masked():
    from app.services.quick_execution import mask

    result = mask(
        {
            "credential": "REVIEW_CANARY",
            "content": [
                {
                    "type": "text",
                    "text": "api_key=REVIEW_CANARY 密碼='two words' cookie=session-value",
                }
            ],
        }
    )
    assert "REVIEW_CANARY" not in str(result)
    assert "two words" not in str(result)
    assert "session-value" not in str(result)
