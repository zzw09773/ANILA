"""離線開關的接線測試(不載 docling 權重)。

守的不變式:``DOCLING_LOCAL_FILES_ONLY`` 是「讀起來像在控制離線」的設定,
就必須真的控制離線——而不是被平台那些測試用 create_app() 繞過之後,
實際上沒有任何人讀它(2026-08-17 審查 HIGH)。

實測事實(docling 2.120.1):EasyOCR 不吃 HF_HUB_OFFLINE / TRANSFORMERS_OFFLINE,
靠 url 直抓權重;離線唯一咬得住 EasyOCR 的是 EasyOcrOptions.download_enabled。
這裡用 fake docling module 驗證 _build 真的把 local_files_only 傳進去了。

⚠ 半離線陷阱(R3 MEDIUM):Dockerfile 把 HF_HUB_OFFLINE/TRANSFORMERS_OFFLINE
烘死成 1。若 apply_offline_environment(False) 不做事,local_files_only=0 只開
EasyOCR 那一半、HF 端仍鎖 1 → 半線上。所以 False 分支必須明確寫 0。測試要
「能在那個半離線狀態下變紅」:先讓 env 處於映像烘死的 1(不自己埋 0),再呼叫,
斷言它被翻成 0。
"""
from __future__ import annotations

import os
import sys
import types

import pytest

from app.model import DoclingConverter, apply_offline_environment


def test_apply_offline_environment_sets_env_when_offline(monkeypatch) -> None:
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    monkeypatch.delenv("TRANSFORMERS_OFFLINE", raising=False)
    apply_offline_environment(True)
    assert os.environ["HF_HUB_OFFLINE"] == "1"
    assert os.environ["TRANSFORMERS_OFFLINE"] == "1"


def test_apply_offline_environment_flips_env_when_not_offline(monkeypatch) -> None:
    """=0 時要主動把 env 翻成 0,不能指望 env 本來就是 0。

    映像烘死的是 1(Dockerfile)。這條測試故意從「1」出發(不是自己塞 0),
    斷言 False 分支把 1 改寫成 0——若 False 分支不做事(半離線缺陷),這裡
    「HF_HUB_OFFLINE 仍 == 1」就會紅,而非恆綠。
    """
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")  # 模擬 Dockerfile 烘死的起始狀態
    monkeypatch.setenv("TRANSFORMERS_OFFLINE", "1")
    apply_offline_environment(False)
    assert os.environ["HF_HUB_OFFLINE"] == "0", (
        "local_files_only=False 必須把映像烘死的 HF_HUB_OFFLINE=1 改成 0,"
        "否則 HF 端仍離線、EasyOCR 端上線=半線上"
    )
    assert os.environ["TRANSFORMERS_OFFLINE"] == "0"
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    monkeypatch.delenv("TRANSFORMERS_OFFLINE", raising=False)


def _install_fake_docling(monkeypatch, captured: dict) -> None:
    """用 monkeypatch.setitem 裝假 docling 模組 → 測試結束自動拆除。

    ⚠ 直接寫 sys.modules[...] = fake 會留在全域,在真的裝了 docling 的 GPU
    主機上,後續測試會靜默拿到這個假模組。monkeypatch 會自動恢復原狀。
    """

    class _InputFormat:
        PDF = "pdf"

    def _easyocr_options(**kwargs):
        captured["easyocr_kwargs"] = kwargs
        return object()

    def _pipeline_options(**kwargs):
        captured["pipeline_kwargs"] = kwargs
        return object()

    fake_pipeline_options = types.ModuleType("docling.datamodel.pipeline_options")
    fake_pipeline_options.EasyOcrOptions = _easyocr_options
    fake_pipeline_options.PdfPipelineOptions = _pipeline_options

    fake_base_models = types.ModuleType("docling.datamodel.base_models")
    fake_base_models.InputFormat = _InputFormat

    class _PdfFormatOption:
        def __init__(self, pipeline_options=None):
            self.pipeline_options = pipeline_options

    fake_converter_mod = types.ModuleType("docling.document_converter")
    fake_converter_mod.PdfFormatOption = _PdfFormatOption
    fake_converter_mod.DocumentConverter = lambda **kw: ("set_format", kw)

    parent = types.ModuleType("docling")
    monkeypatch.setitem(sys.modules, "docling", parent)
    monkeypatch.setitem(
        sys.modules, "docling.datamodel.pipeline_options", fake_pipeline_options
    )
    monkeypatch.setitem(
        sys.modules, "docling.datamodel.base_models", fake_base_models
    )
    monkeypatch.setitem(
        sys.modules, "docling.document_converter", fake_converter_mod
    )


def test_build_wires_local_files_only_into_easyocr(monkeypatch) -> None:
    captured: dict = {}
    _install_fake_docling(monkeypatch, captured)
    # _build 會經 apply_offline_environment 寫全域 os.environ。先用 monkeypatch
    # 記住原值(setenv 由 pytest 在 teardown 還原),免得這條測試跑完不還原、
    # 造成檔案收集順序相依。
    monkeypatch.setenv("HF_HUB_OFFLINE", "0")
    monkeypatch.setenv("TRANSFORMERS_OFFLINE", "0")

    converter = DoclingConverter(local_files_only=True)
    converter._build(["ch_tra", "en"], table_structure=True, picture_description=False)

    assert captured["pipeline_kwargs"]["do_ocr"] is True
    assert captured["pipeline_kwargs"]["artifacts_path"] == "/var/anila/docling-artifacts"
    # 離線宣稱 → EasyOCR 不准自動下載(local_files_only 真的接進選項)。
    assert captured["easyocr_kwargs"]["lang"] == ["ch_tra", "en"]
    assert captured["easyocr_kwargs"]["download_enabled"] is False


def test_build_enables_picture_image_extraction(monkeypatch) -> None:
    """2026-08-21 HIGH：沒開 generate_picture_images=True，docling 不填
    picture.image → /parse 回 images:[]（遠端化掉的一格功能）。

    拿掉這個旗標，這條測試就會紅（captured kwargs 缺這個鍵或非 True）。
    與 do_picture_description 無關：那是「生不生圖說」。
    """
    captured: dict = {}
    _install_fake_docling(monkeypatch, captured)
    monkeypatch.setenv("HF_HUB_OFFLINE", "0")
    monkeypatch.setenv("TRANSFORMERS_OFFLINE", "0")

    converter = DoclingConverter(local_files_only=True)
    converter._build(["ch_tra", "en"], table_structure=True, picture_description=False)

    assert captured["pipeline_kwargs"]["generate_picture_images"] is True


def test_build_wires_online_files_only_into_easyocr(monkeypatch) -> None:
    """=0 時 EasyOCR 端要真的開下載(download_enabled=True)。"""
    captured: dict = {}
    _install_fake_docling(monkeypatch, captured)
    monkeypatch.setenv("HF_HUB_OFFLINE", "0")
    monkeypatch.setenv("TRANSFORMERS_OFFLINE", "0")

    converter = DoclingConverter(local_files_only=False)
    converter._build(["ch_tra", "en"], table_structure=True, picture_description=False)

    assert captured["easyocr_kwargs"]["download_enabled"] is True
