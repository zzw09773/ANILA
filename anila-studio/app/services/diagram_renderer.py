"""Render Graphviz DOT to PNG via the `dot` binary in a subprocess.

Used by the Studio pipeline when a slide has image_kind='diagram'.
Failures (DOT syntax errors, missing binary, timeout) return None;
the hydration layer falls back to standard layout.

Studio Fix 2 (2026-05-18): FLUX.2-dev is a diffusion model and can't
render legible text in images (the "Geneeration / KIGDKED" garbage we
saw on slide 9). When the LLM declares image_kind='diagram' it ships
Graphviz DOT instead of a FLUX prompt, and we render it deterministically
here. CJK label support requires `fonts-noto-cjk` installed in the
container — see myCSPPlatform/backend/Dockerfile.

Security note: we use asyncio.create_subprocess_exec (NOT shell), passing
the binary name and a fixed argument list. The DOT source is fed via
stdin, never interpolated into a shell command, so there is no command
injection surface.
"""
from __future__ import annotations

import asyncio
import logging

from app.services.studio_text_normalizer import (
    strip_inline_citations,
    strip_latex,
)

logger = logging.getLogger(__name__)


_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_DOT_BINARY = "dot"
_DOT_ARGS = ("-Tpng",)


async def render_dot_to_png(dot: str, *, timeout: float = 5.0) -> bytes | None:
    """Run ``dot -Tpng`` reading DOT from stdin; return PNG bytes on success.

    Returns None on any failure (missing binary, non-zero exit, timeout,
    non-PNG output). The caller — typically ``_hydrate_images`` — drops
    the ``diagram_dot`` field on None and lets the renderer fall back to
    the standard layout, same as any other image-path failure.

    The 5-second default timeout matches the rest of the Studio
    pipeline's "don't block the request indefinitely" budget; complex
    DOT graphs may need to override.

    Round 5 Patch S-fix: pre-process DOT text through the same
    normalizer passes (``strip_inline_citations`` + ``strip_latex``)
    that ``studio_text_normalizer.normalize_text`` applies to slide-
    level fields (title, bullets, stat.*, column.*, icon_rows.*).
    ``diagram_dot`` is a separate field and the LLM occasionally emits
    LaTeX (``$\\rightarrow$``) or RAG citation markers (``(參 [5])``)
    inside node labels; the JSON parser eats backslashes, leaving
    residue like ``ightarrow`` that ends up rendered verbatim inside
    graphviz boxes. Field-specific opt-in is the right architectural
    choice — normalizers shouldn't sniff context.
    """
    if dot:
        # Order matches studio_text_normalizer._convert: citations first
        # (end-anchored, doesn't interact with LaTeX); LaTeX second so
        # downstream consumers see plain Unicode.
        dot = strip_inline_citations(dot) or ""
        dot = strip_latex(dot) or ""

    try:
        proc = await asyncio.create_subprocess_exec(
            _DOT_BINARY,
            *_DOT_ARGS,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        logger.warning(
            "Graphviz `dot` binary not available — diagram fallback to standard layout"
        )
        return None

    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(dot.encode("utf-8")),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        logger.warning("Graphviz dot timed out after %.1fs", timeout)
        return None

    if proc.returncode != 0:
        logger.warning(
            "Graphviz dot failed (rc=%s): %s",
            proc.returncode,
            stderr.decode(errors="replace")[:200],
        )
        return None

    if not stdout.startswith(_PNG_MAGIC):
        logger.warning(
            "Graphviz dot output is not PNG (first bytes: %r)", stdout[:8]
        )
        return None

    return stdout
