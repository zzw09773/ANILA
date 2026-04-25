"""Ingestion platform support layer (Phase 2 Sprint 1+).

Sprint 1 (this version) ships only the abstractions every consumer of the
central ingestion-worker service needs:

- ``errors``    — structured ``IngestionError`` taxonomy for retry / alert /
                  UI display logic. The worker classifies every failure into
                  a stable error code so dev UIs and audit logs stay coherent.
- ``chunking_plugins``  (Chunk B)  — Protocol + registry + built-in
                  strategies. anila-core ships the *interface*; the worker
                  service registers and runs the strategies.

Note: the heavyweight parser / OCR / docling pipeline that previously lived
under ``anila_core.ingestion`` was moved out in v0.5.0 (see CHANGELOG). It
now belongs to the ingestion-worker service. anila-core keeps only the
boundary types so SDK callers can talk to the worker without depending on
parsing internals.
"""

from anila_core.ingestion.errors import (
    ChunkError,
    EmbedError,
    IngestionError,
    ParseError,
    StoreError,
)

__all__ = [
    "ChunkError",
    "EmbedError",
    "IngestionError",
    "ParseError",
    "StoreError",
]
