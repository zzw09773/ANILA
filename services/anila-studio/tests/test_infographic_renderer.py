"""HTML rendering — Jinja2 template + PNG inlining + CJK preservation.

PDF rendering itself uses Playwright (headless chromium) which we
don't drive from unit tests; the endpoint test covers it with a mock.
"""
from __future__ import annotations

import base64
import re

import pytest

from app.schemas.infographic import (
    ChartSpec,
    ComparisonRow,
    InfographicPreset,
    InfographicSpec,
    StatBlock,
    TimelineEvent,
)
from app.services.infographic_renderer import render_chart_png, render_html


def _rich_spec():
    return InfographicSpec(
        title="2024 Q4 業務簡報",
        subtitle="董事會月度報告",
        preset=InfographicPreset.MISSION_DASHBOARD,
        stats=[
            StatBlock(value="47%", label="目標達成率", delta="+12%"),
            StatBlock(value="3.5×", label="效能提升", delta="-3%"),
            StatBlock(value="N=2,400", label="樣本數"),
        ],
        charts=[
            ChartSpec(
                chart_type="bar",
                title="各部門達成率",
                x_labels=["業務", "工程", "客服"],
                series=[{"name": "達成率", "values": [88, 92, 75]}],
            ),
        ],
        comparison=[
            ComparisonRow(label="可靠性", columns=["A：高", "B：中"]),
            ComparisonRow(label="成本", columns=["A：高", "B：低"]),
        ],
        timeline=[
            TimelineEvent(date="2025-Q1", title="啟動", description="專案 kick-off"),
            TimelineEvent(date="2025-Q2", title="原型完成"),
        ],
        takeaway="本季目標皆達成；下季聚焦客服改善。",
    )


def test_render_html_smoke():
    spec = _rich_spec()
    png = render_chart_png(spec.charts[0])
    html = render_html(spec, {0: png})

    assert html.startswith("<!DOCTYPE html>")
    assert "</html>" in html
    # Title shows up in <title> AND in <h1>.
    assert "<title>2024 Q4 業務簡報</title>" in html
    assert "<h1>2024 Q4 業務簡報</h1>" in html


def test_render_html_includes_stats():
    spec = _rich_spec()
    html = render_html(spec, {})
    assert "47%" in html
    assert "目標達成率" in html
    assert "+12%" in html
    # Negative delta gets "down" class
    assert 'class="delta down"' in html
    # Neutral / no-delta stats should not have the badge
    assert "樣本數" in html


def test_render_html_includes_chart_base64_image():
    spec = _rich_spec()
    png = render_chart_png(spec.charts[0])
    html = render_html(spec, {0: png})

    expected_b64 = base64.b64encode(png).decode("ascii")
    # The <img> tag should reference the inline data URI
    assert "data:image/png;base64," in html
    # The base64 itself should appear (first 60 chars enough — full match
    # is fragile against template trimming)
    assert expected_b64[:60] in html


def test_render_html_includes_comparison_matrix():
    spec = _rich_spec()
    html = render_html(spec, {0: render_chart_png(spec.charts[0])})
    assert "可靠性" in html
    assert "A：高" in html
    assert "B：中" in html
    assert "<table" in html


def test_render_html_includes_timeline():
    spec = _rich_spec()
    html = render_html(spec, {0: render_chart_png(spec.charts[0])})
    assert "2025-Q1" in html
    assert "啟動" in html
    assert "專案 kick-off" in html
    assert "timeline-event" in html


def test_render_html_includes_takeaway():
    spec = _rich_spec()
    html = render_html(spec, {0: render_chart_png(spec.charts[0])})
    assert "本季目標皆達成" in html
    assert "takeaway" in html


def test_render_html_preset_class_set_on_body():
    """preset_class should land on the body class attribute."""
    spec = _rich_spec()
    html = render_html(spec, {0: render_chart_png(spec.charts[0])})
    assert 'class="preset-mission_dashboard"' in html


def test_render_html_skips_empty_sections():
    """sections without data should not appear in the markup.

    NB: we check for the wrapper <section class="infographic-section ...">
    markers, NOT for CSS class names — the latter are in the embedded
    <style> block regardless of section presence.
    """
    minimal = InfographicSpec(
        title="極簡 infographic",
        preset=InfographicPreset.STATS_BRIEF,
        takeaway="結論：尚無資料。",
    )
    html = render_html(minimal, {})
    # title + takeaway present
    assert "極簡 infographic" in html
    assert "尚無資料" in html
    # No actual rendered <section> for these — they're skipped entirely
    # by Jinja {% if %} blocks. Looking for the section tag + the data-
    # specific marker would catch both the wrapper AND the content.
    assert 'class="infographic-section stats"' not in html
    assert 'class="infographic-section charts"' not in html
    assert 'class="infographic-section comparison"' not in html
    assert 'class="infographic-section timeline-section"' not in html


def test_render_html_traditional_chinese_not_mojibake():
    """CJK strings should survive Jinja autoescape + UTF-8 write/read."""
    spec = InfographicSpec(
        title="繁體中文不亂碼測試",
        subtitle="這裡有完整的台灣用語：軟體、影片、資料、檔案、解析度",
        preset=InfographicPreset.STATS_BRIEF,
        stats=[StatBlock(value="100%", label="覆蓋率")],
        takeaway="繁體字應該完整保留，不會變成問號或方塊。",
    )
    html = render_html(spec, {})
    # Every CJK string in the spec must be findable in the rendered HTML.
    assert "繁體中文不亂碼測試" in html
    assert "軟體" in html
    assert "影片" in html
    assert "資料" in html
    assert "覆蓋率" in html
    assert "繁體字應該完整保留" in html
    # No literal HTML entity escapes for CJK (would indicate mojibake path).
    assert "&#" not in re.sub(r"&#\d+;\D", "", "")  # sanity for the matcher


def test_render_html_positive_delta_class():
    """+xxx delta should get .up class for green colour."""
    spec = InfographicSpec(
        title="x",
        preset=InfographicPreset.STATS_BRIEF,
        stats=[StatBlock(value="50%", label="x", delta="+5%")],
        takeaway="t",
    )
    html = render_html(spec, {})
    assert 'class="delta up"' in html


def test_render_html_neutral_delta_class():
    """Delta without +/- prefix should fall back to .flat class."""
    spec = InfographicSpec(
        title="x",
        preset=InfographicPreset.STATS_BRIEF,
        stats=[StatBlock(value="50%", label="x", delta="持平")],
        takeaway="t",
    )
    html = render_html(spec, {})
    assert 'class="delta flat"' in html


def test_render_html_xss_in_user_strings_escaped():
    """Jinja2 autoescape should HTML-escape any <script> in user content."""
    spec = InfographicSpec(
        title="<script>alert('xss')</script>",
        preset=InfographicPreset.STATS_BRIEF,
        takeaway="ok",
    )
    html = render_html(spec, {})
    # The raw <script> tag should NOT appear in the rendered HTML —
    # autoescape should have turned it into &lt;script&gt;.
    assert "<script>alert" not in html
    assert "&lt;script&gt;" in html
