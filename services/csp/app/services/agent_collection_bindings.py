# -*- coding: utf-8 -*-
"""P4.7 — agent ↔ collection many-to-many binding helpers.

Source of truth: ``agent_collection_bindings``.
Derived mirror: ``agents.bound_collection_id`` = min(ids) or NULL.

Request resolution: ``collection_ids`` wins when present; otherwise the
legacy single ``collection_id`` is expanded to a one-element set (or
empty when null). Never write both representations independently.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.agent import Agent, AgentCollectionBinding


def get_bound_collection_ids(agent: Agent) -> list[int]:
    """Sorted unique collection ids this agent may search.

    Prefers the junction table. Falls back to the legacy single column
    when the relationship is empty but the column is set (pre-migration
    rows / create_all without backfill) so readers never silently drop
    an existing binding.
    """
    bindings = getattr(agent, "collection_bindings", None)
    if bindings:
        return sorted({int(b.collection_id) for b in bindings})
    legacy = getattr(agent, "bound_collection_id", None)
    if legacy is not None:
        return [int(legacy)]
    return []


def resolve_requested_collection_ids(
    *,
    collection_ids: list[int] | None,
    collection_id: int | None,
    collection_ids_provided: bool,
    collection_id_provided: bool,
) -> list[int] | None:
    """Resolve the write-set from register/update request fields.

    Returns ``None`` when neither field was provided (update: leave
    unchanged). Returns a sorted unique list (possibly empty = unbound)
    when either field was provided. ``collection_ids`` wins when both
    are present so callers do not write two independent representations.
    """
    if collection_ids_provided:
        return sorted({int(x) for x in (collection_ids or [])})
    if collection_id_provided:
        if collection_id is None:
            return []
        return [int(collection_id)]
    return None


def set_bound_collection_ids(
    db: Session, agent: Agent, collection_ids: list[int]
) -> list[int]:
    """Replace the agent's bindings and sync the derived single column.

    Returns the sorted unique set that was written. Derived mirror is
    ``min(ids)`` (first of the sorted list) or NULL when unbound.
    """
    unique = sorted({int(x) for x in collection_ids})
    agent.collection_bindings.clear()
    db.flush()
    for cid in unique:
        agent.collection_bindings.append(AgentCollectionBinding(collection_id=cid))
    agent.bound_collection_id = unique[0] if unique else None
    return unique
