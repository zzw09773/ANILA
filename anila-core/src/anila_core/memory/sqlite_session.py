"""Backwards-compat shim — canonical location is ``short_term.sqlite``.

Re-exports both the public surface and the two private symbols
(`_get_connection`, `_conn_cache`) that internal callers and tests
already reach in to. Keeps the contract stable while the codebase
migrates to ``short_term.sqlite`` imports.
"""
from .short_term.sqlite import (  # noqa: F401
    SqliteSession,
    _conn_cache,
    _get_connection,
    close_all_connections,
)

__all__ = ["SqliteSession", "_conn_cache", "_get_connection", "close_all_connections"]
