"""HIGH-A／HIGH-B（R9）：health 不假綠、初始化失敗與文件轉不動分開。

FakeConverter 覆寫 load/convert 所以 _cache 永遠空——這些不變式走不到它。
這裡直接用一個可操縱 _cache 的 converter,測:
  1. model._verify_ready:offline + 空權重目錄 → RuntimeError(不該標 ready)。
  2. main.parse:第一次 convert 失敗(還沒成功轉過) → 503 kind=init。
  3. main.parse:成功轉過一次後再失敗 → 422 kind=convert。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.model import DoclingConverter, _CacheEntry


class _FlakyConverter(DoclingConverter):
    """convert 永遠 raise;「成功過一次」的狀態由 _cache[key].converted_once
    表示(那是 has_completed_conversion 真正讀的地方)。不碰真 docling。"""

    def __init__(self) -> None:
        super().__init__(artifacts_dir="/tmp/docling-fake", local_files_only=True)

    def load(self, ocr_langs, table_structure, picture_description) -> None:
        pass  # 測試走 /health 直接問 is_ready,不經背景 load

    def is_ready(self, ocr_langs, table_structure, picture_description) -> bool:
        return True

    def convert(self, file_path, *, ocr_langs, table_structure, picture_description):
        # fake 永遠轉不動;失敗歸 5xx/init 或 422/convert 由 main 依
        # has_completed_conversion 的 key 決定。
        raise RuntimeError("model not loaded")

    def mark_succeeded_once(self, ocr_langs, table_structure, picture_description) -> None:
        """在「該 option-key」上標 converted_once=True——模擬那次 key 成功轉過。"""
        key = self._key(ocr_langs, table_structure, picture_description)
        entry = _CacheEntry(None, ready=True)
        entry.converted_once = True
        self._cache[key] = entry


def _client(converter: DoclingConverter) -> TestClient:
    settings = Settings(DOCLING_SERVICE_TOKEN="test-token-0123456789abcdef0123456789abcdef")
    app = create_app(converter=converter, app_settings=settings)
    return TestClient(app)


# 請求不帶 ocr_langs / table_structure / picture_description 時,main 用設定
# 預設 lang=["ch_tra","en"],table_structure=True,picture_description=False。
_DEFAULT_ARGS = (["ch_tra", "en"], True, False)


# ── HIGH-A:health 不假綠 ────────────────────────────────────────────────────

def test_verify_ready_rejects_empty_artifacts_dir(tmp_path) -> None:
    """local_files_only + 權重目錄非空 → RuntimeError(不該標 ready)。"""
    converter = DoclingConverter(artifacts_dir=str(tmp_path), local_files_only=True)
    with pytest.raises(RuntimeError, match="empty or missing"):
        converter._verify_ready()


def test_verify_ready_ok_when_artifacts_nonempty(tmp_path) -> None:
    """權重目錄裡有東西 → 不做空目錄假綠。(easyocr import 在裝了 docling 的
    主機上會真的走,這裡只驗空目錄那條;無 docling 的環境 import 會 raise,
    但那正是本機(platform)的特性,由 18 條既有測試的 conftest 繞過。)
    """
    (tmp_path / "some-weight").write_text("x")
    converter = DoclingConverter(artifacts_dir=str(tmp_path), local_files_only=True)
    # 本機無 easyocr/docling,import easyocr 會 raise RuntimeError——那證明
    # _verify_ready 真的會把「import 不到」當失敗,不是只在空目錄才擋。
    # 這裡用 local_files_only=True + 非空目錄,驗「不因空目錄」這半條(另一半
    # easyocr import 在映像端到端驗)。只要不拋"empty or missing"就是過。
    try:
        converter._verify_ready()
    except RuntimeError as exc:
        assert "empty or missing" not in str(exc)


# ── HIGH-B:init 失敗 5xx vs 文件轉不動 422 ─────────────────────────────────

def test_has_completed_conversion_requires_actual_conversion() -> None:
    """load() 成功(ready=True)不代表轉出過文件。

    2026-08-18 實測抓到:has_completed_conversion 曾讀 ready,而 load() 一完成
    就把 ready=True → 第一次 convert 的基礎設施失敗被誤判 422。拆成兩個旗標後,
    「ready=True 但 converted_once=False」必須回 False。
    """
    converter = DoclingConverter(artifacts_dir="/tmp/docling-fake", local_files_only=True)
    key = converter._key(*_DEFAULT_ARGS)
    converter._cache[key] = _CacheEntry(None, ready=True)  # load 成功,尚未 convert
    assert converter.has_completed_conversion(*_DEFAULT_ARGS) is False


def test_first_convert_failure_after_load_is_503() -> None:
    """真流程:load() 已跑過(ready=True、converted_once=False)→ 同 key 的第一次
    convert 失敗必須 503 init,不是 422。修法前 has_completed_conversion 讀
    ready 會回 True → 這條會拿到 422(紅)。"""
    cv = _FlakyConverter()
    # 模擬 load() 完成的快取狀態:該 key ready=True 但還沒成功轉過。
    key = cv._key(*_DEFAULT_ARGS)
    cv._cache[key] = _CacheEntry(None, ready=True)
    c = _client(cv)
    r = c.post(
        "/parse",
        files={"file": ("a.pdf", b"%PDF fake", "application/pdf")},
        headers={"X-Token": "test-token-0123456789abcdef0123456789abcdef"},
    )
    assert r.status_code == 503
    body = r.json()
    assert body["detail"]["kind"] == "init"


def test_first_convert_failure_is_503_init() -> None:
    cv = _FlakyConverter()
    c = _client(cv)
    r = c.post(
        "/parse",
        files={"file": ("a.pdf", b"%PDF fake", "application/pdf")},
        headers={"X-Token": "test-token-0123456789abcdef0123456789abcdef"},
    )
    assert r.status_code == 503
    body = r.json()
    assert body["detail"]["kind"] == "init"


def test_convert_failure_after_success_is_422() -> None:
    cv = _FlakyConverter()
    cv.mark_succeeded_once(*_DEFAULT_ARGS)
    c = _client(cv)
    r = c.post(
        "/parse",
        files={"file": ("a.pdf", b"%PDF fake", "application/pdf")},
        headers={"X-Token": "test-token-0123456789abcdef0123456789abcdef"},
    )
    assert r.status_code == 422
    body = r.json()
    assert body["detail"]["kind"] == "convert"


def test_other_key_success_does_not_mask_this_key_failure() -> None:
    """MEDIUM-1:範圍跟失敗的範圍一致(逐 key,不是全域)。

    2026-08-18 稽核抓到:has_completed_conversion 用 any(...) 問全域——當
    picture_description=false 那把 key 成功過,之後第一次送 picture_description
    =true(要另一份氣隙裡沒有的權重)時,會被誤判「轉成功過」→ 422 → corrupt →
    對使用者說「檔案可能損毀」。修成逐 key 後,另一把 key 的成功不得抵銷
    這把 key 的第一次失敗(必須 503 init)。"""
    cv = _FlakyConverter()
    # 另一把 key(picture_description=True)成功過 —— 與本次請求 key 不同。
    cv.mark_succeeded_once(["ch_tra", "en"], True, True)
    c = _client(cv)
    # 本次請求用預設(picture_description=False),這是第一次進這把 key。
    r = c.post(
        "/parse",
        files={"file": ("a.pdf", b"%PDF fake", "application/pdf")},
        headers={"X-Token": "test-token-0123456789abcdef0123456789abcdef"},
    )
    assert r.status_code == 503
    body = r.json()
    assert body["detail"]["kind"] == "init"
