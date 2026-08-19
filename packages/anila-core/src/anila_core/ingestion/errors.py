"""Structured ingestion error taxonomy.

Per docs/ingestion/ingestion-platform-design.md §8.1, every failure path inside the
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
    # ⚠ 這個欄位已經是 API,不是註解(2026-08-17 八輪審查把它變成三個消費者
    # 的實際控制輸入)。**填它 = 同時選了兩件事**:
    #   - HTTP 狀態碼(parse 路徑):services/csp/app/api/ingestion/preview.py:87
    #       severity=="warning" → 400,否則 500。所以 ParseError.corrupt 之所以
    #       對使用者回 400,唯一原因是它填了 warning——光看欄位名看不出來。
    #   - log 等級(兩處):preview.py:269 與
    #       services/csp/app/services/attachment_service.py:197——
    #       warning→WARNING、critical→CRITICAL、其餘→ERROR。
    # 語意(只對 parse 路徑成立,embedding 的 EmbedError 走 worker,不進這條):
    #   warning   = 檔案的錯(使用者能自救) → 4xx + WARNING
    #   error     = 組態/基礎設施(維運的錯) → 5xx + ERROR
    #   critical  = 安全事件(立即告警)     → 5xx + CRITICAL
    # 新增 ParseError / RemoteParseError 的子類 factory 時,severity 必須在
    # warning 與 error/critical 之間**有意地**選——不是照「挑 log 等級的直覺」。
    # 守衛測試 tests/test_ingestion_errors.py::test_severity_axis_contract 釘住
    # 每一顆 code 的 severity,新增一分類會被迫更新那張表,不是猜。
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

    @classmethod
    def bad_config(
        cls, user_message: str, details: dict[str, Any] | None = None
    ) -> "ParseError":
        # 設定值不合法(如 DOCLING_TIMEOUT_SECONDS=abc)。與 corrupt(檔案壞)、
        # format_unsupported(副檔名)都不同:這是組態錯,訊息要指向那顆變數。
        # terminal,不重試——重試不會把錯的設定變對。
        return cls(
            code="E_PARSE_BAD_CONFIG",
            retryable=False,
            severity="error",
            user_message=user_message,
            details=details or {},
        )

    @classmethod
    def too_large(
        cls, user_message: str, details: dict[str, Any] | None = None
    ) -> "ParseError":
        # 檔案超過上限。是檔案的錯(不重試),但文案跟 corrupt 不同——不能叫
        # 使用者去猜「純圖片／損毀／密碼保護」。
        return cls(
            code="E_PARSE_TOO_LARGE",
            retryable=False,
            severity="warning",
            user_message=user_message,
            details=details or {},
        )


class ChunkError(IngestionError):
    """Chunking-strategy failures (invalid params, OOM)."""


class RemoteParseError(IngestionError):
    """Remote parser-endpoint failures (service down, timeout, unreachable).

    Deliberately a **sibling** of ``ParseError``, not a subclass: ``ParseError``
    means "the file itself is wrong" and is never retryable; a remote-compute
    parser (docling, reached over HTTP like ASR) can be down through no fault
    of the uploaded file — that is infrastructure, so it must be retryable and
    its user message must point at the service, not at the document. Collapsing
    it into ``ParseError.corrupt`` would tell the user "your file is broken"
    when what is broken is the GPU host.
    """

    @classmethod
    def endpoint_unavailable(
        cls, user_message: str, details: dict[str, Any] | None = None
    ) -> "RemoteParseError":
        return cls(
            code="E_PARSE_REMOTE_DOWN",
            retryable=True,
            severity="error",
            user_message=user_message,
            details=details or {},
        )


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
