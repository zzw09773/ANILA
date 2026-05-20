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

from dataclasses import dataclass


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
