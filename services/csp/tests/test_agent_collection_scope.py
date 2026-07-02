"""Unit tests for the S-Q1 agent csk- search scope guard.

These exercise ``_enforce_agent_collection_scope`` in isolation — the
security invariant that an agent service token may search ONLY the single
collection the agent is bound to — without standing up the full app/DB
stack (the integration search path is env-flaky in this harness).
"""
import pytest
from fastapi import HTTPException

from app.api.ingestion.search import SearchPrincipal, _enforce_agent_collection_scope


class _FakeAgent:
    def __init__(self, bound_collection_id):
        self.bound_collection_id = bound_collection_id


def test_agent_can_search_its_bound_collection():
    principal = SearchPrincipal(user=object(), agent=_FakeAgent(bound_collection_id=5))
    # No exception → allowed.
    _enforce_agent_collection_scope(principal, collection_id=5)


def test_agent_blocked_from_other_collection():
    principal = SearchPrincipal(user=object(), agent=_FakeAgent(bound_collection_id=5))
    with pytest.raises(HTTPException) as exc:
        _enforce_agent_collection_scope(principal, collection_id=7)
    assert exc.value.status_code == 403


def test_unbound_agent_cannot_search_any_collection():
    # bound_collection_id None = non-RAG agent → every collection is off-limits.
    principal = SearchPrincipal(user=object(), agent=_FakeAgent(bound_collection_id=None))
    with pytest.raises(HTTPException) as exc:
        _enforce_agent_collection_scope(principal, collection_id=5)
    assert exc.value.status_code == 403


def test_user_principal_is_unaffected():
    # agent is None (user/JWT/sk- path) → guard is a no-op for any collection.
    principal = SearchPrincipal(user=object(), agent=None)
    _enforce_agent_collection_scope(principal, collection_id=5)
    _enforce_agent_collection_scope(principal, collection_id=999)
