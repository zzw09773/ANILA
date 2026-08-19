"""HIGH（R3）的兩條路各一個測試：遠端 parser 故障 vs 真正損毀的檔案。

不變式：基礎設施故障與檔案本身的問題,是兩種不同的錯誤,使用者訊息不能把
前者說成後者。extract_text 裡那道分流是唯一的分界點——RemoteDoclingError
要導到 retryable 的 RemoteParseError(訊指向服務),其他 parser 例外照舊塌進
ParseError.corrupt(retryable=False,訊指向文件)。
"""
from __future__ import annotations

import pytest

from anila_core.ingestion.errors import ParseError, RemoteParseError
from anila_core.ingestion.parsers import extract_text


@pytest.fixture(autouse=True)
def _reset_registry_cache(monkeypatch):
    """確保 extract_text 的 lazy import 每次拿到可被 monkeypatch 的 parse。"""
    import anila_core.ingestion.parser_registry as reg

    monkeypatch.setattr(reg.ParserRegistry, "_docling_parser", None)
    monkeypatch.setattr(reg.ParserRegistry, "_docling_initialised", False)
    yield


def _run(monkeypatch, filename: str, fmt: str, parse_side_effect) -> tuple:
    """Stub ``ParserRegistry.parse`` 後跑一次 extract_text。"""
    import anila_core.ingestion.parser_registry as reg

    monkeypatch.setattr(
        reg.ParserRegistry, "parse", staticmethod(parse_side_effect)
    )
    return extract_text(filename, b"%PDF-1.4 fake", fmt)


def test_remote_docling_parse_down_is_retryable_and_points_at_service(
    monkeypatch,
) -> None:
    """遠端故障 → RemoteParseError(retryable、訊息指服務),不是 corrupt。"""
    from anila_core.ingestion.docling_parser import RemoteDoclingError

    def _raise_remote(path):
        raise RemoteDoclingError(
            "文件解析服務暫時無法處理,請稍後重試。",
            details={"status_code": 503, "endpoint": "http://docling-gpu.internal:9100"},
        )

    with pytest.raises(RemoteParseError) as excinfo:
        _run(monkeypatch, "x.pdf", "application/pdf", _raise_remote)

    assert excinfo.value.code == "E_PARSE_REMOTE_DOWN"
    assert excinfo.value.retryable is True
    # 訊息指服務,不指「你的檔案壞了」;也「不」把內部端點塞進使用者面。
    assert "檔案損毀" not in excinfo.value.user_message
    assert "檔案無法解析" not in excinfo.value.user_message
    assert "docling-gpu.internal" not in excinfo.value.user_message
    # 維運面拿得到端點(M3)。
    assert "docling-gpu.internal" in str(excinfo.value.details)


def test_genuine_corrupt_file_still_maps_to_corrupt_not_retryable(
    monkeypatch,
) -> None:
    """真正損毀的檔案 → 仍然是 ParseError.corrupt、不可重試(舊分支回歸)。

    ⚠ 損毀例外不能是 ValueError——parsers.py 的 `except ValueError` 是「不支援
    格式」分支(排在損毀分支之前)。真正的損毀例外是 RuntimeError 這一類。
    """

    def _raise_generic(path):
        raise RuntimeError("docling: document is corrupt")

    with pytest.raises(ParseError) as excinfo:
        _run(monkeypatch, "x.pdf", "application/pdf", _raise_generic)

    assert excinfo.value.code == "E_PARSE_CORRUPT"
    assert excinfo.value.retryable is False
    # 舊分支的訊息未被新分流污染。
    assert "檔案無法解析" in excinfo.value.user_message


def test_bad_config_setting_maps_to_bad_config_not_format_unsupported(
    monkeypatch,
) -> None:
    """F2 接縫:設定手誤要點名變數,不能掉進「不支援副檔名」分支。

    這條採 factory 的結果「穿過 extract_text」——不是只釘建構時會拋——斷言
    到達使用者手上的是 E_PARSE_BAD_CONFIG、訊息指變數,而非「不支援 .pdf」。
    """
    monkeypatch.setenv("DOC_PARSER", "docling")
    monkeypatch.setenv("DOCLING_URL", "http://docling:9100")
    monkeypatch.setenv("DOCLING_TIMEOUT_SECONDS", "abc")
    monkeypatch.setenv("ANILA_ALLOW_HTTP_ENDPOINT", "1")
    monkeypatch.setenv("ANILA_TRUSTED_HOSTS", "docling")
    import anila_core.ingestion.parser_registry as reg

    monkeypatch.setattr(reg.ParserRegistry, "_docling_parser", None)
    monkeypatch.setattr(reg.ParserRegistry, "_docling_initialised", False)

    with pytest.raises(ParseError) as excinfo:
        extract_text("x.pdf", b"%PDF-1.4 fake", "application/pdf")

    assert excinfo.value.code == "E_PARSE_BAD_CONFIG"
    # 不歸錯因:不是「不支援格式」。
    assert excinfo.value.code != "E_PARSE_FORMAT_UNSUPPORTED"
    # MEDIUM-A:設定名進 details(維運看得到),不進 body(使用者看不到)。
    assert "DOCLING_TIMEOUT_SECONDS" in str(excinfo.value.details)
    assert "DOCLING_TIMEOUT_SECONDS" not in excinfo.value.user_message
