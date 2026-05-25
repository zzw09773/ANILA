"""diagram_renderer — dot subprocess wrapper.

Studio Fix 2 (2026-05-18). Mocks ``asyncio.create_subprocess_exec`` so
the tests don't require the `dot` binary to be installed in the test
environment — we exercise every failure path explicitly.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from app.services.diagram_renderer import render_dot_to_png

_VALID_DOT = "digraph G { A -> B }"
_PNG_HEADER = b"\x89PNG\r\n\x1a\n"


@pytest.mark.asyncio
async def test_returns_png_on_success() -> None:
    """When dot binary returns 0 with PNG output, return the bytes."""
    fake_proc = AsyncMock()
    fake_proc.returncode = 0
    fake_proc.communicate = AsyncMock(
        return_value=(_PNG_HEADER + b"\x00" * 100, b"")
    )

    with patch(
        "app.services.diagram_renderer.asyncio.create_subprocess_exec",
        return_value=fake_proc,
    ):
        out = await render_dot_to_png(_VALID_DOT)

    assert out is not None
    assert out.startswith(_PNG_HEADER)


@pytest.mark.asyncio
async def test_returns_none_when_dot_missing() -> None:
    """FileNotFoundError → graceful None."""
    with patch(
        "app.services.diagram_renderer.asyncio.create_subprocess_exec",
        side_effect=FileNotFoundError("dot not found"),
    ):
        out = await render_dot_to_png(_VALID_DOT)
    assert out is None


@pytest.mark.asyncio
async def test_returns_none_on_nonzero_exit() -> None:
    fake_proc = AsyncMock()
    fake_proc.returncode = 1
    fake_proc.communicate = AsyncMock(return_value=(b"", b"syntax error"))

    with patch(
        "app.services.diagram_renderer.asyncio.create_subprocess_exec",
        return_value=fake_proc,
    ):
        out = await render_dot_to_png(_VALID_DOT)
    assert out is None


@pytest.mark.asyncio
async def test_returns_none_on_timeout() -> None:
    fake_proc = AsyncMock()
    fake_proc.kill = AsyncMock()
    fake_proc.wait = AsyncMock()
    fake_proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError)

    with patch(
        "app.services.diagram_renderer.asyncio.create_subprocess_exec",
        return_value=fake_proc,
    ):
        out = await render_dot_to_png(_VALID_DOT, timeout=0.01)
    assert out is None


@pytest.mark.asyncio
async def test_returns_none_when_output_not_png() -> None:
    fake_proc = AsyncMock()
    fake_proc.returncode = 0
    fake_proc.communicate = AsyncMock(return_value=(b"not a png", b""))

    with patch(
        "app.services.diagram_renderer.asyncio.create_subprocess_exec",
        return_value=fake_proc,
    ):
        out = await render_dot_to_png(_VALID_DOT)
    assert out is None


# ---------------------------------------------------------------------------
# Round 5 Patch S-fix: diagram_dot must be normalized BEFORE feeding to dot.
#
# Round 4 Patch S added LaTeX broken-char repairs to studio_text_normalizer,
# but normalize_text only walks slide-level text fields (title, bullets,
# stat.*, column.*, icon_rows.*). diagram_dot is a separate field: the LLM
# stuffs DOT into it, the hydration code feeds it straight to `dot` via
# stdin, no normalizer ever runs. tech_v4 slide 10 showed a graphviz oval
# containing '感知 (偵測鐵屑 \rightarrow Alert)' because the JSON parser
# ate the backslash on \\rightarrow and 'ightarrow' survived into the PNG.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_diagram_dot_strips_broken_latex_before_render() -> None:
    """Round 5: diagram_dot must pass through strip_latex.

    Regression guard for tech_v4 slide 10 where 'ightarrow' showed up
    in graphviz output because $\\rightarrow$ wasn't normalized — the
    JSON parser ate the backslash, leaving a literal CR + 'ightarrow'.
    """
    # Mirrors what the data looks like AFTER json.loads has eaten the
    # backslash on $\rightarrow$. The LLM emitted "$\rightarrow$" in JSON;
    # parser converted \r to CR, leaving "$" + CR + "ightarrow$" (or the
    # unclosed "$\rightarrow" variant). Round 4 Patch S registered both
    # variants in _LATEX_BROKEN_CHAR_REPLACEMENTS for slide-level fields;
    # Round 5 Patch S-fix extends the same passes to diagram_dot.
    broken_dot = (
        "digraph G {\n"
        '  perception [label="感知 (偵測 $\rightarrow Alert)"];\n'
        '  cognition  [label="認知 (RAG 檢索)"];\n'
        '  action     [label="行動 (生成建議)"];\n'
        "  perception -> cognition -> action;\n"
        "}\n"
    )

    fake_proc = AsyncMock()
    fake_proc.returncode = 0
    fake_proc.communicate = AsyncMock(
        return_value=(_PNG_HEADER + b"\x00" * 16, b"")
    )

    with patch(
        "app.services.diagram_renderer.asyncio.create_subprocess_exec",
        return_value=fake_proc,
    ):
        await render_dot_to_png(broken_dot)

    # Grab the DOT bytes that were piped to dot's stdin.
    assert fake_proc.communicate.await_count == 1
    sent_bytes = fake_proc.communicate.await_args.args[0]
    assert isinstance(sent_bytes, bytes)
    sent = sent_bytes.decode("utf-8")

    assert "ightarrow" not in sent, (
        f"Broken LaTeX residue leaked into dot input: {sent!r}"
    )
    assert "→" in sent, (
        f"Expected '→' after strip_latex; got: {sent!r}"
    )


@pytest.mark.asyncio
async def test_diagram_dot_passes_through_citation_normalizer() -> None:
    """Round 5: diagram_dot must also route through strip_inline_citations.

    Architectural connection test — we verify that the citation
    normalizer is invoked on diagram_dot, same as it is on slide-level
    fields. We can't easily test the visible result because
    ``strip_inline_citations`` is end-anchored by design (see its
    docstring; intra-text citations like "如 (參 [5]) 所述" are
    intentionally left alone to avoid false positives), and a well-
    formed DOT graph always ends in ``}`` so the citation marker
    never sits at the trailing edge of the full string. Mocking the
    normalizer is the cleanest way to assert the wiring exists; the
    actual scrubbing behavior is already covered by the
    studio_text_normalizer test suite.
    """
    dot_with_cite = (
        "digraph G {\n"
        '  a [label="技術背景 (參 [1])"];\n'
        '  b [label="解決方案 (參 [2])"];\n'
        "  a -> b;\n"
        "}\n"
    )

    fake_proc = AsyncMock()
    fake_proc.returncode = 0
    fake_proc.communicate = AsyncMock(
        return_value=(_PNG_HEADER + b"\x00" * 16, b"")
    )

    with (
        patch(
            "app.services.diagram_renderer.asyncio.create_subprocess_exec",
            return_value=fake_proc,
        ),
        patch(
            "app.services.diagram_renderer.strip_inline_citations",
            wraps=lambda s: s,
        ) as m_cite,
        patch(
            "app.services.diagram_renderer.strip_latex",
            wraps=lambda s: s,
        ) as m_latex,
    ):
        await render_dot_to_png(dot_with_cite)

    # Both normalizers were called on the DOT source — that's the wiring
    # this patch installs. Order matters (citations before latex, mirroring
    # studio_text_normalizer._convert).
    assert m_cite.called, "strip_inline_citations must run on diagram_dot"
    assert m_latex.called, "strip_latex must run on diagram_dot"
    assert m_cite.call_args.args[0] == dot_with_cite
