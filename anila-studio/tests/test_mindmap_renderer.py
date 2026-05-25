"""Tests for ``app.services.mindmap_renderer``.

Coverage:

  * ``spec_to_dot`` is a pure function — assert the produced DOT
    contains the rankdir, all node labels, parent→child edges, and
    preset-specific root fill colour. No subprocess invoked.
  * ``render_svg`` is monkey-patched so the real ``dot`` binary never
    runs — we exercise the success path, the FileNotFoundError path
    (missing dot), the non-zero exit path, the timeout path, and the
    non-SVG output path. Same approach as
    ``tests/test_diagram_renderer.py``.

These tests must stay environment-free: no graphviz dependency, no
fonts.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from app.schemas.mindmap import MindmapNode, MindmapPreset, MindmapSpec
from app.services.mindmap_renderer import (
    MindmapRenderError,
    count_nodes,
    render_svg,
    spec_to_dot,
)


def _fixture_spec(
    *, preset: MindmapPreset = MindmapPreset.CONCEPT_TREE, layout: str = "LR",
) -> MindmapSpec:
    """A 3-level fixture: root with 2 children, each with 2 grandchildren."""
    grand_a = MindmapNode(id="a1", label="孫節點 A1")
    grand_b = MindmapNode(id="a2", label="孫節點 A2")
    grand_c = MindmapNode(id="b1", label="孫節點 B1")
    grand_d = MindmapNode(id="b2", label="孫節點 B2")
    branch_a = MindmapNode(
        id="A", label="主題 A", children=[grand_a, grand_b],
    )
    branch_b = MindmapNode(
        id="B", label="主題 B", children=[grand_c, grand_d],
    )
    root = MindmapNode(
        id="root", label="中心概念", children=[branch_a, branch_b],
    )
    return MindmapSpec(
        title="測試心智圖",
        preset=preset,
        root=root,
        layout=layout,  # type: ignore[arg-type]
    )


_SVG_HEAD = b'<?xml version="1.0" encoding="UTF-8" standalone="no"?>\n<svg '


# ── spec_to_dot ─────────────────────────────────────────────────────────


def test_dot_contains_rankdir_from_layout():
    dot = spec_to_dot(_fixture_spec(layout="TB"))
    assert "rankdir=TB" in dot


@pytest.mark.parametrize(
    "layout", ["TB", "LR", "BT", "RL"],
)
def test_dot_emits_each_supported_layout(layout: str):
    dot = spec_to_dot(_fixture_spec(layout=layout))
    assert f"rankdir={layout}" in dot


def test_dot_contains_every_node_label():
    """All 7 labels (root + 2 + 4) must appear in the DOT source."""
    spec = _fixture_spec()
    dot = spec_to_dot(spec)
    for label in [
        "中心概念",
        "主題 A",
        "主題 B",
        "孫節點 A1",
        "孫節點 A2",
        "孫節點 B1",
        "孫節點 B2",
    ]:
        assert label in dot, f"label {label!r} missing from DOT:\n{dot}"


def test_dot_emits_parent_to_child_edges():
    """Edges should connect every parent to its declared children."""
    dot = spec_to_dot(_fixture_spec())
    # Edges are quoted in spec_to_dot — match the exact text.
    expected_edges = [
        '"root" -> "A"',
        '"root" -> "B"',
        '"A" -> "a1"',
        '"A" -> "a2"',
        '"B" -> "b1"',
        '"B" -> "b2"',
    ]
    for edge in expected_edges:
        assert edge in dot, f"edge {edge!r} missing:\n{dot}"


def test_dot_uses_cjk_font():
    """CJK labels render as empty rectangles without an explicit
    fontname pointing to Noto Sans CJK TC."""
    dot = spec_to_dot(_fixture_spec())
    assert '"Noto Sans CJK TC"' in dot


def test_dot_marks_root_with_preset_fill_colour():
    """Each preset should produce a distinct root fillcolor."""
    seen: set[str] = set()
    for preset in MindmapPreset:
        dot = spec_to_dot(_fixture_spec(preset=preset))
        # Root DECLARATION line: starts with `"root"` AND contains `[` (the
        # attribute opener). Edge lines like `"root" -> "A"` lack the `[`.
        root_decl = [
            ln for ln in dot.splitlines()
            if ln.strip().startswith('"root"') and "[" in ln
        ]
        assert len(root_decl) == 1, root_decl
        for token in root_decl[0].split(","):
            tok = token.strip()
            if tok.startswith('fillcolor='):
                seen.add(tok)
                break
    # 4 presets → 4 distinct fill colours.
    assert len(seen) == 4, seen


def test_dot_escapes_double_quote_in_labels():
    """Embedded ``"`` in a label must be escaped as ``\\"`` in DOT."""
    root = MindmapNode(id="r", label='含 "引號" 標籤')
    spec = MindmapSpec(
        title="t", preset=MindmapPreset.CONCEPT_TREE, root=root,
    )
    dot = spec_to_dot(spec)
    # The raw `"引號"` substring must NOT appear unescaped — the inner
    # quotes have to be backslash-escaped.
    assert '含 \\"引號\\" 標籤' in dot
    # ALL inner quotes are escaped; the only naked `"` should belong to
    # DOT's own string delimiters. Quick smoke: every `\"` is preceded
    # by `\\` (the escape) and every naked `"` opens or closes a token.
    assert '引號"' not in dot.replace('\\"', "ESC")


def test_dot_strips_latex_residue_from_labels():
    """LLM residue like $\\rightarrow$ should be normalised before
    landing in the DOT source — same contract as diagram_renderer."""
    root = MindmapNode(id="r", label="輸入 $\\rightarrow$ 輸出")
    spec = MindmapSpec(
        title="t", preset=MindmapPreset.CONCEPT_TREE, root=root,
    )
    dot = spec_to_dot(spec)
    assert "rightarrow" not in dot
    assert "→" in dot


def test_dot_strips_inline_citations_from_labels():
    """Trailing ``(參 [3])`` markers should be stripped, mirroring
    diagram_renderer's normalisation."""
    root = MindmapNode(id="r", label="重要結論 (參 [3])")
    spec = MindmapSpec(
        title="t", preset=MindmapPreset.CONCEPT_TREE, root=root,
    )
    dot = spec_to_dot(spec)
    assert "(參 [3])" not in dot
    # The non-citation portion of the label survives.
    assert "重要結論" in dot


def test_count_nodes_walks_entire_tree():
    assert count_nodes(_fixture_spec()) == 7  # root + 2 + 4
    leaf = MindmapSpec(
        title="t",
        preset=MindmapPreset.CONCEPT_TREE,
        root=MindmapNode(id="r", label="only"),
    )
    assert count_nodes(leaf) == 1


def test_dot_handles_duplicate_ids_without_redeclaration():
    """If the LLM reuses an id, we keep the first declaration and let
    the second emit an edge — no crash, no Python-side dedupe drama."""
    leaf_a = MindmapNode(id="dup", label="first decl")
    # Second `dup` deeper in the tree under a different parent.
    leaf_b = MindmapNode(id="dup", label="second decl")
    branch = MindmapNode(
        id="branch", label="branch", children=[leaf_b],
    )
    root = MindmapNode(
        id="root", label="root", children=[leaf_a, branch],
    )
    spec = MindmapSpec(
        title="t", preset=MindmapPreset.CONCEPT_TREE, root=root,
    )
    dot = spec_to_dot(spec)
    # Only ONE declaration of "dup" — the second was suppressed.
    decl_count = sum(
        1 for line in dot.splitlines()
        if line.strip().startswith('"dup"') and "->" not in line
    )
    assert decl_count == 1
    # But BOTH edges to "dup" survive.
    assert '"root" -> "dup"' in dot
    assert '"branch" -> "dup"' in dot


# ── render_svg (subprocess mocked) ──────────────────────────────────────


_FAKE_DOT_SRC = 'digraph G { "a" -> "b" }'


@pytest.mark.asyncio
async def test_render_svg_returns_bytes_on_success():
    fake_proc = AsyncMock()
    fake_proc.returncode = 0
    fake_proc.communicate = AsyncMock(
        return_value=(_SVG_HEAD + b'<g></g></svg>', b''),
    )
    with patch(
        "app.services.mindmap_renderer.asyncio.create_subprocess_exec",
        return_value=fake_proc,
    ):
        out = await render_svg(_FAKE_DOT_SRC)
    assert out.startswith(_SVG_HEAD)
    assert b'</svg>' in out


@pytest.mark.asyncio
async def test_render_svg_raises_when_dot_missing():
    with patch(
        "app.services.mindmap_renderer.asyncio.create_subprocess_exec",
        side_effect=FileNotFoundError("dot not found"),
    ):
        with pytest.raises(MindmapRenderError) as exc:
            await render_svg(_FAKE_DOT_SRC)
    assert "dot" in str(exc.value).lower()


@pytest.mark.asyncio
async def test_render_svg_raises_on_nonzero_exit():
    fake_proc = AsyncMock()
    fake_proc.returncode = 1
    fake_proc.communicate = AsyncMock(
        return_value=(b'', b'syntax error at line 1'),
    )
    with patch(
        "app.services.mindmap_renderer.asyncio.create_subprocess_exec",
        return_value=fake_proc,
    ):
        with pytest.raises(MindmapRenderError) as exc:
            await render_svg(_FAKE_DOT_SRC)
    assert "syntax error" in str(exc.value)


@pytest.mark.asyncio
async def test_render_svg_raises_on_timeout():
    fake_proc = AsyncMock()
    # The real-code path calls proc.kill() then proc.wait(); kill is
    # sync (not awaitable) on real Popen so we use MagicMock-style
    # behaviour by setting it to a plain function. AsyncMock by default
    # makes kill() a coroutine, but the implementation already wraps
    # the post-kill wait in try/except so we're safe either way.
    fake_proc.kill = lambda: None
    fake_proc.wait = AsyncMock()
    fake_proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError)
    with patch(
        "app.services.mindmap_renderer.asyncio.create_subprocess_exec",
        return_value=fake_proc,
    ):
        with pytest.raises(MindmapRenderError) as exc:
            await render_svg(_FAKE_DOT_SRC, timeout=0.01)
    assert "timed out" in str(exc.value).lower()


@pytest.mark.asyncio
async def test_render_svg_raises_when_output_not_svg():
    fake_proc = AsyncMock()
    fake_proc.returncode = 0
    fake_proc.communicate = AsyncMock(
        return_value=(b'<<NOT-SVG>>', b''),
    )
    with patch(
        "app.services.mindmap_renderer.asyncio.create_subprocess_exec",
        return_value=fake_proc,
    ):
        with pytest.raises(MindmapRenderError) as exc:
            await render_svg(_FAKE_DOT_SRC)
    assert "not SVG" in str(exc.value)


@pytest.mark.asyncio
async def test_render_svg_passes_dot_source_to_subprocess_stdin():
    """The DOT source is fed via stdin, not via the argv. Regression
    guard for accidentally interpolating user-controllable text into
    the command line (which would be a command-injection vector)."""
    fake_proc = AsyncMock()
    fake_proc.returncode = 0
    fake_proc.communicate = AsyncMock(
        return_value=(_SVG_HEAD + b'</svg>', b''),
    )
    with patch(
        "app.services.mindmap_renderer.asyncio.create_subprocess_exec",
        return_value=fake_proc,
    ) as m_exec:
        await render_svg('digraph G { "x" -> "y" }')
    # The argv passed to create_subprocess_exec is the binary + the
    # fixed -Tsvg flag, nothing else.
    args = m_exec.call_args.args
    assert args[0] == "dot"
    assert args[1] == "-Tsvg"
    assert len(args) == 2
    # And the source bytes landed on stdin.
    sent_bytes = fake_proc.communicate.await_args.args[0]
    assert isinstance(sent_bytes, bytes)
    assert b'digraph G' in sent_bytes
