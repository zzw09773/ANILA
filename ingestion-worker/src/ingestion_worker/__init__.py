"""ANILA ingestion-worker.

Arq-based async pipeline that does the heavy lifting for the ingestion
platform: parse uploaded files, chunk them via the strategies registered
in ``anila_core.ingestion.chunking_plugins``, embed each chunk through
the configured embedding endpoint, and index the result into pgvector
through the agent-scoped store.

The worker is the *only* writer to ``document_chunks`` in the canonical
deployment — the CSP API never INSERTs into that table directly. This
keeps RLS / Layer 3 enforcement concentrated in one place.
"""

__version__ = "0.1.0"
