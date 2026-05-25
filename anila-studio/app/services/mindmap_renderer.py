"""Render a ``MindmapSpec`` to Graphviz DOT, then to SVG via the ``dot`` binary.

Follows the same pattern as :mod:`app.services.diagram_renderer`:

  * ``spec_to_dot`` — pure function; ``MindmapSpec`` → DOT source string.
  * ``render_svg`` — subprocess wrapper around ``dot -Tsvg``; raises a
    typed :class:`MindmapRenderError` on failure so the job runner can
    surface a clean message.

Security note: same as ``diagram_renderer`` — we use
``asyncio.create_subprocess_exec`` (NOT shell), passing the binary name
and a fixed argument list. The DOT source is fed via stdin, never
interpolated into a shell command, so there is no command injection
surface.

CJK label support: Graphviz needs ``fonts-noto-cjk`` installed at the
container level (it is, per Dockerfile) and ``fontname="Noto Sans CJK
TC"`` set on the node/graph; the dot binary picks the font up from
fontconfig. Without the font the boxes render as empty rectangles.
"""
from __future__ import annotations

import asyncio
import logging

from app.schemas.mindmap import MindmapNode, MindmapPreset, MindmapSpec
from app.services.studio_text_normalizer import (
    strip_inline_citations,
    strip_latex,
)

logger = logging.getLogger(__name__)


# ── Subprocess constants ─────────────────────────────────────────────────


_DOT_BINARY = "dot"
_DOT_ARGS = ("-Tsvg",)
# SVG header — the dot binary prefixes its output with the XML
# declaration when emitting SVG. We use this for a sanity check, same
# pattern as diagram_renderer using the PNG magic bytes.
_SVG_MARKER = b"<svg"

# Mindmaps with 50+ nodes can take >5s for dot to lay out (especially
# with ortho splines). 30s is the hard ceiling — anything longer and
# the user is better served by an error than a hung tab.
_DEFAULT_TIMEOUT_SECONDS = 30.0


# ── Preset → root fill colour ────────────────────────────────────────────
#
# Root node gets a saturated fill so it visually anchors the map; child
# levels get progressively lighter fills (see ``_LEVEL_FILLS`` below).
# Colours are picked to be readable on a default white background and
# to print legibly when the SVG is exported to a slide deck.


_PRESET_ROOT_FILL: dict[MindmapPreset, str] = {
    MindmapPreset.CONCEPT_TREE: "#1f4e79",  # navy
    MindmapPreset.TASK_BREAKDOWN: "#385723",  # forest green
    MindmapPreset.SOP_FLOW: "#7b2d26",  # deep red
    MindmapPreset.ORG_RELATIONSHIPS: "#5a3a8a",  # purple
}


# Light-fill ladder for non-root depth levels. Index 0 is depth 1
# (children of root), index N-1 is the deepest depth we colour
# distinctly; everything beyond falls into the last fill.
_LEVEL_FILLS: tuple[str, ...] = (
    "#dbeafe",  # depth 1 — light blue
    "#e2e8f0",  # depth 2 — slate
    "#f1f5f9",  # depth 3+ — very light slate
)


# ── Exceptions ───────────────────────────────────────────────────────────


class MindmapRenderError(RuntimeError):
    """Raised when ``dot -Tsvg`` fails (missing binary, non-zero exit,
    timeout, non-SVG output). Carries a short user-safe message so the
    job runner can surface it without leaking subprocess internals.
    """


# ── Internal helpers ─────────────────────────────────────────────────────


def _escape_label(label: str) -> str:
    """Make ``label`` safe to drop inside ``"..."`` in DOT source.

    DOT's quoted-string rules: ``"`` and ``\\`` must be escaped. We
    deliberately do NOT escape ``\\n`` here because callers use it to
    request a forced line break inside the rendered box — dot interprets
    ``\\n`` (literal two-char sequence: backslash + n) in a label as
    "centre-aligned newline". We pre-normalise the label through
    ``strip_latex`` + ``strip_inline_citations`` (the same passes
    diagram_renderer uses on slide diagram_dot) so common LLM residue
    like ``$\\rightarrow$`` and ``(參 [5])`` are scrubbed before we even
    look at escaping.
    """
    cleaned = strip_inline_citations(label) or ""
    cleaned = strip_latex(cleaned) or ""
    # Order matters: escape the backslash FIRST so the later \" doesn't
    # get double-escaped, then escape the quote.
    return cleaned.replace("\\", "\\\\").replace('"', '\\"')


def _fill_for_depth(depth: int) -> str:
    """Map a tree depth (root=0) to a node fill colour.

    Depths beyond the explicit ladder reuse the lightest fill so deep
    branches stay visually subordinate to the root.
    """
    if depth <= 0:
        # Root depth shouldn't ever route through here, but stay safe.
        return _LEVEL_FILLS[0]
    idx = min(depth - 1, len(_LEVEL_FILLS) - 1)
    return _LEVEL_FILLS[idx]


def _walk_nodes(
    node: MindmapNode,
    *,
    depth: int,
    lines: list[str],
    edges: list[str],
    seen_ids: set[str],
    root_fill: str,
) -> None:
    """Depth-first walk: append node + edge declarations to the lists.

    ``seen_ids`` deduplicates by id. If the LLM emits two nodes with the
    same id (which it shouldn't, but we don't crash if it does) we keep
    the first declaration and skip the duplicate, but still emit the
    edge so the user sees the intended connection.
    """
    if node.id in seen_ids:
        # Edge to a previously-declared id is fine — DOT supports cross
        # references. Don't redeclare the node.
        pass
    else:
        seen_ids.add(node.id)
        escaped_label = _escape_label(node.label)
        if depth == 0:
            # Root: bold border, saturated fill, white text for contrast.
            attrs = (
                f'label="{escaped_label}", '
                f'style="filled,bold", '
                f'fillcolor="{root_fill}", '
                f'fontcolor="white", '
                f'penwidth=2'
            )
        else:
            attrs = (
                f'label="{escaped_label}", '
                f'style="filled,rounded", '
                f'fillcolor="{_fill_for_depth(depth)}"'
            )
        # Escape id the same way label gets escaped (defensive — schema
        # caps id to printable chars but the LLM could still send quotes).
        escaped_id = _escape_label(node.id)
        lines.append(f'  "{escaped_id}" [{attrs}];')

    for child in node.children:
        escaped_parent = _escape_label(node.id)
        escaped_child = _escape_label(child.id)
        edges.append(f'  "{escaped_parent}" -> "{escaped_child}";')
        _walk_nodes(
            child,
            depth=depth + 1,
            lines=lines,
            edges=edges,
            seen_ids=seen_ids,
            root_fill=root_fill,
        )


def _count_nodes(node: MindmapNode) -> int:
    """Total node count in the tree rooted at ``node`` (root inclusive).

    Used by the job runner to populate ``MindmapJobStatus.node_count``;
    not part of the DOT source.
    """
    return 1 + sum(_count_nodes(c) for c in node.children)


# ── Public API ───────────────────────────────────────────────────────────


def spec_to_dot(spec: MindmapSpec) -> str:
    """Render a ``MindmapSpec`` as Graphviz DOT source.

    Output structure:

        digraph mindmap {
          rankdir=<LAYOUT>;
          splines=ortho;
          fontname="Noto Sans CJK TC";
          node [shape=box, style="filled,rounded", fontname="Noto Sans CJK TC", ...];
          edge [color="#475569", ...];
          // node declarations
          // edge declarations
        }

    Pure function — no I/O. The caller pipes the returned string to
    ``render_svg`` (or stores it on disk for debug).
    """
    root_fill = _PRESET_ROOT_FILL.get(spec.preset, "#1f4e79")

    title_label = _escape_label(spec.title)
    node_lines: list[str] = []
    edge_lines: list[str] = []
    _walk_nodes(
        spec.root,
        depth=0,
        lines=node_lines,
        edges=edge_lines,
        seen_ids=set(),
        root_fill=root_fill,
    )

    parts: list[str] = [
        "digraph mindmap {",
        f"  rankdir={spec.layout};",
        "  splines=ortho;",
        '  bgcolor="white";',
        f'  labelloc="t"; label="{title_label}";',
        '  fontname="Noto Sans CJK TC";',
        '  fontsize=18;',
        "  node ["
        'shape=box, style="filled,rounded", '
        'fontname="Noto Sans CJK TC", fontsize=12, '
        'margin="0.18,0.10", color="#475569"'
        "];",
        '  edge [color="#475569", arrowsize=0.7];',
    ]
    parts.extend(node_lines)
    parts.extend(edge_lines)
    parts.append("}")
    parts.append("")  # trailing newline so the file ends cleanly
    return "\n".join(parts)


async def render_svg(
    dot_source: str, *, timeout: float = _DEFAULT_TIMEOUT_SECONDS,
) -> bytes:
    """Pipe ``dot_source`` through ``dot -Tsvg`` and return the SVG bytes.

    Raises:
      * :class:`MindmapRenderError` — the dot binary is missing, exited
        non-zero, timed out, or produced non-SVG output.

    Unlike :mod:`app.services.diagram_renderer` (which returns ``None``
    on failure because the slide pipeline can fall back to a text-only
    layout), the mindmap pipeline has no fallback rendering path — if
    dot fails, the job must fail with a clear error.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            _DOT_BINARY,
            *_DOT_ARGS,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise MindmapRenderError(
            "Graphviz `dot` binary not available — install graphviz "
            "in the container."
        ) from exc

    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(dot_source.encode("utf-8")),
            timeout=timeout,
        )
    except asyncio.TimeoutError as exc:
        proc.kill()
        try:
            await proc.wait()
        except Exception:  # noqa: BLE001 — best-effort cleanup
            pass
        raise MindmapRenderError(
            f"Graphviz dot timed out after {timeout:.1f}s — mindmap is "
            "too large or the graph is degenerate."
        ) from exc

    if proc.returncode != 0:
        err_tail = stderr.decode(errors="replace")[:200]
        logger.warning(
            "Mindmap dot failed (rc=%s): %s", proc.returncode, err_tail,
        )
        raise MindmapRenderError(
            f"Graphviz dot exited {proc.returncode}: {err_tail}"
        )

    if _SVG_MARKER not in stdout[:512]:
        # dot occasionally emits a deprecation banner on stdout before
        # the real SVG; scanning the first 512 bytes catches the common
        # cases without false negatives.
        raise MindmapRenderError(
            f"Graphviz dot output is not SVG (first 8 bytes: {stdout[:8]!r})"
        )

    return stdout


def count_nodes(spec: MindmapSpec) -> int:
    """Public wrapper around the internal node-counting walk."""
    return _count_nodes(spec.root)
