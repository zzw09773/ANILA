"""接地 output guardrail：CitedAnswer 無引用且有實質內容時 tripwire。"""

from __future__ import annotations

import pytest

from anila_agent.guardrails.grounding import grounding_guardrail
from anila_agent.models.schemas import Citation, CitedAnswer

pytestmark = pytest.mark.unit


async def _run(output):
    res = await grounding_guardrail.run(None, None, output)
    return res.output.tripwire_triggered


async def test_trips_on_long_answer_without_citations():
    out = CitedAnswer(answer="這是一段足夠長、有實質內容卻完全沒有附上任何來源引用的回答內容。", citations=[])
    assert await _run(out) is True


async def test_allows_answer_with_citations():
    out = CitedAnswer(
        answer="這是一段足夠長、有實質內容並且有附上來源的回答內容。",
        citations=[Citation(source="doc-1")],
    )
    assert await _run(out) is False


async def test_allows_short_answer():
    out = CitedAnswer(answer="查無相關內容。", citations=[])
    assert await _run(out) is False


async def test_ignores_non_cited_output():
    assert await _run("just a string") is False
