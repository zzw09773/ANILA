"""model.py helper tests — no docling weights loaded.

Covers the version-drift-tolerant helpers (safe export / title / page count /
picture extraction / ocr flag). These are the parts that must keep working
across docling data-model churn; the actual ``DocumentConverter`` is a thin
wrapper over docling's own surface and is not unit-tested here (it would pull
in the model weights).
"""

from __future__ import annotations

from app.model import (
    _normalize_title,
    _safe_export_markdown,
    _safe_page_count,
    _safe_ocr_flag,
    _safe_title,
)


class _Doc:
    def __init__(self, *, title=None, name=None, pages=None, pictures=None):
        self.title = title
        self.name = name
        self.pages = pages
        self.pictures = pictures

    def export_to_markdown(self):
        return "  # hello  "


class _PageScore:
    def __init__(self, ocr_score):
        self.ocr_score = ocr_score


class _Confidence:
    def __init__(self, pages):
        self.pages = pages


class _Result:
    """mock ConversionResult：只帶 _safe_ocr_flag 現在讀的 confidence.pages。"""
    def __init__(self, confidence):
        self.confidence = confidence


def test_export_markdown_strips_and_stringifies():
    assert _safe_export_markdown(_Doc()) == "# hello"


def test_export_markdown_returns_empty_on_failure():
    class _Broken(_Doc):
        def export_to_markdown(self):
            raise RuntimeError("boom")

    assert _safe_export_markdown(_Broken()) == ""


def test_safe_title_prefers_document_title():
    assert _safe_title(_Doc(title="  Real Title  "), fallback="fallback") == "Real Title"


def test_safe_title_falls_back_when_no_title():
    assert _safe_title(_Doc(pages=[]), fallback="fallback") == "fallback"


def test_safe_title_prefers_original_name_over_document_name():
    """批次①:docling 會把 document.name 填成 input stem(如 'L312' / 服務
    端暫存名),卻幾乎不設 document.title(實測 None)。舊 _safe_title 先取
    name → 拿到的永遠是暫存名,原始檔名(fallback)輪不到。修成 fallback
    優先於 name——只有真正的 document.title 才贏(那不是 input 名)。"""
    assert _safe_title(_Doc(name="tmp-stem"), fallback="L312.pdf") == "L312.pdf"


def test_normalize_title_strips_control_chars():
    # C0(\r\n\t) 加 DEL 都要剝——title 由 uploader 控制,長 foo、塞控制
    # 字元的檔名會原封進回傳。這層是回傳前的邊界(2026-08-20 revision)。
    assert _normalize_title("clean") == "clean"
    assert _normalize_title("a\r\nb\tc") == "abc"
    assert _normalize_title("a\x7fb") == "ab"  # DEL
    assert _normalize_title("  padded \t") == "padded"


def test_normalize_title_caps_at_255():
    assert len(_normalize_title("x" * 300)) == 255
    assert _normalize_title("x" * 255) == "x" * 255


def test_normalize_title_non_str_is_empty():
    assert _normalize_title(None) == ""
    assert _normalize_title(123) == ""


def test_safe_title_normalizes_control_chars_from_document():
    # _safe_title 的每一出口都走 _normalize_title:document.title 帶控制字元
    # 也要剝,不能只靠 fallback 那個乾淨出口。
    assert _safe_title(_Doc(title="T\r\nX"), fallback="f") == "TX"


def test_safe_page_count():
    assert _safe_page_count(_Doc(pages=[1, 2, 3])) == 3


def test_safe_page_count_none():
    assert _safe_page_count(_Doc()) is None


def test_safe_ocr_flag_detects_ocr_timing_key():
    """OCR 真跑 → 該頁 ocr_score 被設成 float 均值 → 判 True。"""
    assert _safe_ocr_flag(_Result(_Confidence({1: _PageScore(0.73)}))) is True


def test_safe_ocr_flag_false_when_no_ocr_key():
    """OCR 沒跑(文字 PDF 頁) → ocr_score 維持預設 np.nan → 判 False。"""
    import math

    assert _safe_ocr_flag(_Result(_Confidence({1: _PageScore(math.nan)}))) is False


def test_safe_ocr_flag_false_when_no_timings():
    """完全沒有 confidence/pages（非 dict / None）→ 保守回 False，不炸。"""
    assert _safe_ocr_flag(_Result(None)) is False
