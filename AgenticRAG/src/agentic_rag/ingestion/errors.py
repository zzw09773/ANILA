"""Structured ingestion error taxonomy.

Copied from ``anila_core.ingestion.errors`` during Phase 0 decoupling
(2026-05-02). AgenticRAG keeps a local copy so the storage adapters
can wrap connection / RLS / store failures without depending on the
platform-internal package.

Design rule: never raise bare ``Exception`` from ingestion code; wrap
into one of the subclasses below so the worker's retry policy and the
audit pipeline can key off ``code`` and ``retryable``.

Wire contract: the error codes (``E_PG_CONNECT``, ``E_PG_RLS_VIOLATION``,
``E_PARSE_FORMAT_UNSUPPORTED``, ``E_PARSE_CORRUPT``, ``E_EMBED_TIMEOUT``)
must stay in sync with the upstream anila-core taxonomy because the
ingestion-worker writes them into ``ingestion_jobs.error_code`` and
admin dashboards filter on the literals.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class IngestionError(Exception):
    """Base class for every structured ingestion failure.

    Subclasses define the category; instances supply human / audit
    context. ``str(err)`` formats as ``[code] user_message`` for log
    readability without leaking dev-UI copy.
    """

    code: str = "E_INTERNAL"
    retryable: bool = False
    severity: str = "error"
    user_message: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        super().__init__(f"[{self.code}] {self.user_message or self.code}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "retryable": self.retryable,
            "severity": self.severity,
            "user_message": self.user_message,
            "details": dict(self.details),
        }


class ParseError(IngestionError):
    """File-format / parser failures."""

    @classmethod
    def format_unsupported(
        cls, user_message: str, details: dict[str, Any] | None = None
    ) -> "ParseError":
        return cls(
            code="E_PARSE_FORMAT_UNSUPPORTED",
            retryable=False,
            severity="warning",
            user_message=user_message,
            details=details or {},
        )

    @classmethod
    def corrupt(
        cls, user_message: str, details: dict[str, Any] | None = None
    ) -> "ParseError":
        return cls(
            code="E_PARSE_CORRUPT",
            retryable=False,
            severity="warning",
            user_message=user_message,
            details=details or {},
        )


class ChunkError(IngestionError):
    """Chunking-strategy failures (invalid params, OOM)."""


class EmbedError(IngestionError):
    """Embedding-endpoint failures (timeout, rate limit, model down)."""

    @classmethod
    def timeout(
        cls, user_message: str, details: dict[str, Any] | None = None
    ) -> "EmbedError":
        return cls(
            code="E_EMBED_TIMEOUT",
            retryable=True,
            severity="warning",
            user_message=user_message,
            details=details or {},
        )


class StoreError(IngestionError):
    """pgvector / Postgres write-path failures."""

    @classmethod
    def pg_connect(
        cls, user_message: str, details: dict[str, Any] | None = None
    ) -> "StoreError":
        return cls(
            code="E_PG_CONNECT",
            retryable=True,
            severity="error",
            user_message=user_message,
            details=details or {},
        )

    @classmethod
    def rls_violation(
        cls, user_message: str, details: dict[str, Any] | None = None
    ) -> "StoreError":
        # CRITICAL: an unscoped query would have leaked data without RLS.
        # Don't mark retryable — retry without a code fix repeats the bug.
        return cls(
            code="E_PG_RLS_VIOLATION",
            retryable=False,
            severity="critical",
            user_message=user_message,
            details=details or {},
        )
