"""HIGH-A／HIGH-B（R9）：health 不假綠、初始化失敗與文件轉不動分開。

FakeConverter 覆寫 load/convert 所以 _cache 永遠空——這些不變式走不到它。
這裡直接用一個可操縱 _cache 的 converter,測:
  1. model._verify_ready:offline + 空權重目錄 → RuntimeError(不該標 ready)。
  2. main.parse:第一次 convert 失敗(還沒成功轉過) → 503 kind=init。
  3. main.parse:成功轉過一次後再失敗 → 422 kind=convert。
"""
from __future__ import annotations

import importlib
import importlib.util

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.model import DoclingConverter, _CacheEntry


def _require_easyocr() -> None:
    """三狀態判型:easyocr 沒裝 → skip;裝了但原生函式庫壞 → RED;裝了且好 → 走真路。

    easyocr 的 python 套件可進發行,真正容易在部署時壞掉的是它的 native
    函式庫(libxcb.so.1 / libGL / libglib)——那些在 ``import easyocr`` 才炸。
    pytest.importorskip 會把任何 ImportError 都當「模組不存在」skip 掉,於是
    「裝了 easyocr 但 native lib 遺失」被吞成「沒裝」——那正是 08-17
    「422 parse failed: ImportError」的事故狀態,也是 _verify_ready 這個守衛
    存在的目的(開機時抓出來,而不是第一次 convert 才死)。

    所以分兩步,不執行模組判存在、真要 import 了才執行:
    1. find_spec("easyocr") is None → 真沒裝 → skip。
    2. 有 spec → 真的 ``import easyocr``(production 的 _verify_ready 用完全
       同一句):native lib 錯在此 raise → 不 skip,讓測試紅
       (installed-but-broken 是缺陷,不是環境)。
    """
    if importlib.util.find_spec("easyocr") is None:
        pytest.skip("easyocr not installed")
    import easyocr  # noqa: F401  # native lib 遺失在此 raise → RED,不 skip


class _FlakyConverter(DoclingConverter):
    """convert 永遠 raise;「成功過一次」的狀態由 _cache[key].converted_once
    表示(那是 has_completed_conversion 真正讀的地方)。不碰真 docling。"""

    def __init__(self) -> None:
        super().__init__(artifacts_dir="/tmp/docling-fake", local_files_only=True)

    def load(self, ocr_langs, table_structure, picture_description) -> None:
        pass  # 測試走 /health 直接問 is_ready,不經背景 load

    def is_ready(self, ocr_langs, table_structure, picture_description) -> bool:
        return True

    def convert(self, file_path, *, ocr_langs, table_structure, picture_description, original_name=None):
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

def test_require_easyocr_red_when_installed_but_broken(monkeypatch) -> None:
    """installed-but-broken → RED 不 skip(2026-08-20 revision① 的 INVARIANT)。

    pytest.importorskip 會把「裝了 easyocr 但 native lib(libxcb/libGL)遺失」
    的 ImportError 吞成「沒裝」skip 掉——那正是 08-17「422 parse failed:
    ImportError」的事故狀態。_require_easyocr 用 find_spec 判 installed、
    真有 spec 才 import,所以「spec 存在但 import 炸」必須往上游 raise(測試
    紅),不能吞成 skip。
    """
    # 模擬「套件進得了發行」(spec 非 None);本機是未安裝,真 import 一定炸。
    monkeypatch.setattr(
        importlib.util, "find_spec", lambda name: object()
    )
    with pytest.raises(ImportError):
        _require_easyocr()


def test_require_easyocr_skips_when_absent(monkeypatch) -> None:
    """absent → skip(不是紅):沒裝 easyocr 的開發機照樣 skip,不因缺套件假紅。"""
    monkeypatch.setattr(
        importlib.util, "find_spec", lambda name: None
    )
    with pytest.raises(pytest.skip.Exception):
        _require_easyocr()


def test_verify_ready_rejects_empty_artifacts_dir(tmp_path) -> None:
    """local_files_only + 權重目錄空 → RuntimeError(不該標 ready)。

    _verify_ready 先 import easyocr 再查權重目錄;沒裝 easyocr 的機器上若不
    skip,這裡會先在 import 就 raise(訊息是「easyocr import failed」不是
    「empty or missing」)→ 必紅且紅得像「空目錄守衛壞了」的真缺陷,實際只是
    環境缺套件。_require_easyocr 讓缺席＝skip,裝了 easyocr 但 native lib
    壞掉＝RED(不是 skip,那正是守衛存在的目的),裝了且好才走真的斷言。
    """
    _require_easyocr()
    converter = DoclingConverter(artifacts_dir=str(tmp_path), local_files_only=True)
    with pytest.raises(RuntimeError, match="empty or missing"):
        converter._verify_ready()


def test_verify_ready_ok_when_artifacts_nonempty(tmp_path) -> None:
    """權重目錄非空 + easyocr importable → _verify_ready 不該拋。

    舊寫法用 try/except 只斷言「若拋,不是 empty or missing」——在無 easyocr
    的機器上 import 就 raise(不是 empty or missing→斷言過)、裝了 easyocr 的
    機器上直接不拋(斷言沒走到)→ 兩條路都恆綠。那是「負向斷言沒有正向錨點」的
    恆綠(2026-08-19 批五)。改成正向:import easyocr 成功、目錄非空 → 直接呼叫
    不拋;真的拋了 = 紅。installed-but-broken 由 _require_easyocr 保留成 RED。
    """
    _require_easyocr()
    (tmp_path / "some-weight").write_text("x")
    converter = DoclingConverter(artifacts_dir=str(tmp_path), local_files_only=True)
    converter._verify_ready()  # 不拋 = 過;拋 = 紅


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
