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
    RemoteParseError,
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


def test_remote_parse_down_is_retryable_and_distinct_code() -> None:
    """遠端 parser 端點失敗 = 基礎設施,不是檔案壞 → 另一個 code,可重試。

    與 ParseError.corrupt(E_PARSE_CORRUPT / retryable=False)必須是兩種不同的
    code,否則 worker 會把 GPU 主機重開機期間的每份文件都標成使用者檔案損毀。
    """
    err = RemoteParseError.endpoint_unavailable(user_message="docling 端點無回應")
    assert err.code == "E_PARSE_REMOTE_DOWN"
    assert err.retryable is True
    assert err.severity == "error"
    # 不能是 ParseError 的子類——except ParseError 那條舊分支要能把它放過。
    assert not isinstance(err, ParseError)


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


def test_severity_axis_contract() -> None:
    """severity 是 API:填它 = 同時選 HTTP 狀態碼與告警等級(見 errors.py 那欄的 docstring)。

    這張表是**守衛**,不是文件。預期每個 parse 路徑的 factory:
      warning  = 檔案的錯(使用者能自救)            → 4xx
      error    = 組態/基礎設施(維運的錯)           → 5xx
      critical = 安全事件                           → 5xx + 頁級告警

    ⚠ 範圍:只涵蓋 ParseError 與 RemoteParseError——這兩類走 parse 路徑,severity
    會被 preview.py 拿來決定 HTTP 狀態碼。EmbedError / StoreError / ChunkError
    走 worker 的 embed/store/chunk 路徑,severity 只進 alert routing、不進 HTTP
    語意,不在這張「HTTP 語意」對照表的涵蓋範圍內——不是漏掉,是範圍判斷。

    ⚠ 這個範圍不是自明,因此本測試用反射(vars(cls))撈出這兩個類上的全部
    classmethod factory,與下面的表比對——**新增一顆 factory 沒進表,這條會紅**,
    不是靠人記得來補。
    """
    # (factory 名字 → factory, 期望 severity):每一筆都是 parse 路徑的一顆 code。
    exposure = {
        ("ParseError", "format_unsupported"): ("warning", ParseError.format_unsupported),
        ("ParseError", "corrupt"): ("warning", ParseError.corrupt),
        ("ParseError", "too_large"): ("warning", ParseError.too_large),
        ("ParseError", "bad_config"): ("error", ParseError.bad_config),
        ("RemoteParseError", "endpoint_unavailable"): ("error", RemoteParseError.endpoint_unavailable),
    }
    # 反射撈全部 factory,與表比對——宣稱「釘住每一顆」必須是真的,不是手寫清單。
    discovered = set()
    for cls_name, cls in (("ParseError", ParseError), ("RemoteParseError", RemoteParseError)):
        for name, value in vars(cls).items():
            if isinstance(value, classmethod):
                discovered.add((cls_name, name))
    assert discovered == set(exposure), (
        f"severity 對照表與實際 factory 不同步。表裡有:"
        f"{sorted(exposure)};實際 factory:{sorted(discovered)}。"
        "新增 ParseError/RemoteParseError 的子類 factory 時,要在這張表選好它的 severity。"
    )
    for (cls_name, _name), (expected_severity, factory) in exposure.items():
        err = factory(user_message="x")
        assert err.severity == expected_severity, (
            f"{err.code} 的 severity 是 {err.severity},預期 {expected_severity}——"
            "severity 決定它對使用者回 4xx 還是 5xx;改這一格等於改 HTTP 語意。"
        )
    # 反向不變式:warning ⇔ 使用者能自救(parse 路徑 → 400);error/critical ⇔ 5xx。
    for (_cls_name, _name), (expected_severity, factory) in exposure.items():
        if expected_severity == "warning":
            err = factory(user_message="x")
            assert err.retryable is False, f"{err.code} 是檔案的錯,不可重試"
