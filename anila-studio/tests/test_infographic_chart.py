"""matplotlib chart rendering — PNG magic bytes + dispatch coverage.

These tests do NOT verify the visual quality of the charts (too fragile
against matplotlib version bumps). They DO verify:

1. render_chart_png returns bytes (not file path).
2. The bytes start with the PNG magic number (0x89 'PNG\\r\\n\\x1a\\n').
3. Every chart_type in the Literal dispatches without raising.
4. Edge cases: empty series, single-series, multi-series, value coercion
   (string → float), pie/donut with zero values.
"""
from __future__ import annotations

import pytest

from app.schemas.infographic import ChartSpec
from app.services.infographic_renderer import render_chart_png


PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _is_png(blob: bytes) -> bool:
    return blob.startswith(PNG_MAGIC)


def _chart(chart_type, *, series=None, x_labels=None):
    return ChartSpec(
        chart_type=chart_type,
        title="範例圖表",
        x_labels=x_labels or ["A", "B", "C"],
        series=series or [{"name": "default", "values": [1, 2, 3]}],
    )


@pytest.mark.parametrize(
    "chart_type",
    ["bar", "line", "pie", "donut", "hbar"],
)
def test_render_each_chart_type_produces_png(chart_type):
    spec = _chart(chart_type)
    png = render_chart_png(spec)
    assert isinstance(png, bytes)
    assert _is_png(png), f"chart_type={chart_type} produced non-PNG bytes"
    # Reasonable lower bound — a real chart at 8x5 inches @120dpi is ≥3 KB.
    assert len(png) > 1000


def test_render_bar_multi_series():
    spec = _chart(
        "bar",
        series=[
            {"name": "2024", "values": [10, 20, 30]},
            {"name": "2025", "values": [15, 25, 35]},
        ],
    )
    png = render_chart_png(spec)
    assert _is_png(png)


def test_render_line_multi_series():
    spec = _chart(
        "line",
        series=[
            {"name": "α", "values": [1.0, 2.0, 3.0]},
            {"name": "β", "values": [0.5, 1.5, 2.5]},
        ],
    )
    png = render_chart_png(spec)
    assert _is_png(png)


def test_render_pie_with_string_values_coerces():
    """LLM may emit string numbers; renderer must coerce."""
    spec = _chart(
        "pie",
        series=[{"name": "default", "values": ["10", "20", "30"]}],
    )
    png = render_chart_png(spec)
    assert _is_png(png)


def test_render_pie_all_zero_values_still_renders():
    """Defensive: 0 values should fall back to a single placeholder wedge."""
    spec = _chart(
        "pie",
        series=[{"name": "default", "values": [0, 0, 0]}],
    )
    png = render_chart_png(spec)
    assert _is_png(png)


def test_render_donut_has_hole():
    """Smoke test — donut path should not raise, output remains a PNG."""
    spec = _chart(
        "donut",
        series=[{"name": "default", "values": [40, 30, 30]}],
    )
    png = render_chart_png(spec)
    assert _is_png(png)


def test_render_hbar_uses_horizontal_orientation():
    spec = _chart("hbar", series=[{"name": "default", "values": [10, 20, 30]}])
    png = render_chart_png(spec)
    assert _is_png(png)


def test_render_chart_handles_cjk_title_and_labels():
    """CJK should not crash; we don't assert font rendering (Noto fallback
    is environment-dependent), only that no exception is raised."""
    spec = ChartSpec(
        chart_type="bar",
        title="2024 Q4 業務指標達成率",
        x_labels=["業務", "工程", "客服", "行政"],
        series=[{"name": "達成率", "values": [88, 92, 75, 81]}],
    )
    png = render_chart_png(spec)
    assert _is_png(png)


def test_render_bar_pads_short_series_to_match_x_labels():
    """If series.values is shorter than x_labels, renderer pads with 0."""
    spec = ChartSpec(
        chart_type="bar",
        title="x",
        x_labels=["A", "B", "C", "D"],
        series=[{"name": "s", "values": [10]}],  # 1 value but 4 labels
    )
    png = render_chart_png(spec)
    assert _is_png(png)


def test_render_bar_truncates_long_series_to_match_x_labels():
    """If series.values is longer than x_labels, renderer truncates."""
    spec = ChartSpec(
        chart_type="bar",
        title="x",
        x_labels=["A", "B"],
        series=[{"name": "s", "values": [1, 2, 3, 4, 5]}],
    )
    png = render_chart_png(spec)
    assert _is_png(png)
