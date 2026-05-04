"""Postgres-backed long-term memory — contract only.

The concrete ``PostgresMemoryAdapter`` implementation lives in CSP
(``myCSPPlatform.app.services.memory_service``) because it owns the
SQLAlchemy session pool, the alembic migrations, and the embed-
client config. This subpackage carries documentation + any future
shared helpers (e.g. embedding-vector serialisation) that benefit
from sitting next to the contract in anila-core.

For now, intentionally thin — see
:class:`anila_core.memory.long_term.adapter.MemoryAdapter` for the
Protocol that the CSP impl conforms to.
"""
