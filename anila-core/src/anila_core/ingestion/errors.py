"""Structured ingestion error taxonomy.

Per docs/ingestion-platform-design.md §8.1, every failure path inside the
ingestion pipeline is wrapped into a stable error code so:

- The worker's retry policy is decided by ``retryable`` (not by guessing
  exception type at the catch site).
- Dev UIs render ``user_message`` directly without leaking stack traces.
- Audit / alert pipelines key off ``code`` for stable filtering and severity
  triage. ``E_PG_RLS_VIOLATION`` always raises a critical alert because it
  means §3.3 Layer 1 + Layer 2 isolation has been bypassed — that is a
  security incident, not a normal failure.

Sprint 1 ships the 5 most common codes (out of 15 in the design doc table).
The remaining codes are added in subsequent sprints as the parser, chunker,
embedder and store layers each ship — keeping the taxonomy small until
each layer actually needs to raise.

Design rule: never raise bare ``Exception`` from worker code. Anything that
escapes ``IngestionError`` becomes ``E_INTERNAL`` (see worker-side wrapper)
with the original exception preserved on ``details["cause"]``. The UI
shows a generic message, audit logs get the full trace.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class IngestionError(Exception):
    """Base class for every structured ingestion failure.

    Subclasses set the class-level defaults (``code`` / ``retryable`` /
    ``severity``); instance construction supplies the human / audit message
    and arbitrary context details. This means a worker raise site is short:

        raise ParseError.format_unsupported(
            user_message="只支援 PDF / DOCX / TXT；偵測到 image/heic",
            details={"sha256": doc.sha256, "detected_mime": "image/heic"},
        )

    `details` deliberately excludes the host-side stack trace — that goes
    into the audit log via the worker's exception wrapper, NOT here.
    """

    # Stable error code (UI / API contract; never rename — only deprecate).
    code: str = "E_INTERNAL"

    # If True, the worker may retry per its policy (exponential backoff,
    # bounded attempts). If False, the job is marked failed_permanent
    # immediately.
    retryable: bool = False

    # Severity for alert routing. ``critical`` events page on-call.
    severity: str = "error"

    # Localised, dev-safe message. Goes straight to dev UI without escaping.
    # Default empty so callers must supply context-relevant copy.
    user_message: str = ""

    # Free-form audit context (file size, mime, retry count, ...). Keep
    # JSON-serialisable so it round-trips through the job table cleanly.
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Make ``str(err)`` useful in logs without leaking dev-UI copy.
        super().__init__(f"[{self.code}] {self.user_message or self.code}")

    def to_dict(self) -> dict[str, Any]:
        """Serialise for storage in ``ingestion_jobs.error_*`` columns."""
        return {
            "code": self.code,
            "retryable": self.retryable,
            "severity": self.severity,
            "user_message": self.user_message,
            "details": dict(self.details),
        }


# ── Class hierarchy ─────────────────────────────────────────────────────────
# Subclasses exist so ``except ParseError`` can target a whole category;
# the concrete code is set per-instance via the factory classmethods below.


class ParseError(IngestionError):
    """File-format / parser failures (PDF, DOCX, OCR pre-processing)."""

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
        # CRITICAL: this means an unscoped query reached the engine and
        # would have leaked data without RLS. The job fails permanently
        # AND the on-call gets paged via the alerts pipeline. Do not
        # mark this retryable — a retry without a code fix would just
        # trigger again.
        return cls(
            code="E_PG_RLS_VIOLATION",
            retryable=False,
            severity="critical",
            user_message=user_message,
            details=details or {},
        )
