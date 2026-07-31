"""部門名稱唯一性以母單位為界 + 人員可掛在任何一層（r1_0026）。

院內實際編制是 院部 → 研究所／中心 → 組／科，而且人不是全在葉節點：
有人直屬院部，有人掛在所而所底下沒有組。原本 ``departments.name`` 是全域
UNIQUE，兩個所各有一個「企劃組」就會被擋掉。

本檔釘住四件事：
1. 同名不同父可以並存，同名同父要擋，兩個同名根節點也要擋（API 層）。
2. 上面那條在資料庫層也成立（偏唯一索引，繞過 API 直接寫也擋得住）。
3. 使用者可以綁在根節點與中間層，且 get_descendant_ids 對兩者都算對範圍。
4. 建到第四層時的 400 訊息是人看得懂的繁中，並且點名實際層級稱呼。
"""
from __future__ import annotations

import os

os.environ.setdefault("ANILA_ALLOW_DEV_SECRET", "1")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from app.models.department import Department
from app.services.department_tree import get_descendant_ids
from tests.conftest import login, make_user


@pytest.fixture(autouse=True)
def _bypass_dev_secret_gate(monkeypatch):
    import app.services.startup_security as ss_module

    monkeypatch.setattr(ss_module, "assert_no_dev_defaults", lambda: None)


def _auth_headers(client: TestClient, db, username="dept_name_admin") -> dict:
    make_user(db, username=username, role="admin")
    return {"Authorization": f"Bearer {login(client, username=username)}"}


def _create(client: TestClient, headers: dict, name: str, parent_id: int | None = None):
    body: dict = {"name": name}
    if parent_id is not None:
        body["parent_id"] = parent_id
    return client.post("/api/departments", json=body, headers=headers)


# ── 1. 同名不同父 OK；同名同父 / 同名根節點要擋 ────────────────────────────


def test_same_name_under_different_parents_is_allowed(client: TestClient, db):
    """兩個所各有一個「企劃組」是院內常態，不可以被擋掉。"""
    headers = _auth_headers(client, db)
    hq = _create(client, headers, "院部").json()
    aero = _create(client, headers, "航空研究所", parent_id=hq["id"]).json()
    ship = _create(client, headers, "船艦研究所", parent_id=hq["id"]).json()

    first = _create(client, headers, "企劃組", parent_id=aero["id"])
    second = _create(client, headers, "企劃組", parent_id=ship["id"])

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert first.json()["id"] != second.json()["id"]
    assert first.json()["parent_id"] == aero["id"]
    assert second.json()["parent_id"] == ship["id"]


def test_same_name_under_same_parent_is_rejected(client: TestClient, db):
    headers = _auth_headers(client, db, username="dept_name_dup_sib")
    hq = _create(client, headers, "院部").json()
    aero = _create(client, headers, "航空研究所", parent_id=hq["id"]).json()
    assert _create(client, headers, "企劃組", parent_id=aero["id"]).status_code == 200

    dup = _create(client, headers, "企劃組", parent_id=aero["id"])
    assert dup.status_code == 400, dup.text
    detail = dup.json()["detail"]
    # 訊息要點名是哪個母單位撞名，操作者才知道去哪裡看
    assert "航空研究所" in detail
    assert "企劃組" in detail


def test_two_roots_with_same_name_are_rejected(client: TestClient, db):
    """PostgreSQL 的 UNIQUE 視 NULL 兩兩不相等，根層要另外擋。"""
    headers = _auth_headers(client, db, username="dept_name_dup_root")
    assert _create(client, headers, "院部").status_code == 200

    dup = _create(client, headers, "院部")
    assert dup.status_code == 400, dup.text
    assert "院部" in dup.json()["detail"]


def test_reparent_into_a_colliding_name_is_rejected(client: TestClient, db):
    """改掛也會撞名：名稱沒變，但新的母單位底下已經有同名的了。"""
    headers = _auth_headers(client, db, username="dept_name_reparent")
    hq = _create(client, headers, "院部").json()
    aero = _create(client, headers, "航空研究所", parent_id=hq["id"]).json()
    ship = _create(client, headers, "船艦研究所", parent_id=hq["id"]).json()
    assert _create(client, headers, "企劃組", parent_id=aero["id"]).status_code == 200
    movable = _create(client, headers, "企劃組", parent_id=ship["id"]).json()

    collide = client.put(
        f"/api/departments/{movable['id']}",
        json={"parent_id": aero["id"]},
        headers=headers,
    )
    assert collide.status_code == 400, collide.text
    assert "航空研究所" in collide.json()["detail"]

    # 未改寫
    listed = {d["id"]: d for d in client.get("/api/departments", headers=headers).json()}
    assert listed[movable["id"]]["parent_id"] == ship["id"]


def test_rename_to_a_sibling_name_is_rejected_but_moving_house_is_fine(
    client: TestClient, db
):
    headers = _auth_headers(client, db, username="dept_name_rename")
    hq = _create(client, headers, "院部").json()
    aero = _create(client, headers, "航空研究所", parent_id=hq["id"]).json()
    ship = _create(client, headers, "船艦研究所", parent_id=hq["id"]).json()
    _create(client, headers, "企劃組", parent_id=aero["id"])
    other = _create(client, headers, "測試組", parent_id=aero["id"]).json()

    clash = client.put(
        f"/api/departments/{other['id']}",
        json={"name": "企劃組"},
        headers=headers,
    )
    assert clash.status_code == 400, clash.text

    # 同樣的名字換到別的所底下就沒問題
    ok = client.put(
        f"/api/departments/{other['id']}",
        json={"name": "企劃組", "parent_id": ship["id"]},
        headers=headers,
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["name"] == "企劃組"
    assert ok.json()["parent_id"] == ship["id"]


# ── 2. 資料庫層的偏唯一索引（繞過 API 也要擋得住）──────────────────────────


def test_db_index_allows_same_name_under_different_parents(db):
    hq = Department(name="院部", parent_id=None, is_active=True)
    db.add(hq)
    db.commit()
    db.refresh(hq)
    aero = Department(name="航空研究所", parent_id=hq.id, is_active=True)
    ship = Department(name="船艦研究所", parent_id=hq.id, is_active=True)
    db.add_all([aero, ship])
    db.commit()
    db.refresh(aero)
    db.refresh(ship)

    db.add_all(
        [
            Department(name="企劃組", parent_id=aero.id, is_active=True),
            Department(name="企劃組", parent_id=ship.id, is_active=True),
        ]
    )
    db.commit()  # 不可以拋 IntegrityError
    assert db.query(Department).filter(Department.name == "企劃組").count() == 2


def test_db_index_rejects_same_name_under_same_parent(db):
    hq = Department(name="院部", parent_id=None, is_active=True)
    db.add(hq)
    db.commit()
    db.refresh(hq)

    db.add(Department(name="企劃組", parent_id=hq.id, is_active=True))
    db.commit()
    db.add(Department(name="企劃組", parent_id=hq.id, is_active=True))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_db_index_rejects_two_roots_with_same_name(db):
    """uq_departments_root_name：少了它，NULL != NULL 會放行兩個「院部」。"""
    db.add(Department(name="院部", parent_id=None, is_active=True))
    db.commit()
    db.add(Department(name="院部", parent_id=None, is_active=True))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


# ── 3. 人可以掛在任何一層，範圍計算對每一層都要對 ─────────────────────────


def test_user_can_bind_to_root_and_mid_level_and_scoping_is_correct(
    client: TestClient, db
):
    """有人直屬院部、有人掛在所（底下還有組），兩種都要能綁且範圍要對。"""
    headers = _auth_headers(client, db, username="dept_bind_admin")
    hq = _create(client, headers, "院部").json()
    aero = _create(client, headers, "航空研究所", parent_id=hq["id"]).json()
    aerodyn = _create(client, headers, "氣動組", parent_id=aero["id"]).json()

    hq_person = make_user(db, username="hq_staff", role="user")
    inst_person = make_user(db, username="inst_staff", role="user")
    section_person = make_user(db, username="section_staff", role="user")
    hq_person.department_id = hq["id"]
    inst_person.department_id = aero["id"]
    section_person.department_id = aerodyn["id"]
    db.commit()
    for person in (hq_person, inst_person, section_person):
        db.refresh(person)

    # 綁定真的寫進去了（含直屬院部與直屬所這兩個擁有者點名的情境）
    assert hq_person.department_id == hq["id"]
    assert inst_person.department_id == aero["id"]
    assert section_person.department_id == aerodyn["id"]

    # 院部的範圍要涵蓋所與組；所的範圍只涵蓋自己與底下的組
    assert get_descendant_ids(db, hq["id"], include_self=True) == {
        hq["id"],
        aero["id"],
        aerodyn["id"],
    }
    assert get_descendant_ids(db, aero["id"], include_self=True) == {
        aero["id"],
        aerodyn["id"],
    }
    assert get_descendant_ids(db, aerodyn["id"], include_self=True) == {aerodyn["id"]}
    # 不含自身時，掛在葉節點的人所屬單位底下是空的
    assert get_descendant_ids(db, aerodyn["id"]) == set()


def test_institute_with_no_sections_is_a_valid_home(client: TestClient, db):
    """所底下沒有組時，人就掛在所上；範圍只有它自己。"""
    headers = _auth_headers(client, db, username="dept_leaf_inst")
    hq = _create(client, headers, "院部").json()
    lone = _create(client, headers, "資訊中心", parent_id=hq["id"]).json()

    person = make_user(db, username="lone_staff", role="user")
    person.department_id = lone["id"]
    db.commit()
    db.refresh(person)

    assert person.department_id == lone["id"]
    assert get_descendant_ids(db, lone["id"], include_self=True) == {lone["id"]}


# ── 4. 三層建得起來，第四層的 400 訊息要是人話 ────────────────────────────


def test_three_real_levels_build_and_fourth_is_rejected_readably(
    client: TestClient, db
):
    headers = _auth_headers(client, db, username="dept_depth_msg")
    hq = _create(client, headers, "院部")
    assert hq.status_code == 200, hq.text
    aero = _create(client, headers, "航空研究所", parent_id=hq.json()["id"])
    assert aero.status_code == 200, aero.text
    aerodyn = _create(client, headers, "氣動組", parent_id=aero.json()["id"])
    assert aerodyn.status_code == 200, aerodyn.text

    fourth = _create(client, headers, "第四層小組", parent_id=aerodyn.json()["id"])
    assert fourth.status_code == 400, fourth.text
    detail = fourth.json()["detail"]
    # 說得出上限、說得出實際層級稱呼、說得出是誰擋住的、還告訴操作者怎麼辦
    assert "3 層" in detail
    assert "院部" in detail
    assert "研究所／中心" in detail
    assert "組／科" in detail
    assert "氣動組" in detail
    assert "同一層" in detail
    # 不可以再吐出舊的機器味字串
    assert "院→所→組" not in detail
