"""_rebalance_layouts — invoke focused LLM call to fix violations.

Tests the rebalance pass that fires after `_audit_layout_distribution`
detects hard V1/V2 violations. The actual LLM round-trip is mocked via
`_call_llm_for_rebalance` — that helper was extracted specifically so
tests don't need to stand up the model registry / proxy / DB pieces.
"""
from __future__ import annotations

import logging

from unittest.mock import AsyncMock, patch

import pytest

from app.services.studio_layout import (
    LAYOUT_REBALANCE_MAX_CHANGES,
    LayoutViolation,
    _rebalance_layouts,
    _should_rebalance,
)


def _baseline_spec_dict() -> dict:
    """Build a minimal spec_dict with one slide that fails V4 — enumeration
    title + 3+ bullets + standard layout. Makes the test bodies short.
    """
    return {
        "title": "ANILA 簡報",
        "slides": [
            {
                "title": "三大核心能力",
                "bullets": ["感知", "認知", "行動"],
                "layout_kind": "standard",
            },
            {
                "title": "其他",
                "bullets": ["a", "b"],
                "layout_kind": "standard",
            },
        ],
        "palette": "navy_amber",
    }


def _baseline_violations() -> list[LayoutViolation]:
    """Hard V1 + soft V4 — enough to trigger a rebalance and give the LLM
    a candidate.
    """
    return [
        LayoutViolation(
            kind="V1",
            severity="hard",
            slide_indices=[0, 1],
            detail="standard 比例 100% 超過上限 60%（2/2）",
        ),
        LayoutViolation(
            kind="V4_CONTENT",
            severity="soft",
            slide_indices=[0],
            detail="slide 0 標題含列舉關鍵字 '三大'、3 個 bullet、layout=standard",
        ),
    ]


@pytest.mark.asyncio
@patch("app.services.studio_layout._call_llm_for_rebalance", new_callable=AsyncMock)
async def test_rebalance_applies_changes_to_spec(mock_call):
    """LLM returns {changes: [...]}; rebalance mutates spec accordingly."""
    mock_call.return_value = {
        "changes": [
            {
                "slide_index": 0,
                "new_layout_kind": "icon_rows",
                "new_payload": {"icon_rows": [
                    {"concept": "user", "heading": "感知", "description": "外部訊號接收與整合。"},
                    {"concept": "model", "heading": "認知", "description": "在抽象層上推理與決策。"},
                    {"concept": "action", "heading": "行動", "description": "把決策變成具體行為。"},
                ]},
            }
        ]
    }

    new_spec_dict = await _rebalance_layouts(
        _baseline_spec_dict(),
        _baseline_violations(),
        chunks_text="some chunks",
        bearer="test-bearer",  # mocked LLM path doesn't hit csp proxy
    )

    # Slide 0 should now be icon_rows with the LLM-supplied payload.
    assert new_spec_dict["slides"][0]["layout_kind"] == "icon_rows"
    assert "icon_rows" in new_spec_dict["slides"][0]
    assert len(new_spec_dict["slides"][0]["icon_rows"]) == 3
    # Title and bullets must be untouched — that's the whole contract.
    assert new_spec_dict["slides"][0]["title"] == "三大核心能力"
    assert new_spec_dict["slides"][0]["bullets"] == ["感知", "認知", "行動"]


@pytest.mark.asyncio
@patch("app.services.studio_layout._call_llm_for_rebalance", new_callable=AsyncMock)
async def test_rebalance_respects_max_changes(mock_call):
    """LLM returns 10 changes; only first 3 applied."""
    # Build a spec_dict with 10 standard slides so each LLM-proposed change
    # has a valid target index. Use the V4 enumeration title for slide 0 so
    # we still have the candidate set populated; the cap is independent of
    # whether the violation list is well-formed.
    spec_dict = {
        "title": "T",
        "slides": [
            {
                "title": f"三大 slide {i}",
                "bullets": ["a", "b", "c"],
                "layout_kind": "standard",
            }
            for i in range(10)
        ],
        "palette": "navy_amber",
    }
    mock_call.return_value = {
        "changes": [
            {
                "slide_index": i,
                "new_layout_kind": "icon_rows",
                "new_payload": {"icon_rows": [
                    {"concept": "user", "heading": f"H{i}", "description": "X" * 30},
                    {"concept": "model", "heading": f"H{i}", "description": "X" * 30},
                    {"concept": "action", "heading": f"H{i}", "description": "X" * 30},
                ]},
            }
            for i in range(10)
        ]
    }
    violations = [
        LayoutViolation(
            kind="V1",
            severity="hard",
            slide_indices=list(range(10)),
            detail="standard 100%",
        )
    ]

    result = await _rebalance_layouts(
        spec_dict, violations, chunks_text="", bearer="test-bearer",
    )

    # First LAYOUT_REBALANCE_MAX_CHANGES slides switched to icon_rows; the
    # rest remained standard.
    assert LAYOUT_REBALANCE_MAX_CHANGES == 3
    changed = [s for s in result["slides"] if s["layout_kind"] == "icon_rows"]
    untouched = [s for s in result["slides"] if s["layout_kind"] == "standard"]
    assert len(changed) == LAYOUT_REBALANCE_MAX_CHANGES
    assert len(untouched) == 10 - LAYOUT_REBALANCE_MAX_CHANGES


@pytest.mark.asyncio
@patch("app.services.studio_layout._call_llm_for_rebalance", new_callable=AsyncMock)
async def test_rebalance_logs_warning_when_v1_still_violated(mock_call, caplog):
    """If LLM's changes don't fix V1, log warning and continue (don't 502)."""
    # Return zero changes — V1 (100% standard) will remain after the pass.
    # Use a long-form deck; 口講用短頁 (≤6 pages) no longer audits V1.
    mock_call.return_value = {"changes": []}
    spec_dict = {
        "title": "T",
        "slides": [
            {
                "title": f"三大 slide {i}",
                "bullets": ["a", "b", "c"],
                "layout_kind": "standard",
            }
            for i in range(8)
        ],
        "palette": "navy_amber",
    }
    violations = [
        LayoutViolation(
            kind="V1",
            severity="hard",
            slide_indices=list(range(8)),
            detail="standard 100%",
        )
    ]

    with caplog.at_level(logging.WARNING, logger="app.services.studio_layout"):
        result = await _rebalance_layouts(
            spec_dict,
            violations,
            chunks_text="",
            bearer="test-bearer",
        )

    # No exception, returns a usable spec_dict.
    assert isinstance(result, dict)
    assert "slides" in result
    # The "V1 still violates" warning must be emitted.
    assert any(
        "V1" in rec.message and "still violates" in rec.message
        for rec in caplog.records
    ), f"expected V1-still-violates warning, got: {[r.message for r in caplog.records]}"


# ── _should_rebalance — Round 2 Patch D trigger broadening ──


def test_should_rebalance_fires_on_two_v4():
    """Round 6: V4_CONTENT violations trigger rebalance even without V1/V2."""
    violations = [
        LayoutViolation(kind="V4_CONTENT", severity="soft", slide_indices=[3], detail=""),
        LayoutViolation(kind="V4_CONTENT", severity="soft", slide_indices=[7], detail=""),
    ]
    assert _should_rebalance(violations) is True


def test_should_not_rebalance_on_single_title_hint():
    """Round 6: a lone V4_TITLE hint is never actionable → no rebalance."""
    violations = [
        LayoutViolation(kind="V4_TITLE", severity="hint", slide_indices=[3], detail=""),
    ]
    assert _should_rebalance(violations) is False


def test_should_rebalance_on_v1_alone():
    violations = [
        LayoutViolation(kind="V1", severity="hard", slide_indices=[0, 1, 2, 3, 4, 5], detail=""),
    ]
    assert _should_rebalance(violations) is True


def test_should_not_rebalance_on_soft_v3_and_title_hints():
    """Round 6: V3 runs + V4_TITLE hints carry no actionable signal → no call.

    The old `soft_count >= 3` trigger is gone; only V4_CONTENT or hard fire.
    """
    violations = [
        LayoutViolation(kind="V3", severity="soft", slide_indices=[2], detail=""),
        LayoutViolation(kind="V4_TITLE", severity="hint", slide_indices=[5], detail=""),
        LayoutViolation(kind="V3", severity="soft", slide_indices=[8], detail=""),
    ]
    assert _should_rebalance(violations) is False
