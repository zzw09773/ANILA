"""HTTP clients for accessing the user-tenant memory backend.

Used by sub-agents that need to read facts about the user they're
serving — the cross-tenant flow added in route-3 Phase 3. The
storage backend (CSP's ``PostgresMemoryAdapter``) exposes
``GET /api/memory/users/{user_id}/facts`` behind agent service
token auth; :class:`HttpUserFactReader` is the typed client side.

Why this is in anila-core, not in CSP: agents run anila-core, not
CSP. Putting the client here means an agent SDK consumer doesn't
have to depend on the platform backend package just to read user
facts.
"""
from .http_user_facts import HttpUserFactReader, UserFactReadError

__all__ = ["HttpUserFactReader", "UserFactReadError"]
