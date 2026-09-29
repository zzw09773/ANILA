"""調單位之後，單位範圍的權限立刻跟著走。自己的對話留在原帳號。"""
from __future__ import annotations

from app.models.conversation import Conversation, ConversationShare
from app.models.department import Department
from app.models.ingestion import IngestionCollection, IngestionDocument
from app.models.registered_service import RegisteredService
from app.models.service_access_grant import ServiceAccessGrant
from app.services.access_control import can_access_service
from app.services.conversation_service import user_has_active_share
from tests.conftest import login, make_user


def _dept(db, name, parent_id=None) -> Department:
    row = Department(name=name, parent_id=parent_id, is_active=True)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _collection(db, *, owner, department, name, origin="csp") -> IngestionCollection:
    row = IngestionCollection(
        name=name,
        chunking_config={"strategy": "fixed", "params": {}},
        embedding_model="test-embed",
        embedding_dim=8,
        created_by=owner.id,
        department_id=department.id if department is not None else None,
        origin=origin,
        status="active",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _headers(client, username):
    return {"Authorization": f"Bearer {login(client, username)}"}


def test_department_change_hides_old_unit_collections_and_keeps_conversations(
    client, db, monkeypatch
):
    unit_a = _dept(db, "甲單位")
    unit_b = _dept(db, "乙單位")
    child = _dept(db, "甲下級", parent_id=unit_a.id)
    mover = make_user(db, username="mover", department_id=unit_a.id)
    colleague = make_user(db, username="colleague", department_id=unit_a.id)
    junior = make_user(db, username="junior", department_id=child.id)
    unit_library = _collection(db, owner=colleague, department=unit_a, name="甲單位規章")
    own_library = _collection(db, owner=mover, department=unit_a, name="甲單位自己的庫")
    personal = _collection(db, owner=mover, department=None, name="個人筆記", origin="anilalm")
    personal.department_id = None
    db.commit()

    mine = Conversation(user_id=mover.id, title="我的對話")
    db.add(mine)
    db.commit()

    service = RegisteredService(
        name="甲單位系統",
        slug="unit-a-tool",
        entry_url="http://example.invalid/unit-a",
        is_public=False,
        is_active=True,
    )
    db.add(service)
    db.commit()
    db.add(ServiceAccessGrant(department_id=unit_a.id, service_id=service.id))
    shared = Conversation(user_id=colleague.id, title="單位分享")
    db.add(shared)
    db.commit()
    db.add(
        ConversationShare(
            conversation_id=shared.id,
            target_department_id=unit_a.id,
            created_by=colleague.id,
        )
    )
    db.commit()

    assert can_access_service(db, mover, service) is True
    assert user_has_active_share(db, shared, mover) is True
    seen = client.get(
        "/api/ingestion/collections",
        headers=_headers(client, "mover"),
    )
    assert seen.status_code == 200, seen.text
    names = {row["name"] for row in seen.json()}
    assert "甲單位規章" in names
    assert "甲單位自己的庫" in names
    assert "個人筆記" in names
    junior_seen = client.get(
        "/api/ingestion/collections",
        headers=_headers(client, "junior"),
    )
    assert "甲單位規章" in {row["name"] for row in junior_seen.json()}

    called = []
    monkeypatch.setattr(
        "app.api.ingestion.search.scope_collection_rls",
        lambda *args, **kwargs: called.append(args),
    )

    mover.department_id = unit_b.id
    db.commit()

    assert can_access_service(db, mover, service) is False
    grant = db.query(ServiceAccessGrant).one()
    assert grant.department_id == unit_a.id
    assert user_has_active_share(db, shared, mover) is False
    db.refresh(mine)
    assert mine.user_id == mover.id
    assert mine.title == "我的對話"

    hidden = client.get(
        f"/api/ingestion/collections/{unit_library.id}",
        headers=_headers(client, "mover"),
    )
    assert hidden.status_code == 403, hidden.text
    search = client.post(
        f"/api/ingestion/collections/{unit_library.id}/search",
        headers=_headers(client, "mover"),
        json={"query": "規章"},
    )
    assert search.status_code == 403, search.text
    assert called == []

    own_hidden = client.get(
        f"/api/ingestion/collections/{own_library.id}",
        headers=_headers(client, "mover"),
    )
    assert own_hidden.status_code == 403, own_hidden.text
    own_search = client.post(
        f"/api/ingestion/collections/{own_library.id}/search",
        headers=_headers(client, "mover"),
        json={"query": "規章"},
    )
    assert own_search.status_code == 403, own_search.text

    still = client.get(
        "/api/ingestion/collections",
        headers=_headers(client, "mover"),
    )
    still_names = {row["name"] for row in still.json()}
    assert "甲單位規章" not in still_names
    assert "甲單位自己的庫" not in still_names
    assert "個人筆記" in still_names
    personal_still = client.get(
        f"/api/ingestion/collections/{personal.id}",
        headers=_headers(client, "mover"),
    )
    assert personal_still.status_code == 200, personal_still.text
    junior_still = client.get(
        f"/api/ingestion/collections/{unit_library.id}",
        headers=_headers(client, "junior"),
    )
    assert junior_still.status_code == 200, junior_still.text


def test_departed_owner_is_listed_and_transfer_keeps_the_unit(client, db):
    unit_a = _dept(db, "原單位")
    unit_b = _dept(db, "新單位")
    owner = make_user(db, username="library-owner", department_id=unit_a.id)
    admin = make_user(db, username="library-admin", role="admin")
    successor = make_user(db, username="successor", department_id=unit_a.id)
    coll = _collection(db, owner=owner, department=unit_a, name="要移交的庫")
    owner.department_id = unit_b.id
    db.commit()

    listed = client.get(
        "/api/ingestion/collections",
        headers=_headers(client, "library-admin"),
    )
    assert listed.status_code == 200, listed.text
    match = next(row for row in listed.json() if row["id"] == coll.id)
    assert match["owner_left_unit"] is True
    assert match["department_id"] == unit_a.id

    moved = client.post(
        f"/api/ingestion/collections/{coll.id}/transfer",
        headers=_headers(client, "library-admin"),
        json={"username": "successor"},
    )
    assert moved.status_code == 200, moved.text
    body = moved.json()
    assert body["created_by"] == successor.id
    assert body["department_id"] == unit_a.id
    assert body["owner_left_unit"] is False


def test_new_csp_collection_snapshots_department_and_anilalm_does_not(client, db):
    unit = _dept(db, "快照單位")
    make_user(db, username="maker", department_id=unit.id)
    headers = _headers(client, "maker")
    payload = {
        "name": "單位新庫",
        "chunking_config": {"strategy": "fixed", "params": {}},
        "embedding_dim": 4000,
        "origin": "csp",
    }
    created = client.post("/api/ingestion/collections", headers=headers, json=payload)
    assert created.status_code == 201, created.text
    assert created.json()["department_id"] == unit.id

    personal = client.post(
        "/api/ingestion/collections",
        headers=headers,
        json={**payload, "name": "個人新庫", "origin": "anilalm"},
    )
    assert personal.status_code == 201, personal.text
    assert personal.json()["department_id"] is None


def test_list_collections_documents_same_unit_visibility():
    import inspect

    from app.api.ingestion.collections import list_collections

    text = inspect.getdoc(list_collections) or ""
    assert "同單位" in text
    assert "擁有者" in text
    assert "原擁有者已調離" in text
    assert "另外看得到" not in text
    signature = inspect.getsource(list_collections).split('"""', 1)[0]
    assert "原擁有者已調離" in signature
    assert "全部" in signature


def test_creator_who_left_the_unit_loses_write_until_an_admin_transfers(client, db):
    """調離單位範圍的建立者不能再寫或刪。沒有單位的庫仍由建立者寫。

    移交之後，新的擁有者可以寫；原建立者不行。管理員隨時可以寫。
    """
    unit = _dept(db, "原編制")
    elsewhere = _dept(db, "調去的單位")
    owner = make_user(db, username="moved-owner", department_id=unit.id)
    make_user(db, username="scope-admin", role="admin")
    successor = make_user(db, username="new-owner", department_id=unit.id)
    coll = _collection(db, owner=owner, department=unit, name="單位庫")
    personal = _collection(
        db, owner=owner, department=None, name="個人庫", origin="anilalm",
    )
    doc = IngestionDocument(
        collection_id=coll.id,
        filename="stay.txt",
        sha256="c" * 64,
        status="indexed",
        classification_level="無機密",
    )
    db.add(doc)
    db.commit()
    owner.department_id = elsewhere.id
    db.commit()
    owner_h = _headers(client, "moved-owner")
    admin_h = _headers(client, "scope-admin")

    assert client.patch(
        f"/api/ingestion/collections/{coll.id}",
        headers=owner_h,
        json={"name": "調走的人不能改"},
    ).status_code == 403
    assert client.delete(
        f"/api/ingestion/documents/{doc.id}",
        headers=owner_h,
    ).status_code == 403
    assert client.post(
        f"/api/ingestion/collections/{coll.id}/documents",
        headers=owner_h,
        files={"file": ("note.txt", b"no", "text/plain")},
    ).status_code == 403
    assert client.delete(
        f"/api/ingestion/collections/{coll.id}",
        headers=owner_h,
    ).status_code == 403

    personal_edit = client.patch(
        f"/api/ingestion/collections/{personal.id}",
        headers=owner_h,
        json={"name": "個人庫仍可改"},
    )
    assert personal_edit.status_code == 200, personal_edit.text
    assert personal_edit.json()["name"] == "個人庫仍可改"

    admin_edit = client.patch(
        f"/api/ingestion/collections/{coll.id}",
        headers=admin_h,
        json={"name": "管理員改的"},
    )
    assert admin_edit.status_code == 200, admin_edit.text

    transferred = client.post(
        f"/api/ingestion/collections/{coll.id}/transfer",
        headers=admin_h,
        json={"username": "new-owner"},
    )
    assert transferred.status_code == 200, transferred.text
    successor_edit = client.patch(
        f"/api/ingestion/collections/{coll.id}",
        headers=_headers(client, "new-owner"),
        json={"name": "新擁有者改的"},
    )
    assert successor_edit.status_code == 200, successor_edit.text
    still_blocked = client.patch(
        f"/api/ingestion/collections/{coll.id}",
        headers=owner_h,
        json={"name": "原建立者仍不行"},
    )
    assert still_blocked.status_code == 403, still_blocked.text
    db.expire_all()
    stored = db.get(IngestionCollection, coll.id)
    assert stored.name == "新擁有者改的"
    assert stored.created_by == successor.id
    assert db.get(IngestionDocument, doc.id) is not None


def test_same_unit_member_can_read_documents_but_not_change_them(client, db):
    """同單位（含下級）可以讀庫裡的文件，不能上傳或刪除。"""
    unit = _dept(db, "讀寫單位")
    child = _dept(db, "讀寫下級", parent_id=unit.id)
    owner = make_user(db, username="lib-owner", department_id=unit.id)
    member = make_user(db, username="lib-member", department_id=unit.id)
    junior = make_user(db, username="lib-junior", department_id=child.id)
    coll = _collection(db, owner=owner, department=unit, name="單位規章")
    doc = IngestionDocument(
        collection_id=coll.id,
        filename="rule.txt",
        sha256="b" * 64,
        status="indexed",
        classification_level="無機密",
    )
    db.add(doc)
    db.commit()

    listed = client.get(
        f"/api/ingestion/collections/{coll.id}/documents",
        headers=_headers(client, "lib-member"),
    )
    assert listed.status_code == 200, listed.text
    assert listed.json()[0]["filename"] == "rule.txt"
    child_listed = client.get(
        f"/api/ingestion/collections/{coll.id}/documents",
        headers=_headers(client, "lib-junior"),
    )
    assert child_listed.status_code == 200, child_listed.text

    removed = client.delete(
        f"/api/ingestion/documents/{doc.id}",
        headers=_headers(client, "lib-member"),
    )
    assert removed.status_code == 403, removed.text
    uploaded = client.post(
        f"/api/ingestion/collections/{coll.id}/documents",
        headers=_headers(client, "lib-member"),
        files={"file": ("note.txt", b"hello", "text/plain")},
    )
    assert uploaded.status_code == 403, uploaded.text
    patched = client.patch(
        f"/api/ingestion/collections/{coll.id}",
        headers=_headers(client, "lib-member"),
        json={"name": "同單位不能改"},
    )
    assert patched.status_code == 403, patched.text
    reprocessed = client.post(
        f"/api/ingestion/documents/{doc.id}/reprocess",
        headers=_headers(client, "lib-member"),
    )
    assert reprocessed.status_code == 403, reprocessed.text
    db.refresh(doc)
    assert doc.filename == "rule.txt"
    db.refresh(coll)
    assert coll.name == "單位規章"


def test_transfer_refuses_an_owner_outside_the_department_scope(client, db):
    unit_a = _dept(db, "移交甲")
    unit_b = _dept(db, "移交乙")
    child = _dept(db, "移交甲下", parent_id=unit_a.id)
    owner = make_user(db, username="transfer-owner", department_id=unit_a.id)
    make_user(db, username="outsider", department_id=unit_b.id)
    make_user(db, username="junior-owner", department_id=child.id)
    make_user(db, username="transfer-admin", role="admin")
    coll = _collection(db, owner=owner, department=unit_a, name="移交庫")
    admin = _headers(client, "transfer-admin")

    refused = client.post(
        f"/api/ingestion/collections/{coll.id}/transfer",
        headers=admin,
        json={"username": "outsider"},
    )
    assert refused.status_code == 409, refused.text
    assert "單位" in refused.json()["detail"]
    stayed = client.get(f"/api/ingestion/collections/{coll.id}", headers=admin)
    assert stayed.json()["created_by"] == owner.id

    allowed = client.post(
        f"/api/ingestion/collections/{coll.id}/transfer",
        headers=admin,
        json={"username": "junior-owner"},
    )
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["created_by"] != owner.id
    assert allowed.json()["department_id"] == unit_a.id

    personal = _collection(
        db, owner=owner, department=None, name="個人移交", origin="anilalm",
    )
    personal.department_id = None
    db.commit()
    moved = client.post(
        f"/api/ingestion/collections/{personal.id}/transfer",
        headers=admin,
        json={"username": "outsider"},
    )
    assert moved.status_code == 200, moved.text

    unapproved = make_user(
        db, username="transfer-pending", department_id=unit_a.id, is_approved=False,
    )
    refused_pending = client.post(
        f"/api/ingestion/collections/{coll.id}/transfer",
        headers=admin,
        json={"username": "transfer-pending"},
    )
    assert refused_pending.status_code == 404, refused_pending.text
    stayed_pending = client.get(f"/api/ingestion/collections/{coll.id}", headers=admin)
    assert stayed_pending.json()["created_by"] != unapproved.id

    sleeping = make_user(db, username="transfer-inactive", department_id=unit_a.id)
    sleeping.is_active = False
    db.commit()
    refused_inactive = client.post(
        f"/api/ingestion/collections/{coll.id}/transfer",
        headers=admin,
        json={"username": "transfer-inactive"},
    )
    assert refused_inactive.status_code == 404, refused_inactive.text
    stayed_inactive = client.get(f"/api/ingestion/collections/{coll.id}", headers=admin)
    assert stayed_inactive.json()["created_by"] != sleeping.id


def _post_collection(client, headers, name, **extra):
    body = {
        "name": name,
        "chunking_config": {"strategy": "fixed", "params": {"size": 256}},
        "classification_level": "無機密",
        "origin": "csp",
    }
    body.update(extra)
    return client.post("/api/ingestion/collections", headers=headers, json=body)


def test_admin_chooses_institute_or_department_on_create_and_others_cannot(client, db):
    """管理員建庫可以選全院（department_id null）或某個單位。
    沒送這個欄位時仍用建立者自己的單位。非管理員不能改範圍。
    """
    home = _dept(db, "建立者的單位")
    other = _dept(db, "指定的單位")
    retired = _dept(db, "停用的單位")
    retired.is_active = False
    db.commit()
    make_user(db, username="scope-admin", role="admin", department_id=home.id)
    make_user(db, username="scope-dev", role="developer", department_id=home.id)
    admin = _headers(client, "scope-admin")
    dev = _headers(client, "scope-dev")

    omitted = _post_collection(client, admin, "沿用自己的單位")
    assert omitted.status_code == 201, omitted.text
    assert omitted.json()["department_id"] == home.id

    institute = _post_collection(client, admin, "全院庫", department_id=None)
    assert institute.status_code == 201, institute.text
    assert institute.json()["department_id"] is None

    chosen = _post_collection(client, admin, "指定單位庫", department_id=other.id)
    assert chosen.status_code == 201, chosen.text
    assert chosen.json()["department_id"] == other.id

    dead = _post_collection(client, admin, "停用單位庫", department_id=retired.id)
    assert dead.status_code == 400, dead.text

    own = _post_collection(client, dev, "開發者的庫")
    assert own.status_code == 201, own.text
    assert own.json()["department_id"] == home.id
    refused = _post_collection(client, dev, "開發者想建全院", department_id=None)
    assert refused.status_code == 403, refused.text
    echoed = _post_collection(client, dev, "送出自己的單位", department_id=home.id)
    assert echoed.status_code == 201, echoed.text
    assert echoed.json()["department_id"] == home.id
    other_unit = _post_collection(client, dev, "指定別的單位", department_id=other.id)
    assert other_unit.status_code == 403, other_unit.text


def test_collection_update_refuses_the_mix_before_writing():
    from pathlib import Path

    text = (
        Path(__file__).resolve().parents[1] / "app" / "api" / "ingestion" / "collections.py"
    ).read_text()
    # 定義一次、開標記時一次、PATCH 結果態一次。套用之後不再呼叫第二次。
    assert text.count("_refuse_institutional_department_mix(") == 3


def test_collection_list_loads_owners_once(client, db, db_engine):
    from sqlalchemy import event

    unit = _dept(db, "清單單位")
    admin = make_user(db, username="list-admin", role="admin", department_id=unit.id)
    for index in range(3):
        owner = make_user(db, username=f"list-owner-{index}", department_id=None)
        _collection(db, owner=owner, department=unit, name=f"清單庫{index}")
    statements: list[str] = []

    def _capture(conn, cursor, statement, parameters, context, executemany):
        compact = " ".join(statement.split()).lower()
        if " from users" in compact:
            statements.append(compact)

    event.listen(db_engine, "before_cursor_execute", _capture)
    try:
        listed = client.get(
            "/api/ingestion/collections",
            headers=_headers(client, admin.username),
        )
    finally:
        event.remove(db_engine, "before_cursor_execute", _capture)
    assert listed.status_code == 200, listed.text
    assert len(listed.json()) >= 3
    point_lookups = [
        sql for sql in statements
        if "users.id" in sql and " in " not in sql and " in(" not in sql
    ]
    assert len(point_lookups) <= 2, point_lookups


def test_visible_collections_load_the_parent_map_once(db, monkeypatch):
    from app.services import collection_scope, department_tree

    unit = _dept(db, "樹一次")
    child = _dept(db, "樹一次下級", parent_id=unit.id)
    owner = make_user(db, username="tree-owner", department_id=unit.id)
    viewer = make_user(db, username="tree-viewer", department_id=child.id)
    rows = [
        _collection(db, owner=owner, department=unit, name=f"樹庫{index}")
        for index in range(3)
    ]
    calls = {"n": 0}
    real = department_tree._load_edges

    def _counting(session):
        calls["n"] += 1
        return real(session)

    monkeypatch.setattr(department_tree, "_load_edges", _counting)
    visible, _owners, _parent_of = collection_scope.select_visible_collections(db, viewer, rows)
    assert [row.name for row in visible] == ["樹庫0", "樹庫1", "樹庫2"]
    assert calls["n"] == 1
