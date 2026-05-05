"""Storage backends for long-term memory.

Each subpackage is one concrete implementation of the contract
defined in ``anila_core.memory.long_term.adapter.MemoryAdapter``
(or, for legacy, the ``MemdirManager`` interface that predates
the adapter abstraction):

* :mod:`anila_core.memory.long_term.backends.filesystem` — the
  original Claude-Code-style memdir, file-system + YAML
  frontmatter, per-agent tenant.
* :mod:`anila_core.memory.long_term.backends.postgres` —
  contract-only stub; the concrete ``PostgresMemoryAdapter`` lives
  in CSP because it owns the SQLAlchemy session pool.
"""
