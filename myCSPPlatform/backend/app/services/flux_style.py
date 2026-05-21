"""FLUX house-style descriptor — Stage 1 locked contract 3.6.

Layer A (the prompt rewriter) needs a house-style *suffix* to append to
every FLUX prompt so the whole deck shares one visual language, and the
provider needs a `style_id` to fold into its cache key (contract 3.3) so
that swapping styles in a later stage doesn't return stale cached images.

This module owns both the data shape (`StyleDescriptor`) and the lookup
(`get_style_descriptor`). The lookup interface is frozen now; only the
*implementation* changes per stage:

  * Stage 1 (here): hardcoded `_DEFAULT_STYLE`, ignores `brand_id`.
  * Stage 3: `get_style_descriptor` reads `brand.yaml` keyed by brand_id
    and may populate `lora_path` / `redux_ref_path`.

Because `style_id` is already in the cache key, switching from the
hardcoded default to a brand-specific style in Stage 3 will not collide
with Stage 1 cached PNGs.
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import Protocol

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class StyleDescriptor:
    """A house visual style.

    `style_id`  goes into the FLUX provider cache key (3.3) so two styles
                never share a cached image.
    `suffix`    is appended verbatim to every rewriter-produced FLUX prompt.
    `lora_path` / `redux_ref_path` are Stage 3+ knobs; defined now so the
                FLUX service contract and provider signature don't have to
                grow new parameters later.
    """

    style_id: str
    suffix: str
    lora_path: str | None = None
    redux_ref_path: str | None = None


# Stage 1 hardcoded house style. The suffix encodes the two rules the
# research + prior ANILA rounds insisted on:
#   1. A consistent flat-editorial / isometric look across the deck.
#   2. Positive phrasing of "no text" (FLUX is guidance-distilled and
#      does NOT honour negative prompts, so "no text, no letters, ..."
#      must ride inside the positive prompt body).
_DEFAULT_STYLE = StyleDescriptor(
    style_id="default",
    suffix=(
        "flat editorial illustration, isometric perspective, "
        "muted teal and warm gray palette, soft diffused lighting, "
        "generous negative space, clean unmarked surfaces, "
        "no text, no letters, no symbols, no signage"
    ),
)


def get_style_descriptor(brand_id: str | None = None) -> StyleDescriptor:
    """Return the house style for a brand.

    Stage 1: always the hardcoded default (brand_id ignored).
    Stage 3: will read brand.yaml keyed by brand_id.
    """
    return _DEFAULT_STYLE


# ── Stage 3: content-inferred deck style ───────────────────────────────────
_STYLE_SYSTEM = """\
You are an art director. Given a presentation's title and a sample of its
source content, output ONE concise visual house-style descriptor for the
deck's slide illustrations: palette, illustration/render style, lighting,
mood, and composition. 12-40 words. Output ONLY the style phrase — no
preamble, no explanation, no quotes, no sentences describing the topic.
The illustrations must contain no text or letters.
"""

# Positive "no text" guard. FLUX is guidance-distilled and ignores negative
# prompts, so this must ride inside the positive suffix. Appended whenever the
# LLM's free-form style omits it.
_NO_TEXT_GUARD = "no text, no letters, no symbols, no signage"

_MAX_CONTENT_SAMPLE = 1500  # chars of source content fed to the LLM: keep it cheap
_MAX_SUFFIX_LEN = 400
_MIN_SUFFIX_LEN = 10


class _LLMCompleter(Protocol):
    """Minimal LLM contract: one completion. (Same shape as the rewriter's.)"""

    async def complete(self, *, system: str, user: str) -> str: ...


def _hash8(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:8]


def _normalize_suffix(text: str) -> str:
    """Collapse all whitespace (incl. newlines) to single spaces, trim, cap."""
    collapsed = " ".join(text.split())
    return collapsed[:_MAX_SUFFIX_LEN].strip()


async def infer_deck_style(
    *,
    title: str,
    content_sample: str,
    llm: _LLMCompleter,
) -> StyleDescriptor:
    """Infer one house style for the whole deck from its title + content.

    Free-form: the LLM writes a style phrase; we normalize it, guarantee the
    no-text guard, and derive a stable ``style_id`` from the suffix hash so the
    FLUX provider cache (contract 3.3) is deterministic for the same style.

    Any failure (LLM error, empty/too-short output) degrades to
    ``_DEFAULT_STYLE`` — style inference must never break a job.
    """
    sample = (content_sample or "")[:_MAX_CONTENT_SAMPLE]
    user = f"TITLE: {title}\nCONTENT:\n{sample}"
    try:
        raw = await llm.complete(system=_STYLE_SYSTEM, user=user)
    except Exception as e:  # noqa: BLE001
        logger.warning("Style inference LLM call failed: %s — using default", e)
        return _DEFAULT_STYLE

    suffix = _normalize_suffix(raw or "")
    if len(suffix) < _MIN_SUFFIX_LEN:
        logger.warning(
            "Style inference produced too-short suffix %r — using default", suffix
        )
        return _DEFAULT_STYLE

    if "no text" not in suffix.lower():
        suffix = f"{suffix}, {_NO_TEXT_GUARD}"

    return StyleDescriptor(style_id=f"auto-{_hash8(suffix)}", suffix=suffix)
