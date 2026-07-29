"""OW-1 message-history tree helpers.

單一查詢載入 (id, parent_id)，於 Python 組裝；不使用遞迴 SQL。
Idiom: ``app.services.department_tree`` (flat load + maps).
See docs/plans/ow1-message-tree-blueprint.md.

Revisit pagination when a conversation exceeds ~500 messages.
"""

from __future__ import annotations

from collections import defaultdict

from sqlalchemy.orm import Session

from app.models.message import Message


def load_edges(
    db: Session,
    conversation_id: int,
) -> list[tuple[int, int | None]]:
    """Flat ``(id, parent_id)`` ordered by ``(created_at, id)`` — no recursive SQL."""
    rows = (
        db.query(Message.id, Message.parent_id)
        .filter(Message.conversation_id == conversation_id)
        .order_by(Message.created_at, Message.id)
        .all()
    )
    return [(row.id, row.parent_id) for row in rows]


def children_map(
    edges: list[tuple[int, int | None]],
) -> dict[int | None, list[int]]:
    """parent_id → child ids in encounter order (stable ``(created_at, id)``)."""
    children: dict[int | None, list[int]] = defaultdict(list)
    for msg_id, parent_id in edges:
        children[parent_id].append(msg_id)
    return children


def sibling_groups(
    edges: list[tuple[int, int | None]],
) -> dict[int | None, list[int]]:
    """Same as :func:`children_map` — siblings share a parent_id (incl. NULL roots)."""
    return children_map(edges)


def active_path_ids(
    edges: list[tuple[int, int | None]],
    leaf_id: int | None,
) -> list[int]:
    """Root→leaf path by walking parents; cycle-safe. Empty if leaf missing/None."""
    if leaf_id is None:
        return []
    parent_of = {msg_id: parent_id for msg_id, parent_id in edges}
    if leaf_id not in parent_of:
        return []
    path: list[int] = []
    seen: set[int] = set()
    current: int | None = leaf_id
    while current is not None:
        if current in seen:
            break
        if current not in parent_of:
            break
        seen.add(current)
        path.append(current)
        current = parent_of[current]
    path.reverse()
    return path


def descendants(
    children: dict[int | None, list[int]],
    node_id: int,
    *,
    include_self: bool = True,
) -> set[int]:
    """Subtree under ``node_id``; cycle-safe via visited set."""
    result: set[int] = set()
    stack = list(children.get(node_id, []))
    while stack:
        current = stack.pop()
        if current in result:
            continue
        result.add(current)
        stack.extend(children.get(current, []))
    if include_self:
        result.add(node_id)
    return result


def newest_leaf_under(
    children: dict[int | None, list[int]],
    node_id: int,
) -> int:
    """Descend repeatedly to the newest child (last in ordered sibling list)."""
    current = node_id
    seen: set[int] = set()
    while True:
        kids = children.get(current, [])
        if not kids:
            return current
        nxt = kids[-1]
        if nxt in seen:
            return current
        seen.add(current)
        current = nxt
