"""Unit tests for ``anila_core.ingestion.errors``.

Verifies the structured error taxonomy invariants:

- Code, severity and retryability are correctly attached per factory.
- ``E_PG_RLS_VIOLATION`` is *always* critical and *never* retryable —
  this is a security-relevant invariant tested explicitly so a future
  refactor cannot accidentally weaken it.
- ``to_dict`` round-trips cleanly for storage in ``ingestion_jobs``.
- ``str(err)`` includes the code (greppable in logs).
"""

from __future__ import annotations

import pytest

from anila_core.ingestion.errors import (
    ChunkError,
    EmbedError,
    IngestionError,
    ParseError,
    StoreError,
)


def test_parse_format_unsupported_is_warning_and_not_retryable() -> None:
    err = ParseError.format_unsupported(
        user_message="只支援 PDF / DOCX / TXT",
        details={"detected_mime": "image/heic"},
    )
    assert err.code == "E_PARSE_FORMAT_UNSUPPORTED"
    assert err.retryable is False
    assert err.severity == "warning"
    assert err.details["detected_mime"] == "image/heic"


def test_parse_corrupt_is_terminal() -> None:
    err = ParseError.corrupt(user_message="file is corrupt")
    assert err.retryable is False
    assert err.severity == "warning"


def test_embed_timeout_is_retryable() -> None:
    err = EmbedError.timeout(user_message="upstream timeout")
    assert err.code == "E_EMBED_TIMEOUT"
    assert err.retryable is True


def test_pg_connect_is_retryable() -> None:
    err = StoreError.pg_connect(user_message="conn refused")
    assert err.retryable is True
    assert err.severity == "error"


def test_rls_violation_is_critical_and_never_retryable() -> None:
    """Security-relevant invariant — DO NOT relax in future refactors.

    RLS bypass means §3.3 Layer 1+2 failed; retrying would just trigger
    the same defect, and the severity must always be high enough to page
    on-call. The test name documents the contract so anyone changing
    the factory has to read the rationale first.
    """
    err = StoreError.rls_violation(user_message="audit only")
    assert err.code == "E_PG_RLS_VIOLATION"
    assert err.retryable is False, "RLS violations must NEVER auto-retry"
    assert err.severity == "critical", "RLS violations must page on-call"


def test_to_dict_round_trip() -> None:
    err = StoreError.pg_connect(
        user_message="db down", details={"host": "csp-db", "attempt": 3}
    )
    d = err.to_dict()
    assert d == {
        "code": "E_PG_CONNECT",
        "retryable": True,
        "severity": "error",
        "user_message": "db down",
        "details": {"host": "csp-db", "attempt": 3},
    }


def test_str_includes_code() -> None:
    err = ParseError.corrupt(user_message="bad pdf")
    assert "E_PARSE_CORRUPT" in str(err)
    assert "bad pdf" in str(err)


def test_default_internal_error() -> None:
    """Direct instantiation falls back to the safe E_INTERNAL default."""
    err = IngestionError(user_message="audit only")
    assert err.code == "E_INTERNAL"
    assert err.retryable is False
    assert err.severity == "error"


def test_chunk_error_subclass_isinstance() -> None:
    """Catching ``ChunkError`` traps the whole chunking category."""
    err = ChunkError(code="E_CHUNK_INVALID_PARAMS", user_message="bad strategy")
    assert isinstance(err, IngestionError)
    with pytest.raises(ChunkError):
        raise err
