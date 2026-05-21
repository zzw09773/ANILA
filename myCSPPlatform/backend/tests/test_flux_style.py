"""Stage 3 — content-inferred deck style (infer_deck_style) tests."""
from __future__ import annotations

import pytest

from app.services.flux_style import (
    _DEFAULT_STYLE,
    StyleDescriptor,
    infer_deck_style,
)


class _StubLLM:
    """Returns a fixed completion regardless of prompt."""

    def __init__(self, reply: str) -> None:
        self._reply = reply

    async def complete(self, *, system: str, user: str) -> str:  # noqa: ARG002
        return self._reply


class _RaisingLLM:
    async def complete(self, *, system: str, user: str) -> str:  # noqa: ARG002
        raise RuntimeError("boom")


@pytest.mark.asyncio
async def test_infer_returns_descriptor_with_auto_id():
    s = await infer_deck_style(
        title="Q3 財報",
        content_sample="營收年增 20%",
        llm=_StubLLM("deep navy and amber palette, soft editorial illustration"),
    )
    assert isinstance(s, StyleDescriptor)
    assert s.style_id.startswith("auto-")
    assert "deep navy" in s.suffix


@pytest.mark.asyncio
async def test_infer_deterministic_style_id():
    llm = _StubLLM("muted teal palette, flat editorial illustration")
    a = await infer_deck_style(title="t", content_sample="c", llm=llm)
    b = await infer_deck_style(title="t", content_sample="c", llm=llm)
    assert a.style_id == b.style_id


@pytest.mark.asyncio
async def test_infer_appends_no_text_guard():
    s = await infer_deck_style(
        title="t", content_sample="c",
        llm=_StubLLM("warm cinematic lighting, oil painting texture"),
    )
    assert "no text" in s.suffix.lower()


@pytest.mark.asyncio
async def test_infer_empty_falls_back_to_default():
    s = await infer_deck_style(title="t", content_sample="c", llm=_StubLLM("   "))
    assert s is _DEFAULT_STYLE


@pytest.mark.asyncio
async def test_infer_too_short_falls_back():
    s = await infer_deck_style(title="t", content_sample="c", llm=_StubLLM("blue"))
    assert s is _DEFAULT_STYLE


@pytest.mark.asyncio
async def test_infer_llm_exception_falls_back():
    s = await infer_deck_style(title="t", content_sample="c", llm=_RaisingLLM())
    assert s is _DEFAULT_STYLE


@pytest.mark.asyncio
async def test_infer_normalizes_whitespace():
    s = await infer_deck_style(
        title="t", content_sample="c",
        llm=_StubLLM("line one palette\n\n  line two   lighting texture"),
    )
    assert "\n" not in s.suffix
    assert "  " not in s.suffix


@pytest.mark.asyncio
async def test_infer_distinct_outputs_distinct_ids():
    a = await infer_deck_style(
        title="t", content_sample="c",
        llm=_StubLLM("teal flat editorial illustration palette"),
    )
    b = await infer_deck_style(
        title="t", content_sample="c",
        llm=_StubLLM("crimson baroque oil painting palette"),
    )
    assert a.style_id != b.style_id
