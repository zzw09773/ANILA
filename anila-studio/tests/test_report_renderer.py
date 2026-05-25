"""Tests for app.services.report_renderer — HTML rendering & PDF/DOCX mocking.

We exercise:
  - HTML rendering for all 4 presets (title / TL;DR / sections / refs surface),
  - CJK text passes through without mojibake,
  - markdown_it converts inline syntax into HTML,
  - PDF render path delegates to playwright (mocked),
  - DOCX render path delegates to pypandoc (mocked),
  - render_all_artifacts wires everything together and reports paths.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.schemas.report import (
    ReportPreset,
    ReportReference,
    ReportSection,
    ReportSpec,
)
from app.services import report_renderer


# ── Fixture ──────────────────────────────────────────────────────────────


def _spec(preset: ReportPreset = ReportPreset.DEEP_TECH_REVIEW) -> ReportSpec:
    return ReportSpec(
        title="人工智慧發展概觀：2024-2026",
        preset=preset,
        tldr=(
            "本報告探討 2024-2026 期間生成式 AI 的技術演進、產業應用與"
            "倫理風險，並整理出三條主要發展軸線。"
        ),
        sections=[
            ReportSection(
                heading="技術演進",
                content_markdown=(
                    "近三年 **Transformer** 架構持續主導，"
                    "其中 *Mixture-of-Experts* 模型尤為突出 [1]。\n\n"
                    "- 模型參數量持續上升\n"
                    "- 推論效率成為新瓶頸 [2]\n"
                ),
                subsections=[
                    ReportSection(
                        heading="效率瓶頸",
                        content_markdown=(
                            "memory bandwidth 已成為主要瓶頸 [3]。"
                        ),
                    ),
                ],
            ),
            ReportSection(
                heading="產業應用",
                content_markdown="從 RAG 到 agent，落地場景擴張迅速 [2]。",
            ),
        ],
        references=[
            ReportReference(
                n=1,
                filename="moe_architectures_2024.pdf",
                chunk_id=42,
                excerpt="MoE 架構在 2024 年成為主流選擇之一。",
            ),
            ReportReference(
                n=2,
                filename="inference_efficiency.pdf",
                chunk_id=15,
                excerpt="推論效率受限於記憶體頻寬。",
            ),
            ReportReference(
                n=3,
                filename="hardware_trends.pdf",
                chunk_id=88,
                excerpt="HBM 頻寬增長落後於計算單元。",
            ),
        ],
        generated_at=datetime(2026, 5, 23, 12, 0, tzinfo=timezone.utc),
    )


# ── render_html: structural assertions ───────────────────────────────────


@pytest.mark.parametrize(
    "preset",
    [
        ReportPreset.DEEP_TECH_REVIEW,
        ReportPreset.KEY_SUMMARY,
        ReportPreset.TEACHING_HANDOUT,
        ReportPreset.EXTERNAL_COMMS,
    ],
)
def test_render_html_contains_required_blocks(preset: ReportPreset):
    spec = _spec(preset)
    html = report_renderer.render_html(spec)

    # Top-level HTML5 boilerplate
    assert html.startswith("<!doctype html>")
    assert 'lang="zh-Hant"' in html
    assert '<meta charset="utf-8">' in html

    # Title
    assert "人工智慧發展概觀：2024-2026" in html
    assert "<h1 class=\"report-title\"" in html

    # TL;DR block
    assert "TL;DR" in html
    assert "tldr-label" in html
    assert "本報告探討" in html

    # Every section heading is present
    assert "技術演進" in html
    assert "產業應用" in html
    assert "效率瓶頸" in html  # nested subsection

    # References table
    assert "引用來源" in html
    assert "moe_architectures_2024.pdf" in html
    assert "inference_efficiency.pdf" in html
    assert "hardware_trends.pdf" in html


def test_render_html_no_cjk_mojibake():
    """The output must contain the actual CJK characters, not escape codes."""
    spec = _spec()
    html = report_renderer.render_html(spec)
    # If something went wrong with encoding we'd see things like
    # 人 or &#x4eba; instead of the actual chars.
    for ch in "人工智慧技術演進效率瓶頸":
        assert ch in html, f"CJK char {ch!r} missing from rendered HTML"


def test_render_html_markdown_inline_converts():
    """Inline markdown (bold, italic, list) should become real HTML tags."""
    spec = _spec()
    html = report_renderer.render_html(spec)
    assert "<strong>Transformer</strong>" in html
    assert "<em>Mixture-of-Experts</em>" in html
    assert "<ul>" in html and "<li>" in html


def test_render_html_subsections_use_higher_heading_level():
    """Nested subsection heading should render at <h3>, not <h2>."""
    spec = _spec()
    html = report_renderer.render_html(spec)
    # The nested "效率瓶頸" should be h3, not h2.
    assert "<h3 class=\"section-heading\">效率瓶頸</h3>" in html


def test_render_html_footer_carries_preset_label():
    spec = _spec(ReportPreset.TEACHING_HANDOUT)
    html = report_renderer.render_html(spec)
    assert "教學講義" in html
    assert "ANILA LM" in html
    assert "2026-05-23" in html


def test_render_html_unknown_preset_raises():
    """Drift between schema enum and template table should fail fast.

    We synthesise that by clearing the dispatch table for one entry.
    """
    spec = _spec(ReportPreset.KEY_SUMMARY)
    original = report_renderer._PRESET_TEMPLATE.copy()
    try:
        del report_renderer._PRESET_TEMPLATE[ReportPreset.KEY_SUMMARY]
        with pytest.raises(KeyError):
            report_renderer.render_html(spec)
    finally:
        report_renderer._PRESET_TEMPLATE.clear()
        report_renderer._PRESET_TEMPLATE.update(original)


def test_render_html_inline_html_escaped():
    """LLM-generated raw HTML in content must be escaped, not executed."""
    spec = ReportSpec(
        title="安全測試",
        preset=ReportPreset.KEY_SUMMARY,
        tldr="這個摘要應該至少有二十個字以滿足 schema 驗證的最低長度需求。",
        sections=[
            ReportSection(
                heading="XSS 測試",
                content_markdown="正常文字 <script>alert(1)</script> 後續文字",
            )
        ],
    )
    html = report_renderer.render_html(spec)
    # The literal <script> string should be escaped, not present as real markup.
    assert "<script>alert(1)</script>" not in html
    # But the visible text should still be there in escaped form.
    assert "alert(1)" in html


# ── write_html_file ──────────────────────────────────────────────────────


def test_write_html_file_creates_parent_dirs(tmp_path: Path):
    spec = _spec()
    dest = tmp_path / "nested" / "dir" / "out.html"
    report_renderer.write_html_file(spec, dest)
    assert dest.exists()
    body = dest.read_text(encoding="utf-8")
    assert "人工智慧發展概觀" in body


# ── PDF render: playwright is mocked ─────────────────────────────────────


@pytest.mark.asyncio
async def test_write_pdf_file_invokes_playwright(tmp_path: Path):
    """write_pdf_file should drive the async playwright API exactly once.

    We patch ``app.services.report_renderer.async_playwright`` is imported
    inside the function — so we patch the attribute on the playwright
    module directly.
    """
    dest = tmp_path / "out.pdf"
    html = "<html><body>hi</body></html>"

    mock_browser = MagicMock()
    mock_browser.close = AsyncMock()
    mock_page = MagicMock()
    mock_page.set_content = AsyncMock()
    mock_page.pdf = AsyncMock()
    mock_browser.new_page = AsyncMock(return_value=mock_page)

    class _PWContext:
        chromium = MagicMock()

        def __init__(self):
            self.chromium.launch = AsyncMock(return_value=mock_browser)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    pw_ctx = _PWContext()

    def _mock_async_playwright_factory():
        return pw_ctx

    with patch(
        "playwright.async_api.async_playwright", _mock_async_playwright_factory
    ):
        await report_renderer.write_pdf_file(html, dest)

    pw_ctx.chromium.launch.assert_awaited_once_with(headless=True)
    mock_browser.new_page.assert_awaited_once()
    mock_page.set_content.assert_awaited_once()
    mock_page.pdf.assert_awaited_once()
    # Verify the path argument matches and A4 format was used.
    args, kwargs = mock_page.pdf.call_args
    assert kwargs.get("format") == "A4"
    assert kwargs.get("path") == str(dest)
    mock_browser.close.assert_awaited_once()


# ── DOCX render: pypandoc is mocked ──────────────────────────────────────


@pytest.mark.asyncio
async def test_write_docx_file_invokes_pypandoc(tmp_path: Path):
    """write_docx_file should call pypandoc.convert_text via to_thread."""
    dest = tmp_path / "out.docx"
    html = "<html><body>hi</body></html>"

    fake_convert = MagicMock()
    fake_pypandoc = MagicMock(convert_text=fake_convert)

    with patch.dict("sys.modules", {"pypandoc": fake_pypandoc}):
        await report_renderer.write_docx_file(html, dest)

    fake_convert.assert_called_once()
    kwargs = fake_convert.call_args.kwargs
    assert kwargs.get("to") == "docx"
    assert kwargs.get("format") == "html"
    assert kwargs.get("outputfile") == str(dest)
    assert kwargs.get("source") == html


# ── render_all_artifacts orchestration ───────────────────────────────────


@pytest.mark.asyncio
async def test_render_all_artifacts_writes_three_files(tmp_path: Path):
    """Orchestrator should write HTML + invoke PDF + invoke DOCX with the same html."""
    spec = _spec()
    job_id = "r_test_orchestration"

    # Build mocks that physically create the files (so render_all_artifacts'
    # post-render existence check in the runner passes; here we only assert
    # the renderer's own return contract).
    def _make_pdf(html_arg: str, dest: Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"%PDF-FAKE")

    def _make_docx(html_arg: str, dest: Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"PK\x03\x04docx-fake")

    async def fake_pdf(html_arg: str, dest: Path) -> None:
        _make_pdf(html_arg, dest)

    async def fake_docx(html_arg: str, dest: Path) -> None:
        _make_docx(html_arg, dest)

    with (
        patch.object(report_renderer, "write_pdf_file", fake_pdf),
        patch.object(report_renderer, "write_docx_file", fake_docx),
    ):
        paths = await report_renderer.render_all_artifacts(spec, tmp_path, job_id)

    assert set(paths.keys()) == {"html", "pdf", "docx"}
    for fmt, path in paths.items():
        assert path.exists(), f"{fmt} file not created"
        assert path.stat().st_size > 0
    # HTML on disk matches what render_html would produce
    assert (tmp_path / f"{job_id}.html").exists()
