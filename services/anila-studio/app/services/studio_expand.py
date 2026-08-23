"""Coverage pass: add real pages when a long-form draft leaves sources unused.

The first LLM call treats 12 as "done". Layout rebalance only swaps
``layout_kind``. This module looks at indexed chunks vs cites and, for
詳細簡報 / 經典報告, inserts new claim-titled slides. 口講用短頁 is
never padded.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from pydantic import ValidationError

from app.schemas.studio import Slide, SlidesSpec
from app.services.llm_json import (
    extract_json_object,
    loads_lenient,
)
from app.services.studio_config import SLIDES_LLM_MODEL
from app.services.studio_llm import (
    _EXPANDABLE_PRESETS,
    _is_short_talk,
    build_expand_slides_prompt,
    call_llm_chat,
)

logger = logging.getLogger(__name__)

_CITE_NUM_RE = re.compile(r"\[(\d+)\]")
_SLIDES_SPEC_MAX = 30
_MIN_CHUNK_CHARS = 20


def _cite_nums(*texts: str | None) -> set[int]:
    found: set[int] = set()
    for text in texts:
        if not text:
            continue
        for match in _CITE_NUM_RE.finditer(text):
            found.add(int(match.group(1)))
    return found


def _slide_texts(slide: Slide) -> list[str | None]:
    texts: list[str | None] = [slide.title, slide.speaker_notes, *slide.bullets]
    if slide.quote is not None:
        texts.extend([slide.quote.text, slide.quote.attribution])
    if slide.stat is not None:
        texts.extend([slide.stat.label, slide.stat.supporting])
    if slide.columns:
        for column in slide.columns:
            texts.append(column.heading)
            texts.extend(column.bullets)
    if slide.icon_rows:
        for row in slide.icon_rows:
            texts.extend([row.heading, row.description])
    return texts


def cited_chunk_indexes(spec: SlidesSpec) -> set[int]:
    """1-based chunk indexes already used by a slide."""
    cited: set[int] = set()
    for slide in spec.slides:
        cited.update(slide.citation_refs or [])
        cited.update(_cite_nums(*_slide_texts(slide)))
    return cited


def uncovered_chunks(
    spec: SlidesSpec,
    chunks: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Indexed hits that the draft never cited, with their 1-based index."""
    cited = cited_chunk_indexes(spec)
    leftover: list[dict[str, Any]] = []
    for i, chunk in enumerate(chunks, start=1):
        if i in cited:
            continue
        content = str(chunk.get("content") or "").strip()
        if len(content) < _MIN_CHUNK_CHARS:
            continue
        leftover.append({
            "index": i,
            "filename": chunk.get("filename") or "<unknown>",
            "content": content,
        })
    return leftover


def should_expand_deck(
    preset: str,
    spec: SlidesSpec,
    chunks: list[dict[str, Any]],
) -> bool:
    """True when a long-form draft is thin or barely cites leftover sources."""
    if _is_short_talk(preset):
        return False
    if preset.strip() not in _EXPANDABLE_PRESETS:
        return False
    if not chunks:
        return False
    n = len(spec.slides)
    if n >= _SLIDES_SPEC_MAX:
        return False
    leftover = uncovered_chunks(spec, chunks)
    if not leftover:
        return False
    cited_n = max(0, len(chunks) - len(leftover))
    cited_ratio = cited_n / len(chunks)
    below_floor = n < 12
    thin_leftover = n <= 15 and len(leftover) >= 2
    barely_cited = cited_ratio < 0.5 and n < 24
    return below_floor or thin_leftover or barely_cited


def _norm_title(title: str) -> str:
    return re.sub(r"\s+", "", title.replace("（重做）", "")).casefold()


def _insert_before_takeaway(slides: list[Slide], new_slides: list[Slide]) -> list[Slide]:
    """Keep cover first and the last page as the close; splice in the middle."""
    if not new_slides:
        return slides
    if len(slides) <= 1:
        return list(slides) + new_slides
    return list(slides[:-1]) + new_slides + [slides[-1]]


def merge_expanded_slides(
    spec: SlidesSpec,
    incoming: list[Slide],
    *,
    valid_chunk_indexes: set[int],
) -> SlidesSpec:
    """Append real new pages; drop filler, dups, and slides with no cite."""
    room = _SLIDES_SPEC_MAX - len(spec.slides)
    if room <= 0 or not incoming:
        return spec
    seen = {_norm_title(s.title) for s in spec.slides}
    kept: list[Slide] = []
    for slide in incoming:
        if len(kept) >= room:
            break
        title_key = _norm_title(slide.title)
        if not title_key or title_key in seen:
            continue
        refs = set(slide.citation_refs or []) | _cite_nums(*_slide_texts(slide))
        if not (refs & valid_chunk_indexes):
            continue
        seen.add(title_key)
        kept.append(slide)
    if not kept:
        return spec
    return spec.model_copy(
        update={"slides": _insert_before_takeaway(list(spec.slides), kept)},
    )


def _coerce_new_slides(parsed: Any) -> list[dict[str, Any]]:
    if isinstance(parsed, dict) and isinstance(parsed.get("slides"), list):
        return [s for s in parsed["slides"] if isinstance(s, dict)]
    if isinstance(parsed, list):
        return [s for s in parsed if isinstance(s, dict)]
    if isinstance(parsed, dict) and parsed.get("title") and parsed.get("bullets"):
        return [parsed]
    return []


def expand_add_count(spec: SlidesSpec, leftover: list[dict[str, Any]]) -> int:
    """How many pages to ask for: cover leftovers, stay in 12–15 then up to 30."""
    room = max(0, _SLIDES_SPEC_MAX - len(spec.slides))
    if room == 0 or not leftover:
        return 0
    # Prefer to leave the 12–15 band with one page per leftover claim,
    # but do not ask for more than 8 in one pass.
    want = max(2, min(len(leftover), 8))
    if len(spec.slides) < 15:
        want = max(want, 15 - len(spec.slides))
    return min(room, want)


async def expand_undercovered_deck(
    spec: SlidesSpec,
    chunks: list[dict[str, Any]],
    preset: str,
    *,
    bearer: str,
) -> SlidesSpec:
    """One LLM pass that inserts pages for leftover sources. No-op on failure."""
    if not should_expand_deck(preset, spec, chunks):
        return spec
    leftover = uncovered_chunks(spec, chunks)
    add_count = expand_add_count(spec, leftover)
    room = _SLIDES_SPEC_MAX - len(spec.slides)
    if add_count <= 0 or room <= 0:
        return spec
    system, user = build_expand_slides_prompt(
        spec.title,
        [s.title for s in spec.slides],
        leftover,
        add_count,
        room,
    )
    try:
        raw = await call_llm_chat(
            bearer,
            SLIDES_LLM_MODEL,
            [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=0.3,
        )
        parsed = loads_lenient(extract_json_object(raw))
    except (ValueError, ValidationError, TypeError) as exc:
        logger.warning("Expand LLM failed: %s — keeping original spec", exc)
        return spec

    new_slides: list[Slide] = []
    for row in _coerce_new_slides(parsed):
        try:
            new_slides.append(Slide.model_validate(row))
        except ValidationError:
            continue
    valid = set(range(1, len(chunks) + 1))
    merged = merge_expanded_slides(spec, new_slides, valid_chunk_indexes=valid)
    if len(merged.slides) > len(spec.slides):
        logger.info(
            "Expanded deck %s → %s slides (%d leftover sources)",
            len(spec.slides), len(merged.slides), len(leftover),
        )
    return merged
