"""Unit tests for the S-Q1 / P4.7 agent csk- search scope guard.

These exercise ``_enforce_agent_collection_scope`` in isolation — the
security invariant that an agent service token may search ONLY the
collections the agent is bound to — without standing up the full app/DB
stack (the integration search path is env-flaky in this harness).
"""
import pytest
from fastapi import HTTPException

from app.api.ingestion.search import SearchPrincipal, _enforce_agent_collection_scope
from app.models.agent import AgentCollectionBinding


class _FakeAgent:
    def __init__(self, *, bound_collection_ids=None, bound_collection_id=None):
        ids = list(bound_collection_ids) if bound_collection_ids is not None else []
        self.collection_bindings = [
            AgentCollectionBinding(collection_id=cid) for cid in ids
        ]
        self.bound_collection_id = bound_collection_id


def test_agent_can_search_its_bound_collection():
    principal = SearchPrincipal(
        user=object(),
        agent=_FakeAgent(bound_collection_ids=[5], bound_collection_id=5),
    )
    _enforce_agent_collection_scope(principal, collection_id=5)


def test_agent_blocked_from_other_collection():
    principal = SearchPrincipal(
        user=object(),
        agent=_FakeAgent(bound_collection_ids=[5], bound_collection_id=5),
    )
    with pytest.raises(HTTPException) as exc:
        _enforce_agent_collection_scope(principal, collection_id=7)
    assert exc.value.status_code == 403


def test_unbound_agent_cannot_search_any_collection():
    # Empty bindings + null column = non-RAG agent → every collection off-limits.
    principal = SearchPrincipal(
        user=object(),
        agent=_FakeAgent(bound_collection_ids=[], bound_collection_id=None),
    )
    with pytest.raises(HTTPException) as exc:
        _enforce_agent_collection_scope(principal, collection_id=5)
    assert exc.value.status_code == 403


def test_user_principal_is_unaffected():
    principal = SearchPrincipal(user=object(), agent=None)
    _enforce_agent_collection_scope(principal, collection_id=5)
    _enforce_agent_collection_scope(principal, collection_id=999)


def test_agent_with_several_collections_may_search_each():
    principal = SearchPrincipal(
        user=object(),
        agent=_FakeAgent(bound_collection_ids=[2, 7], bound_collection_id=2),
    )
    _enforce_agent_collection_scope(principal, collection_id=2)
    _enforce_agent_collection_scope(principal, collection_id=7)
    with pytest.raises(HTTPException):
        _enforce_agent_collection_scope(principal, collection_id=3)
