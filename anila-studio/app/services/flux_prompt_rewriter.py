"""Layer A — LLM prompt rewriter for FLUX image generation (Stage 1, 4.1).

Turns a slide's ``title`` + ``bullets`` into a single English, text-free,
house-styled FLUX prompt — or signals ``USE_GRAPHVIZ`` when the slide is
structural (architecture / flow / comparison) and should go through the
deterministic Graphviz path instead of diffusion.

Two hard rules from the research + prior ANILA rounds, enforced here:

  1. **CJK never reaches FLUX pixels.** The LLM is told to translate to
     English first; ``_strip_cjk_and_quoted`` is a deterministic second
     guard that removes any residual CJK characters.

  2. **No quoted literals.** Quoted strings in a prompt coax FLUX into
     trying to *render that text*, producing the garbled-letter artefacts
     prior rounds hit. The same guard strips quote-wrapped runs.

The ``llm`` argument is any object exposing
``async def complete(*, system: str, user: str) -> str``. studio.py wraps
its existing ``_call_llm_chat`` proxy call in such an adapter, so this
module never needs to know about DB sessions, model registry, or usage
metering — it just asks for one completion.
"""
from __future__ import annotations

import re
from typing import Protocol

from app.schemas.studio import ImageUseCase
from app.services.flux_style import StyleDescriptor


class _LLMCompleter(Protocol):
    """Minimal contract the rewriter needs from an LLM client."""

    async def complete(self, *, system: str, user: str) -> str: ...


_REWRITER_SYSTEM = """\
You convert a slide's title and bullet points into a single English
image-generation prompt for FLUX.2-dev. Output ONE paragraph, 40-80 words.

REQUIREMENTS
- Translate any non-English content to English first.
- Abstract the slide's core concept into ONE visual metaphor or scene.
- NO text, words, characters, letters, digits, logos, brand names,
  signage, charts, graphs, arrows, UI elements, or screenshots.
- Specify: lighting (quality/direction/color), materials, color palette,
  mood, composition, render style.
- If the slide is structural (architecture / flow / sequence / comparison
  table / org chart), output ONLY the string "USE_GRAPHVIZ".

OUTPUT: one paragraph only, no preamble, no JSON, no quotes.
"""


# CJK ranges: CJK Unified Ideographs (+ Ext A), Hiragana/Katakana, Hangul,
# and fullwidth/CJK punctuation. Anything matching is deleted outright.
_CJK_RE = re.compile(
    r"[　-〿"  # CJK symbols & punctuation
    r"぀-ヿ"  # Hiragana + Katakana
    r"㐀-䶿"  # CJK Ext A
    r"一-鿿"  # CJK Unified Ideographs
    r"가-힯"  # Hangul syllables
    r"＀-￯]+"  # Fullwidth / halfwidth forms
)

# Runs wrapped in single, double, or CJK quotation marks. Stripped because
# FLUX treats quoted text as "render this literally".
_QUOTED_RE = re.compile(
    r"[\"'‘’“”「」『』]"
    r"[^\"'‘’“”「」『』]*"
    r"[\"'‘’“”「」『』]"
)

# Collapse the whitespace left behind after deletions.
_WS_RE = re.compile(r"\s{2,}")


def _strip_cjk_and_quoted(text: str) -> str:
    """Remove quoted runs first (so a quoted CJK phrase goes wholesale),
    then any stray CJK characters, then tidy whitespace. Deterministic
    second line of defence behind the system-prompt instruction."""
    text = _QUOTED_RE.sub(" ", text)
    text = _CJK_RE.sub(" ", text)
    text = _WS_RE.sub(" ", text)
    return text.strip()


async def derive_flux_prompt(
    *,
    title: str,
    bullets: list[str],
    use_case: ImageUseCase,
    style: StyleDescriptor,
    llm: _LLMCompleter,
) -> str | None:
    """Return a FLUX prompt, or None if the slide should use graphviz.

    None means the rewriter judged the slide structural and emitted the
    ``USE_GRAPHVIZ`` sentinel — the caller should route to the Graphviz
    path (or, for a cover where that shouldn't happen, fall back gracefully).
    """
    user = (
        f"USE_CASE: {use_case.value}\n"
        f"TITLE: {title}\n"
        "BULLETS:\n"
        + "\n".join(f"- {b}" for b in bullets)
    )
    raw = (await llm.complete(system=_REWRITER_SYSTEM, user=user)).strip()
    if "USE_GRAPHVIZ" in raw:
        return None
    # Defence in depth: strip any residual CJK / quoted literals the LLM
    # left in despite the system prompt.
    raw = _strip_cjk_and_quoted(raw)
    if not raw:
        # Everything got stripped (e.g. LLM answered entirely in CJK) —
        # treat as "no usable prompt" so the caller falls back rather than
        # sending a bare style suffix to FLUX.
        return None
    return f"{raw} {style.suffix}".strip()
