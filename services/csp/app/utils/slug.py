# -*- coding: utf-8 -*-
"""URL-safe slug generation for RegisteredService identifiers.

The slug is the stable string service id used as the launch-token ``aud``
claim and the ``/api/services/{service_id}`` path parameter, so it must be
deterministic, lowercase, and collision-checkable. Non-ASCII names (e.g.
Traditional-Chinese service titles) collapse to empty — callers fall back to a
prefixed row id in that case.
"""

from __future__ import annotations

import re

_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


def slugify(value: str) -> str:
    """Lowercase, collapse non-alphanumerics to single hyphens, trim.

    Returns ``""`` when nothing usable survives (e.g. a purely CJK name) so
    the caller can substitute a stable fallback.
    """
    if not value:
        return ""
    lowered = value.strip().lower()
    slug = _SLUG_STRIP.sub("-", lowered).strip("-")
    return slug


def unique_slug(value: str, taken: set[str], *, fallback: str = "service") -> str:
    """Return a slug for ``value`` not already present in ``taken``.

    Appends ``-2``, ``-3``, … on collision. ``fallback`` seeds the slug when
    ``slugify`` yields nothing. ``taken`` is NOT mutated (caller owns it).
    """
    base = slugify(value) or fallback
    candidate = base
    n = 2
    while candidate in taken:
        candidate = f"{base}-{n}"
        n += 1
    return candidate
