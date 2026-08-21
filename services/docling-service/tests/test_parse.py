"""docling-service 契約測試（fake converter,不載 docling 權重）。

對齊 asr-decoder 的 tests/test_transcribe.py 形狀:注入 fake converter,
直接測 HTTP 契約(token gate、multipart 解析、health 三態)。真正的
docling 轉換在 model.py 的 unit test 已包(見 test_model.py)。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app, _parse_bool
from app.model import DoclingConverter


class FakeConverter(DoclingConverter):
    def __init__(self) -> None:
        super().__init__(artifacts_dir="/tmp/docling-fake", local_files_only=True)
        self._ready = True
        self.last = None

    def load(self, ocr_langs, table_structure, picture_description) -> None:
        pass

    def is_ready(self, ocr_langs, table_structure, picture_description) -> bool:
        return self._ready

    def convert(self, file_path, *, ocr_langs, table_structure, picture_description, original_name=None) -> dict:
        self.last = (ocr_langs, table_structure, picture_description)
        # 批次①:title fallback 優先用呼叫端傳來的原始檔名(不再回暫存名)。
        title = original_name or "fake"
        return {
            "markdown": "# fake doc",
            "title": title,
            "page_count": 1,
            "ocr_applied": False,
            "images": [],
        }


def _client(converter: FakeConverter | None = None) -> TestClient:
    settings = Settings(DOCLING_SERVICE_TOKEN="test-token-0123456789abcdef0123456789abcdef")
    app = create_app(converter=converter or FakeConverter(), app_settings=settings)
    return TestClient(app)


def test_health_ok_when_ready() -> None:
    c = _client()
    r = c.get("/health")
    assert r.status_code == 200
    assert r.json()["model_ready"] is True


def test_health_loading_when_not_ready() -> None:
    cv = FakeConverter()
    cv._ready = False
    c = _client(cv)
    r = c.get("/health")
    assert r.status_code == 503
    assert r.json()["model_ready"] is False


def test_parse_requires_token() -> None:
    c = _client()
    r = c.post(
        "/parse",
        files={"file": ("a.pdf", b"%PDF fake", "application/pdf")},
    )
    assert r.status_code == 401


def test_parse_rejects_wrong_token() -> None:
    c = _client()
    r = c.post(
        "/parse",
        files={"file": ("a.pdf", b"%PDF fake", "application/pdf")},
        headers={"X-Token": "wrong"},
    )
    assert r.status_code == 401


def test_parse_ok_with_token() -> None:
    cv = FakeConverter()
    c = _client(cv)
    r = c.post(
        "/parse",
        files={"file": ("a.pdf", b"%PDF fake", "application/pdf")},
        headers={"X-Token": "test-token-0123456789abcdef0123456789abcdef"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["markdown"] == "# fake doc"
    assert body["page_count"] == 1


def test_parse_rejects_unsupported_extension() -> None:
    c = _client()
    r = c.post(
        "/parse",
        files={"file": ("a.exe", b"MZ fake", "application/octet-stream")},
        headers={"X-Token": "test-token-0123456789abcdef0123456789abcdef"},
    )
    assert r.status_code == 400


def test_unsupported_extension_detail_contract() -> None:
    """守衛:400 body 的 "unsupported extension" 子字串是跨服務分類依據。

    anila-core 的 docling_parser._raise_for_status 靠它在 400 裡認出「不支援
    格式」→ E_PARSE_FORMAT_UNSUPPORTED。服務端改寫／在地化這個字串,客戶端會
    靜默退回 corrupt(對使用者說「檔案可能損毀」)。這是 R7 MEDIUM-2 的守衛:
    「改掉那個子字串 → 這條測試紅」。
    """
    from app.main import SUPPORTED_SUFFIXES

    # 守衛的前提:副檔名真的不在 service 的支援集合裡——這樣下面的 400 才是
    # 走「不支援格式」那條路,不是什麼別的路。
    assert ".zzz" not in SUPPORTED_SUFFIXES

    # 直接用往返最小不變式:服務端 SUPPORTED_SUFFIXES 的補集副檔名會收到
    # detail 含 "unsupported extension"。不寫死英文以外的實作,只釘子字串。
    r = _client().post(
        "/parse",
        files={"file": ("a.zzz", b"MZ fake", "application/octet-stream")},
        headers={"X-Token": "test-token-0123456789abcdef0123456789abcdef"},
    )
    assert r.status_code == 400
    assert "unsupported extension" in r.json()["detail"]


def test_parse_passes_options_through() -> None:
    cv = FakeConverter()
    c = _client(cv)
    r = c.post(
        "/parse",
        files={"file": ("a.pdf", b"%PDF fake", "application/pdf")},
        data={"ocr_langs": "ch_tra", "table_structure": "false", "picture_description": "true"},
        headers={"X-Token": "test-token-0123456789abcdef0123456789abcdef"},
    )
    assert r.status_code == 200
    assert cv.last == (["ch_tra"], False, True)


def test_parse_bool_variants() -> None:
    assert _parse_bool("1", True) is True
    assert _parse_bool("false", True) is False
    assert _parse_bool(None, True) is True
    with pytest.raises(Exception):
        _parse_bool("maybe", True)


def test_empty_file_rejected() -> None:
    c = _client()
    r = c.post(
        "/parse",
        files={"file": ("a.pdf", b"", "application/pdf")},
        headers={"X-Token": "test-token-0123456789abcdef0123456789abcdef"},
    )
    assert r.status_code == 400


def test_non_ascii_token_gets_401_not_500() -> None:
    """非 ASCII token:verify_token 要 .encode() 後比,否則 compare_digest
    對非 ASCII str 拋 TypeError → 500。直接測守衛(httpx.TestClient 建請求時
    就拒收非 ASCII header 值,送不到這層)。"""
    from starlette.exceptions import HTTPException as _HTTPException

    from app.main import verify_token

    class _State:
        settings = Settings(DOCLING_SERVICE_TOKEN="test-token-0123456789abcdef0123456789abcdef")

    class _App:
        state = _State

    class _Req:
        app = _App()

    with pytest.raises(_HTTPException) as excinfo:
        verify_token(_Req(), "祕密-非-ASCII-令牌")
    assert excinfo.value.status_code == 401


def test_non_ascii_token_rejected_at_settings_with_clear_message() -> None:
    """組態層:非 ASCII token 在 import 期就拒收,錯誤點名欄位(不指向網路)。"""
    from pydantic import ValidationError

    with pytest.raises(ValidationError) as excinfo:
        Settings(DOCLING_SERVICE_TOKEN="祕密-非-ASCII-令牌")
    assert "DOCLING_SERVICE_TOKEN" in str(excinfo.value)
    assert "ASCII" in str(excinfo.value)


def test_oversize_file_rejected_413() -> None:
    settings = Settings(DOCLING_SERVICE_TOKEN="test-token-0123456789abcdef0123456789abcdef", DOCLING_MAX_FILE_BYTES=10)
    app = create_app(converter=FakeConverter(), app_settings=settings)
    c = TestClient(app)
    r = c.post(
        "/parse",
        files={"file": ("a.pdf", b"x" * 11, "application/pdf")},
        headers={"X-Token": "test-token-0123456789abcdef0123456789abcdef"},
    )
    assert r.status_code == 413


def test_settings_ocr_langs_used_when_form_omitted() -> None:
    """表單沒帶 ocr_langs → 要回落設定檔的 DOCLING_OCR_LANGS(不是硬寫預設)。"""
    cv = FakeConverter()
    settings = Settings(
        DOCLING_SERVICE_TOKEN="test-token-0123456789abcdef0123456789abcdef", DOCLING_OCR_LANGS="ja,en"
    )
    app = create_app(converter=cv, app_settings=settings)
    c = TestClient(app)
    r = c.post(
        "/parse",
        files={"file": ("a.pdf", b"%PDF fake", "application/pdf")},
        headers={"X-Token": "test-token-0123456789abcdef0123456789abcdef"},
    )
    assert r.status_code == 200
    assert cv.last[0] == ["ja", "en"]
