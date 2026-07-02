"""Tests for app.schemas.report — ReportSpec validation behaviours.

We exercise the validator decisions documented in app/schemas/report.py
to lock them down before the runner and renderer depend on them:

  - title / heading must not be empty after strip,
  - tldr length window (20-600 chars; soft cap),
  - sections min/max counts,
  - reference.n uniqueness,
  - recursive subsections deserialise correctly,
  - GenerateReportRequest top_k bounds,
  - ReportPreset enum coverage matches the renderer's preset table.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas.report import (
    GenerateReportRequest,
    ReportPreset,
    ReportReference,
    ReportSection,
    ReportSpec,
)


def _minimal_spec(**overrides) -> ReportSpec:
    base: dict = dict(
        title="測試標題",
        preset=ReportPreset.KEY_SUMMARY,
        tldr="這是一份測試報告的摘要文字，至少超過二十個字以滿足下限。",
        sections=[ReportSection(heading="第一節", content_markdown="內文")],
    )
    base.update(overrides)
    return ReportSpec(**base)


def test_minimal_valid_spec_constructs():
    spec = _minimal_spec()
    assert spec.title == "測試標題"
    assert spec.preset == ReportPreset.KEY_SUMMARY
    assert len(spec.sections) == 1
    assert spec.references == []
    assert spec.generated_at is not None


def test_title_empty_after_strip_rejected():
    with pytest.raises(ValidationError):
        _minimal_spec(title="   ")


def test_section_heading_empty_after_strip_rejected():
    with pytest.raises(ValidationError):
        ReportSection(heading="   ", content_markdown="")


def test_section_heading_trimmed():
    sec = ReportSection(heading="  Trimmed  ", content_markdown="")
    assert sec.heading == "Trimmed"


def test_tldr_too_short_rejected():
    with pytest.raises(ValidationError):
        _minimal_spec(tldr="太短")


def test_tldr_too_long_rejected():
    long_tldr = "a" * 700
    with pytest.raises(ValidationError):
        _minimal_spec(tldr=long_tldr)


def test_sections_empty_rejected():
    with pytest.raises(ValidationError):
        _minimal_spec(sections=[])


def test_sections_max_count_enforced():
    too_many = [
        ReportSection(heading=f"S{i}", content_markdown="") for i in range(25)
    ]
    with pytest.raises(ValidationError):
        _minimal_spec(sections=too_many)


def test_reference_numbering_must_be_unique():
    with pytest.raises(ValidationError):
        _minimal_spec(
            references=[
                ReportReference(n=1, filename="a.pdf"),
                ReportReference(n=1, filename="b.pdf"),
            ]
        )


def test_reference_gaps_allowed():
    """Gaps in citation numbering are ok — LLM might cite [1], [2], [5]."""
    spec = _minimal_spec(
        references=[
            ReportReference(n=1, filename="a.pdf"),
            ReportReference(n=2, filename="b.pdf"),
            ReportReference(n=5, filename="c.pdf"),
        ]
    )
    assert [r.n for r in spec.references] == [1, 2, 5]


def test_subsections_recursive_construct():
    inner = ReportSection(heading="3.1", content_markdown="x")
    middle = ReportSection(heading="3.", content_markdown="", subsections=[inner])
    outer = ReportSection(heading="3", content_markdown="", subsections=[middle])
    assert outer.subsections[0].subsections[0].heading == "3.1"


def test_request_top_k_lower_bound():
    with pytest.raises(ValidationError):
        GenerateReportRequest(
            collection_id=1, preset=ReportPreset.KEY_SUMMARY, top_k=0
        )


def test_request_top_k_upper_bound():
    with pytest.raises(ValidationError):
        GenerateReportRequest(
            collection_id=1, preset=ReportPreset.KEY_SUMMARY, top_k=99
        )


def test_request_default_top_k():
    req = GenerateReportRequest(collection_id=1, preset=ReportPreset.KEY_SUMMARY)
    assert req.top_k == 12
    assert req.extra_instructions is None
    assert req.document_ids is None


def test_request_extra_instructions_max_length():
    too_long = "a" * 2500
    with pytest.raises(ValidationError):
        GenerateReportRequest(
            collection_id=1,
            preset=ReportPreset.KEY_SUMMARY,
            extra_instructions=too_long,
        )


def test_preset_enum_string_round_trip():
    """API receives JSON string → enum coercion works."""
    req = GenerateReportRequest.model_validate(
        {
            "collection_id": 7,
            "preset": "deep_tech_review",
        }
    )
    assert req.preset is ReportPreset.DEEP_TECH_REVIEW


def test_preset_enum_invalid_string_rejected():
    with pytest.raises(ValidationError):
        GenerateReportRequest.model_validate(
            {"collection_id": 7, "preset": "nope_not_a_preset"}
        )


def test_all_four_presets_defined():
    """Renderer assumes 4 presets — guard against accidental enum drift."""
    expected = {
        "deep_tech_review",
        "key_summary",
        "teaching_handout",
        "external_comms",
    }
    assert {p.value for p in ReportPreset} == expected
