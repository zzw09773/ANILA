"""Tests for ingestion.ocr — fallback heuristic and vision-API backend.

The actual vision LLM endpoint is heavy (4× H100 server); these tests
cover the parts that don't need a real model:
* the ``needs_ocr_fallback`` trigger heuristic
* the ``build_ocr_backend_from_env`` env switch
* the ``VisionApiOcrBackend`` request shape and parallel-page wiring,
  using a stub HTTP transport and a faked ``fitz`` (pymupdf) so no
  PDF rasterisation runs.
"""
from __future__ import annotations

import base64
import sys
import threading

import httpx
import pytest

from agentic_rag.ingestion.ocr import (
    VisionApiOcrBackend,
    build_ocr_backend_from_env,
    needs_ocr_fallback,
)


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    for k in (
        "PDF_OCR_FALLBACK",
        "PDF_OCR_DPI",
        "PDF_OCR_CONCURRENCY",
        "PDF_OCR_MAX_PAGES",
        "PDF_OCR_VISION_PROMPT",
        "VISION_URL",
        "VISION_MODEL",
        "VISION_API_KEY",
        "VISION_VERIFY_SSL",
    ):
        monkeypatch.delenv(k, raising=False)


# ---------------------------------------------------------------------------
# needs_ocr_fallback
# ---------------------------------------------------------------------------

def test_short_extraction_triggers_ocr():
    assert needs_ocr_fallback("") is True
    assert needs_ocr_fallback("   ") is True
    assert needs_ocr_fallback("hi") is True


def test_normal_extraction_does_not_trigger():
    body = "申誡之條件依陸海空軍懲罰法第八條規定如下，凡軍人違反軍紀者..." * 3
    assert needs_ocr_fallback(body) is False


def test_high_placeholder_density_triggers_ocr():
    # Mostly <?> sentinels — classic font-subsetted PDF symptom.
    junk = "<?>" * 50 + "abc"
    assert needs_ocr_fallback(junk) is True


def test_low_placeholder_density_does_not_trigger():
    body = "申誡之條件依陸海空軍懲罰法第八條規定如下" * 5 + "<?>"
    assert needs_ocr_fallback(body) is False


# ---------------------------------------------------------------------------
# build_ocr_backend_from_env
# ---------------------------------------------------------------------------

def test_disabled_by_default():
    assert build_ocr_backend_from_env() is None


def test_enabled_without_vision_url_returns_none(monkeypatch):
    monkeypatch.setenv("PDF_OCR_FALLBACK", "true")
    monkeypatch.setenv("VISION_MODEL", "meta/llama-4-maverick")
    assert build_ocr_backend_from_env() is None


def test_enabled_without_vision_model_returns_none(monkeypatch):
    monkeypatch.setenv("PDF_OCR_FALLBACK", "true")
    monkeypatch.setenv("VISION_URL", "http://172.16.120.35/v1")
    assert build_ocr_backend_from_env() is None


def test_enabled_constructs_vision_backend_with_defaults(monkeypatch):
    monkeypatch.setenv("PDF_OCR_FALLBACK", "true")
    monkeypatch.setenv("VISION_URL", "http://172.16.120.35/v1")
    monkeypatch.setenv("VISION_MODEL", "meta/llama-4-maverick")
    monkeypatch.setenv("VISION_API_KEY", "sk-test")
    monkeypatch.setenv("VISION_VERIFY_SSL", "false")

    backend = build_ocr_backend_from_env()
    assert isinstance(backend, VisionApiOcrBackend)
    assert backend._base_url == "http://172.16.120.35/v1"
    assert backend._model == "meta/llama-4-maverick"
    assert backend._api_key == "sk-test"
    assert backend._verify_ssl is False
    assert backend._dpi == 200
    assert backend._concurrency == 4
    assert backend._max_pages == 100


def test_enabled_with_overridden_settings(monkeypatch):
    monkeypatch.setenv("PDF_OCR_FALLBACK", "true")
    monkeypatch.setenv("VISION_URL", "http://x/v1")
    monkeypatch.setenv("VISION_MODEL", "m")
    monkeypatch.setenv("PDF_OCR_DPI", "300")
    monkeypatch.setenv("PDF_OCR_CONCURRENCY", "8")
    monkeypatch.setenv("PDF_OCR_MAX_PAGES", "50")
    monkeypatch.setenv("PDF_OCR_VISION_PROMPT", "Extract text verbatim.")

    backend = build_ocr_backend_from_env()
    assert backend._dpi == 300
    assert backend._concurrency == 8
    assert backend._max_pages == 50
    assert backend._prompt == "Extract text verbatim."


# ---------------------------------------------------------------------------
# Constructor argument validation
# ---------------------------------------------------------------------------

def test_constructor_rejects_empty_url():
    with pytest.raises(ValueError, match="base_url"):
        VisionApiOcrBackend(base_url="", model="m")


def test_constructor_rejects_empty_model():
    with pytest.raises(ValueError, match="model"):
        VisionApiOcrBackend(base_url="http://x/v1", model="")


def test_constructor_clamps_concurrency_floor():
    """Concurrency=0 would deadlock the thread pool; must clamp to 1."""
    backend = VisionApiOcrBackend(
        base_url="http://x/v1", model="m", concurrency=0, max_pages=0
    )
    assert backend._concurrency == 1
    assert backend._max_pages == 1


# ---------------------------------------------------------------------------
# VisionApiOcrBackend.extract — full pipeline with stubs
# ---------------------------------------------------------------------------

class _FakePixmap:
    def __init__(self, content: bytes) -> None:
        self._content = content

    def tobytes(self, fmt: str) -> bytes:
        assert fmt == "png"
        return self._content


class _FakePage:
    def __init__(self, content: bytes) -> None:
        self._content = content

    def get_pixmap(self, dpi: int) -> _FakePixmap:
        return _FakePixmap(self._content)


class _FakePdfDoc:
    def __init__(self, page_count: int) -> None:
        self._pages = [
            _FakePage(f"PNG_BYTES_PAGE_{i + 1}".encode()) for i in range(page_count)
        ]
        self.closed = False

    def __len__(self) -> int:
        return len(self._pages)

    def __getitem__(self, idx: int) -> _FakePage:
        return self._pages[idx]

    def close(self) -> None:
        self.closed = True


class _FakeFitz:
    """Drop-in replacement for the ``fitz`` module, returning a configured doc."""

    def __init__(self, doc: _FakePdfDoc) -> None:
        self._doc = doc

    def open(self, path: str) -> _FakePdfDoc:  # noqa: D401
        return self._doc


def _install_fake_fitz(monkeypatch, page_count: int) -> _FakePdfDoc:
    doc = _FakePdfDoc(page_count)
    monkeypatch.setitem(sys.modules, "fitz", _FakeFitz(doc))
    return doc


def _capturing_post(captured: list[dict], canned_text_per_page: list[str]):
    """Build a stub for ``httpx.Client.post`` that records each payload.

    Returns canned text in PNG-byte order so we can assert that the
    backend re-orders pages by page number when rejoining results.
    """
    lock = threading.Lock()

    def _post(self, url: str, *, headers=None, json=None, **kwargs):  # noqa: A002
        with lock:
            captured.append({
                "url": url,
                "headers": dict(headers or {}),
                "body": json,
            })
            # Identify which page this is by decoding the embedded data URL.
            assert isinstance(json, dict)
            content_blocks = json["messages"][0]["content"]
            data_url = content_blocks[1]["image_url"]["url"]
            png_b64 = data_url.split(",", 1)[1]
            png_bytes = base64.b64decode(png_b64)
            page_marker = png_bytes.decode()  # e.g. "PNG_BYTES_PAGE_2"
            page_num = int(page_marker.split("_")[-1])

        text = canned_text_per_page[page_num - 1]
        payload = {
            "choices": [{"message": {"content": text}}]
        }
        return httpx.Response(200, json=payload, request=httpx.Request("POST", url))

    return _post


def test_extract_sends_one_request_per_page_with_correct_shape(monkeypatch):
    _install_fake_fitz(monkeypatch, page_count=2)
    captured: list[dict] = []

    monkeypatch.setattr(
        "httpx.Client.post",
        _capturing_post(captured, ["page one text", "page two text"]),
        raising=True,
    )

    backend = VisionApiOcrBackend(
        base_url="http://172.16.120.35/v1",
        model="meta/llama-4-maverick",
        api_key="sk-test",
        prompt="extract verbatim",
        dpi=200,
        concurrency=2,
    )
    out = backend.extract("dummy.pdf")

    assert len(captured) == 2
    for req in captured:
        assert req["url"] == "http://172.16.120.35/v1/chat/completions"
        assert req["headers"]["Authorization"] == "Bearer sk-test"
        assert req["headers"]["Content-Type"] == "application/json"
        assert req["body"]["model"] == "meta/llama-4-maverick"
        assert req["body"]["temperature"] == 0.0
        msg = req["body"]["messages"][0]
        assert msg["role"] == "user"
        assert msg["content"][0] == {"type": "text", "text": "extract verbatim"}
        assert msg["content"][1]["type"] == "image_url"
        assert msg["content"][1]["image_url"]["url"].startswith(
            "data:image/png;base64,"
        )

    # Pages reassembled in order regardless of completion order.
    assert out == "page one text\n\npage two text"


def test_extract_omits_auth_when_api_key_blank(monkeypatch):
    _install_fake_fitz(monkeypatch, page_count=1)
    captured: list[dict] = []
    monkeypatch.setattr(
        "httpx.Client.post",
        _capturing_post(captured, ["only page"]),
        raising=True,
    )
    backend = VisionApiOcrBackend(
        base_url="http://x/v1", model="m", api_key="", concurrency=1
    )
    backend.extract("dummy.pdf")
    assert "Authorization" not in captured[0]["headers"]


def test_extract_drops_no_text_marker_pages(monkeypatch):
    """[NO_TEXT] pages from the vision LLM must not pollute the output."""
    _install_fake_fitz(monkeypatch, page_count=3)
    captured: list[dict] = []
    monkeypatch.setattr(
        "httpx.Client.post",
        _capturing_post(captured, ["有文字頁", "[NO_TEXT]", "另一段文字"]),
        raising=True,
    )
    backend = VisionApiOcrBackend(
        base_url="http://x/v1", model="m", concurrency=2
    )
    out = backend.extract("dummy.pdf")
    assert out == "有文字頁\n\n另一段文字"


def test_extract_truncates_at_max_pages(monkeypatch, caplog):
    """If the PDF has more pages than PDF_OCR_MAX_PAGES, only first N are sent."""
    _install_fake_fitz(monkeypatch, page_count=10)
    captured: list[dict] = []
    monkeypatch.setattr(
        "httpx.Client.post",
        _capturing_post(captured, [f"text {i}" for i in range(10)]),
        raising=True,
    )
    backend = VisionApiOcrBackend(
        base_url="http://x/v1", model="m", concurrency=2, max_pages=3
    )
    with caplog.at_level("WARNING"):
        out = backend.extract("dummy.pdf")
    assert len(captured) == 3
    assert out == "text 0\n\ntext 1\n\ntext 2"
    assert any("truncating" in rec.message.lower() for rec in caplog.records)


def test_extract_continues_when_one_page_fails(monkeypatch):
    """Per-page failures are isolated — surviving pages must still come through."""
    _install_fake_fitz(monkeypatch, page_count=3)

    def _post(self, url, *, headers=None, json=None, **kwargs):  # noqa: A002
        content_blocks = json["messages"][0]["content"]
        png_bytes = base64.b64decode(
            content_blocks[1]["image_url"]["url"].split(",", 1)[1]
        )
        marker = png_bytes.decode()
        if marker.endswith("_2"):
            return httpx.Response(500, json={"error": "boom"}, request=httpx.Request("POST", url))
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": f"ok {marker[-1]}"}}]},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr("httpx.Client.post", _post, raising=True)

    backend = VisionApiOcrBackend(
        base_url="http://x/v1", model="m", concurrency=3
    )
    out = backend.extract("dummy.pdf")
    # Page 2 failed → its slot is dropped, page 1 + page 3 survive in order.
    assert out == "ok 1\n\nok 3"


def test_extract_without_pymupdf_raises_importerror(monkeypatch):
    """When pymupdf isn't installed the lazy import must surface a helpful error."""
    monkeypatch.setitem(sys.modules, "fitz", None)
    backend = VisionApiOcrBackend(base_url="http://x/v1", model="m")
    with pytest.raises(ImportError, match=r"agentic-rag\[rag\]"):
        backend.extract("dummy.pdf")
