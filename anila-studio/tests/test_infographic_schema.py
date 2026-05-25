"""Pydantic validation tests for InfographicSpec + sub-models.

Lower-level than the renderer / endpoint tests — they assert the
schema's *shape contract* so downstream changes that break field
constraints fail fast.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas.infographic import (
    ChartSpec,
    ComparisonRow,
    GenerateInfographicRequest,
    InfographicPreset,
    InfographicSpec,
    StatBlock,
    TimelineEvent,
)


# ── StatBlock ──────────────────────────────────────────────────────────────


def test_stat_block_minimum_fields():
    s = StatBlock(value="47%", label="目標達成率")
    assert s.value == "47%"
    assert s.label == "目標達成率"
    assert s.delta is None
    assert s.icon is None


def test_stat_block_full_fields():
    s = StatBlock(value="3.5×", label="效能提升", delta="+12%", icon="metrics")
    assert s.delta == "+12%"
    assert s.icon == "metrics"


def test_stat_block_rejects_empty_value():
    with pytest.raises(ValidationError):
        StatBlock(value="", label="label")


def test_stat_block_rejects_oversized_value():
    with pytest.raises(ValidationError):
        StatBlock(value="x" * 21, label="label")


# ── ChartSpec ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "chart_type",
    ["bar", "line", "pie", "donut", "hbar"],
)
def test_chart_spec_valid_chart_types(chart_type):
    c = ChartSpec(
        chart_type=chart_type,
        title="範例圖表",
        x_labels=["A", "B", "C"],
        series=[{"name": "default", "values": [1, 2, 3]}],
    )
    assert c.chart_type == chart_type


def test_chart_spec_rejects_unknown_chart_type():
    with pytest.raises(ValidationError):
        ChartSpec(
            chart_type="radar",  # not in Literal
            title="x",
            x_labels=["A"],
            series=[{"name": "s", "values": [1]}],
        )


def test_chart_spec_rejects_empty_x_labels():
    with pytest.raises(ValidationError):
        ChartSpec(
            chart_type="bar",
            title="x",
            x_labels=[],
            series=[{"name": "s", "values": [1]}],
        )


def test_chart_spec_rejects_empty_series():
    with pytest.raises(ValidationError):
        ChartSpec(
            chart_type="bar",
            title="x",
            x_labels=["A"],
            series=[],
        )


# ── InfographicSpec ────────────────────────────────────────────────────────


def _minimal_spec(**overrides):
    """Build a minimal valid spec, allowing per-test field tweaks."""
    base = dict(
        title="2024 Q4 業務簡報",
        preset=InfographicPreset.STATS_BRIEF,
        takeaway="本季關鍵指標達成預期、Q1 持續推進。",
    )
    base.update(overrides)
    return InfographicSpec(**base)


def test_infographic_spec_minimum_required_fields():
    spec = _minimal_spec()
    assert spec.title == "2024 Q4 業務簡報"
    assert spec.preset is InfographicPreset.STATS_BRIEF
    assert spec.takeaway.startswith("本季")
    assert spec.stats == []
    assert spec.charts == []
    assert spec.comparison is None
    assert spec.timeline is None


def test_infographic_spec_rejects_missing_takeaway():
    with pytest.raises(ValidationError):
        InfographicSpec(
            title="x",
            preset=InfographicPreset.STATS_BRIEF,
            # takeaway omitted → required
        )


def test_infographic_spec_max_six_stats():
    six_stats = [StatBlock(value=f"{i}%", label=f"指標{i}") for i in range(6)]
    spec = _minimal_spec(stats=six_stats)
    assert len(spec.stats) == 6

    with pytest.raises(ValidationError):
        _minimal_spec(
            stats=[StatBlock(value=f"{i}%", label=f"指標{i}") for i in range(7)]
        )


def test_infographic_spec_max_three_charts():
    chart = ChartSpec(
        chart_type="bar",
        title="範例",
        x_labels=["A"],
        series=[{"name": "s", "values": [1]}],
    )
    spec = _minimal_spec(charts=[chart, chart, chart])
    assert len(spec.charts) == 3

    with pytest.raises(ValidationError):
        _minimal_spec(charts=[chart, chart, chart, chart])


def test_infographic_spec_preset_enum_coercion_from_string():
    spec = InfographicSpec(
        title="x",
        preset="mission_dashboard",  # string, not Enum
        takeaway="t",
    )
    assert spec.preset is InfographicPreset.MISSION_DASHBOARD


# ── ComparisonRow + TimelineEvent ──────────────────────────────────────────


def test_comparison_row_valid():
    r = ComparisonRow(label="可靠性", columns=["A 方案：高", "B 方案：中"])
    assert r.label == "可靠性"
    assert len(r.columns) == 2


def test_comparison_row_rejects_empty_columns():
    with pytest.raises(ValidationError):
        ComparisonRow(label="x", columns=[])


def test_timeline_event_valid():
    ev = TimelineEvent(date="2025-Q1", title="啟動")
    assert ev.date == "2025-Q1"
    assert ev.description is None


def test_timeline_event_rejects_empty_date():
    with pytest.raises(ValidationError):
        TimelineEvent(date="", title="x")


# ── GenerateInfographicRequest ─────────────────────────────────────────────


def test_generate_request_required_fields():
    req = GenerateInfographicRequest(
        collection_id=42,
        preset=InfographicPreset.COMPARISON_MATRIX,
    )
    assert req.collection_id == 42
    assert req.preset is InfographicPreset.COMPARISON_MATRIX
    assert req.top_k == 12  # default


def test_generate_request_top_k_bounds():
    with pytest.raises(ValidationError):
        GenerateInfographicRequest(
            collection_id=1, preset=InfographicPreset.STATS_BRIEF, top_k=0,
        )
    with pytest.raises(ValidationError):
        GenerateInfographicRequest(
            collection_id=1, preset=InfographicPreset.STATS_BRIEF, top_k=31,
        )
    # Boundary values OK
    req = GenerateInfographicRequest(
        collection_id=1, preset=InfographicPreset.STATS_BRIEF, top_k=30,
    )
    assert req.top_k == 30


def test_generate_request_rejects_negative_collection_id():
    with pytest.raises(ValidationError):
        GenerateInfographicRequest(
            collection_id=0, preset=InfographicPreset.STATS_BRIEF,
        )
