"""知識庫的單位範圍。讀取當下用部門樹判斷，不在調單位時改寫授權列。

沒有 department_id 的庫（含 anilalm 個人庫）只有擁有者能讀、能寫，不回填單位。
有 department_id 的庫只看使用者現在的單位（含下級）。擁有者調走之後
立刻不能再讀、也不能寫；列留在原單位，由管理員移交。移交後的新擁有者才能寫。
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.ingestion import IngestionCollection
from app.models.user import User
from app.services.auth_service import is_admin_tier
from app.services.department_tree import (
    ancestor_ids_from_parent_map,
    get_ancestor_ids,
    load_parent_map,
)


def department_scope_contains(
    db: Session,
    collection_department_id: int | None,
    user_department_id: int | None,
    *,
    parent_of: dict[int, int | None] | None = None,
) -> bool:
    """使用者的單位是不是落在這個知識庫的單位（含下級）裡。"""
    if collection_department_id is None or user_department_id is None:
        return False
    if parent_of is None:
        ancestors = get_ancestor_ids(db, user_department_id, include_self=True)
    else:
        ancestors = ancestor_ids_from_parent_map(
            parent_of, user_department_id, include_self=True
        )
    return collection_department_id in ancestors


def owner_left_unit(
    db: Session,
    coll: IngestionCollection,
    owner: User | None,
    *,
    parent_of: dict[int, int | None] | None = None,
) -> bool:
    """單位知識庫的擁有者已經不在這個單位（含下級）。"""
    if coll.department_id is None:
        return False
    if owner is None or owner.department_id is None:
        return True
    return not department_scope_contains(
        db,
        coll.department_id,
        owner.department_id,
        parent_of=parent_of,
    )


def can_read_collection(db: Session, user: User, coll: IngestionCollection) -> bool:
    if is_admin_tier(user):
        return True
    if coll.department_id is None:
        return coll.created_by == user.id
    return department_scope_contains(db, coll.department_id, user.department_id)


def can_write_collection(db: Session, user: User, coll: IngestionCollection) -> bool:
    """寫入只限管理員，或仍在這個庫單位範圍內的建立者。

    沒有單位的庫，建立者一直能寫。建立者調離單位範圍後不能寫，也不能刪，
    要等管理員移交給還在範圍內的人。
    """
    if is_admin_tier(user):
        return True
    if coll.created_by != user.id:
        return False
    if coll.department_id is None:
        return True
    return department_scope_contains(db, coll.department_id, user.department_id)


def select_visible_collections(
    db: Session,
    user: User,
    rows: list[IngestionCollection],
) -> tuple[list[IngestionCollection], dict[int, User], dict[int, int | None]]:
    """預設清單。管理員多看到「原擁有者已調離」的單位庫，避免被「只看自己的」藏住。

    回傳可見的庫、這批擁有者、部門父節點。清單端點用同一份對照，不再逐列查擁有者。
    """
    if not rows:
        return [], {}, {}
    owners = _owners(db, rows)
    parent_of = load_parent_map(db)
    visible: list[IngestionCollection] = []
    for coll in rows:
        if is_admin_tier(user):
            if coll.created_by == user.id or owner_left_unit(
                db, coll, owners.get(coll.created_by), parent_of=parent_of
            ):
                visible.append(coll)
            continue
        if coll.department_id is None:
            if coll.created_by == user.id:
                visible.append(coll)
            continue
        if department_scope_contains(
            db, coll.department_id, user.department_id, parent_of=parent_of
        ):
            visible.append(coll)
    return visible, owners, parent_of


def load_owners(db: Session, rows: list[IngestionCollection]) -> dict[int, User]:
    """一次查出這批知識庫的擁有者。"""
    return _owners(db, rows)


def annotate_owner_left(db: Session, coll: IngestionCollection) -> bool:
    owner = None
    if coll.created_by is not None:
        owner = db.query(User).filter(User.id == coll.created_by).first()
    return owner_left_unit(db, coll, owner)


def _owners(db: Session, rows: list[IngestionCollection]) -> dict[int, User]:
    ids = {row.created_by for row in rows if row.created_by is not None}
    if not ids:
        return {}
    found = db.query(User).filter(User.id.in_(ids)).all()
    return {user.id: user for user in found}
