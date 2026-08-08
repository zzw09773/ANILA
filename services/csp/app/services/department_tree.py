"""部門三層樹查詢輔助（院 → 所 → 組）。

單一查詢載入 (id, parent_id)，於 Python 組裝；不使用遞迴 SQL。
P1.2 用量 rollup 會消費 ``get_descendant_ids``。
"""

from __future__ import annotations

from sqlalchemy import func, text
from sqlalchemy.orm import Session

from app.models.department import Department
from app.models.platform_setting import get_setting
from app.models.user import User


def max_depth(db: Session) -> int:
    """部門樹允許的最大層數(院=1、所=2、組=3)。

    可設定而非寫死:SYSTEM-MAP 定的是三層,但院內實際編制(例如處下設科)
    若需要第四層,從設定頁改即可,不必動程式碼與重跑審查。放寬只影響新建
    與 re-parent 的檢查,既有資料不受影響。

    **每次呼叫都真的解一次**(``limits.department_max_depth``:DB 那一列 →
    ``ANILA_DEPARTMENT_MAX_DEPTH`` → 程式預設 3)。以前讀的是 ``config.py``
    那個 import 期就凍結的 ``settings`` 物件 —— 那表示改完要 recreate 容器,
    而登錄表把這一顆宣告成不需重啟。宣告與現實的那道縫在這裡焊起來。
    """
    return int(get_setting(db, "limits.department_max_depth"))

# Transaction-scoped advisory lock for hierarchy mutations (create-with-parent,
# re-parent, activate/deactivate). Fixed key so concurrent writers serialize
# on the same lock; Postgres releases it at transaction end.
# 0xDE_071_7EE ≈ "DEPT_TREE" mnemonic (documented constant, not a secret).
DEPT_TREE_ADVISORY_LOCK_KEY = 0xDE_071_7EE


def acquire_dept_tree_lock(db: Session) -> None:
    """Acquire ``pg_advisory_xact_lock`` on PostgreSQL; no-op on other dialects."""
    bind = db.get_bind()
    if bind is None or bind.dialect.name != "postgresql":
        return
    db.execute(
        text("SELECT pg_advisory_xact_lock(:k)"),
        {"k": DEPT_TREE_ADVISORY_LOCK_KEY},
    )


def _load_edges(db: Session) -> list[tuple[int, int | None]]:
    rows = db.query(Department.id, Department.parent_id).all()
    return [(row.id, row.parent_id) for row in rows]


def _children_map(
    edges: list[tuple[int, int | None]],
) -> dict[int, list[int]]:
    children: dict[int, list[int]] = {}
    for dept_id, parent_id in edges:
        if parent_id is not None:
            children.setdefault(parent_id, []).append(dept_id)
    return children


def _descendants_from_children(
    children: dict[int, list[int]],
    dept_id: int,
    include_self: bool = False,
) -> set[int]:
    """Walk ``children`` map for one root; cycle-safe via visited set."""
    result: set[int] = set()
    stack = list(children.get(dept_id, []))
    while stack:
        current = stack.pop()
        if current in result:
            continue
        result.add(current)
        stack.extend(children.get(current, []))
    if include_self:
        result.add(dept_id)
    return result


def get_descendant_ids(
    db: Session,
    dept_id: int,
    include_self: bool = False,
) -> set[int]:
    """回傳 ``dept_id`` 的所有子孫 id（可選含自身）。"""
    children = _children_map(_load_edges(db))
    return _descendants_from_children(children, dept_id, include_self)


def get_descendant_ids_many(
    db: Session,
    dept_ids: set[int] | list[int],
    include_self: bool = False,
) -> set[int]:
    """Union of descendants for many roots; ONE ``_load_edges`` call."""
    if not dept_ids:
        return set()
    children = _children_map(_load_edges(db))
    out: set[int] = set()
    for dept_id in dept_ids:
        out |= _descendants_from_children(children, dept_id, include_self)
    return out


def get_depth(db: Session, dept_id: int) -> int:
    """節點深度：根（parent_id IS NULL）= 1。不存在則回 0。"""
    edges = {id_: parent_id for id_, parent_id in _load_edges(db)}
    if dept_id not in edges:
        return 0
    depth = 1
    current = dept_id
    seen: set[int] = set()
    while edges[current] is not None:
        parent = edges[current]
        if parent in seen or parent not in edges:
            break
        seen.add(current)
        current = parent
        depth += 1
    return depth


def depth_under_parent(db: Session, parent_id: int | None) -> int:
    """新節點（或搬移後）掛在 ``parent_id`` 下的深度；NULL → 根 = 1。"""
    if parent_id is None:
        return 1
    return get_depth(db, parent_id) + 1


def get_subtree_height(db: Session, dept_id: int) -> int:
    """子樹高度：葉 = 1。Cycle-safe via visited set (mirrors get_depth)."""
    children = _children_map(_load_edges(db))

    def _height(node_id: int, visited: set[int]) -> int:
        if node_id in visited:
            # Revisit contributes zero height so a cycle does not inflate
            # the depth gate and block the only repair (PUT parent_id=null).
            return 0
        visited.add(node_id)
        kids = children.get(node_id, [])
        if not kids:
            return 1
        return 1 + max(_height(kid, visited) for kid in kids)

    return _height(dept_id, set())


def build_tree(db: Session) -> list[dict]:
    """組裝整棵部門樹（根節點 list）；每個節點含 children。

    Cycle-tolerant: if parent pointers form a cycle, ``_sort`` stops
    descending on revisit so GET /tree never RecursionError.
    """
    rows = (
        db.query(
            Department,
            func.count(User.id).label("user_count"),
        )
        .outerjoin(User, User.department_id == Department.id)
        .group_by(Department.id)
        .all()
    )

    by_id: dict[int, dict] = {}
    for dept, user_count in rows:
        by_id[dept.id] = {
            "id": dept.id,
            "name": dept.name,
            "is_active": dept.is_active,
            "parent_id": dept.parent_id,
            "user_count": int(user_count or 0),
            "children": [],
        }

    roots: list[dict] = []
    for node in by_id.values():
        parent_id = node["parent_id"]
        parent = by_id.get(parent_id) if parent_id is not None else None
        if parent is None:
            roots.append(node)
        else:
            parent["children"].append(node)

    def _sort(nodes: list[dict], visited: set[int] | None = None) -> None:
        if visited is None:
            visited = set()
        nodes.sort(key=lambda n: (not n["is_active"], n["name"]))
        for child in nodes:
            if child["id"] in visited:
                child["children"] = []
                continue
            visited.add(child["id"])
            _sort(child["children"], visited)

    _sort(roots)
    return roots
