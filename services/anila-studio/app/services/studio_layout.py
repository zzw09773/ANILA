"""Studio layout post-validation + LLM rebalance pass (Studio Fix 1).

Extracted from ``api/studio.py`` (god-module split, final block). The slide
LLM is told "standard layout ≤ 60%" but doesn't follow proportional rules
under cognitive load, so this module audits the produced spec deterministically
and, when hard rules fire, issues ONE focused LLM call that only re-selects
layout_kind on a small candidate set. It also applies deterministic
title-keyword theme overrides (Round 5 Patch U).

Public surface (re-exported by ``app.api.studio`` for ``_run_pipeline``):
  * ``_apply_theme_title_override`` — force a theme from title framing words.
  * ``_audit_layout_distribution`` — produce the LayoutViolation list.
  * ``_should_rebalance`` — decide whether the violations warrant a fix pass.
  * ``_rebalance_layouts`` — run the surgical layout-only rebalance pass.

The rest (candidate selection / prompt build / LLM call / change application)
are module-internal; tests patch ``_call_llm_for_rebalance`` and import
``LayoutViolation`` / ``LAYOUT_REBALANCE_MAX_CHANGES`` from this module.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Literal

from app.schemas.studio import SlidesSpec, Step
from app.services.llm_json import (
    extract_json_object as _extract_json_object,
    loads_lenient as _loads_lenient,
)
from app.services.studio_config import SLIDES_LLM_MODEL
from app.services.studio_llm import call_llm_chat as _call_llm_chat

logger = logging.getLogger(__name__)


#
# The system prompt tells gemma4 "standard layout ≤ 60% of slides", but
# attention is split between content writing and layout selection, so the
# rule isn't actually enforced. Decks come back with 8-of-9 standard
# slides, no stat_callout for clearly numeric content, and 3+ consecutive
# bullet pages — visually flat. This module audits the produced spec
# deterministically and (when hard rules fire) issues ONE focused LLM
# call that only re-selects layout_kind on a small candidate set.
#
# Why not just bump temperature or rewrite the system prompt?
# - Bumping temperature degrades JSON validity (we did this once already).
# - Rewriting the prompt for the 9th time chases an asymptote — gemma4
#   doesn't follow proportional rules under cognitive load.
# Deterministic audit + surgical LLM fix is cheaper and more reliable.


# Hard rule V1: standard layout proportion must not exceed this cap.
LAYOUT_STANDARD_MAX_RATIO = 0.60

# Soft rule V3: runs of this many or more consecutive standard slides
# count as a "flat stretch" that hurts visual rhythm.
LAYOUT_CONSECUTIVE_STANDARD_LIMIT = 3

# V4: enumeration title keywords. When a slide title contains any of these
# AND it has 3+ bullets AND layout_kind=standard, it's a textbook candidate
# for icon_rows — the LLM "described 3 things" but didn't reach for the
# matching layout.
_ENUMERATION_KEYWORDS = (
    # Numeric enumeration
    "三大", "四大", "五大", "兩大", "三項", "三類",
    # Process / sequence
    "步驟", "階段", "流程", "歷程", "順序",
    "workflow", "pipeline", "process",
    # Structural enumeration
    "面向", "層面", "維度", "方面",
    # Architecture / topology (slide 13 case)
    "架構", "拓撲", "拓樸", "結構", "設計", "佈局",
    # Capability / function lists
    "核心能力", "能力", "功能", "特性", "特徵",
    # Strategy / approach
    "策略", "方案", "模式", "機制", "方法",
    # Comparison framing (space-padded vs to avoid 'previous' false matches)
    "對比", "對照", " vs ", " vs.",
)

# ── Round 5 Patch U: deterministic title-keyword theme overrides ──
#
# LLM tone detection (Patch O) is non-deterministic at signal boundaries.
# v4 picked warm_journal for "11月學習心得報告"; v5 picked corporate_navy
# on essentially the same content because chunks lean technical and the
# only warm_journal signal was the "心得" in the title.
#
# Architectural decision: title is the strongest author-intent signal —
# the framing the author explicitly chose. When title contains an
# unambiguous theme keyword, override the LLM's tone-based choice.
#
# Patterns are deliberately CONSERVATIVE (only high-confidence keywords).
# Title with no match leaves the LLM's choice intact. This is opt-in
# overriding, not blanket replacement.
_THEME_TITLE_OVERRIDES: list[tuple[re.Pattern[str], str]] = [
    # ── warm_journal: personal reflection framings ──
    (re.compile(r"心得|反思|回顧|感想|札記|手記"), "warm_journal"),

    # ── academic_paper: scholarly/conference framings ──
    (re.compile(
        r"論文|研究發表|期刊論文|workshop|conference paper|"
        r"研討會|學會發表",
        re.IGNORECASE,
    ), "academic_paper"),

    # ── startup_pitch: external pitch framings ──
    (re.compile(
        r"募資|產品發表|launch event|pitch deck|"
        r"投資人簡報|demo day",
        re.IGNORECASE,
    ), "startup_pitch"),

    # ── executive_brief: high-level briefing framings ──
    (re.compile(
        r"executive briefing|高層 review|主管 briefing|"
        r"季度 review|半年檢討|年度檢討",
        re.IGNORECASE,
    ), "executive_brief"),
]


_ARROW_SPLIT_RE = re.compile(r"\s*(?:→|->|➜|⇒)\s*")
_MARKER_RE = re.compile(r"^[●◦▪○◯■▫・]\s*")


def convert_arrow_bullets_to_process(spec: SlidesSpec) -> SlidesSpec:
    """A standard slide whose bullets are really one「A → B → C」chain gets the
    process layout deterministically (the live deck wrote 申訴 flows as arrow
    bullets under a heading instead of using process).

    Rule: collect the bullets that contain ≥2 arrows; if together they yield
    2-6 steps and no other substantive bullet remains (a heading-only
    bullet that repeats the title is ignored), convert."""
    out = []
    changed = False
    for s in spec.slides:
        if s.layout_kind != "standard":
            out.append(s); continue
        steps: list[str] = []
        others: list[str] = []
        for b in s.bullets:
            text = _MARKER_RE.sub("", str(b)).strip()
            parts = [p.strip() for p in _ARROW_SPLIT_RE.split(text) if p.strip()]
            if len(parts) >= 3:
                steps.extend(parts)
            elif text and text != (s.title or "").strip():
                others.append(text)
        if 2 <= len(steps) <= 6 and not others:
            out.append(s.model_copy(update={
                "layout_kind": "process",
                "steps": [Step(heading=p[:80], description="") for p in steps],
            }))
            changed = True
            logger.info("arrow bullets → process on '%s' (%d steps)", s.title, len(steps))
        else:
            out.append(s)
    return spec.model_copy(update={"slides": out}) if changed else spec


def drop_redundant_section_breaks(spec: SlidesSpec) -> SlidesSpec:
    """A section_break immediately followed by a content slide with the same
    title is the model mistaking "章節頁" for "每張前面加一頁" — the live
    two-pass deck had 「依身分區分之懲罰種類」twice in a row. Drop the break;
    the cover (index 0) is never touched."""
    slides = spec.slides
    keep = []
    for i, s in enumerate(slides):
        nxt = slides[i + 1] if i + 1 < len(slides) else None
        if (
            i > 0
            and s.layout_kind == "section_break"
            and nxt is not None
            and nxt.layout_kind != "section_break"
            and (nxt.title or "").strip() == (s.title or "").strip()
        ):
            logger.info("dropping redundant section_break before '%s'", s.title)
            continue
        keep.append(s)
    if len(keep) == len(slides):
        return spec
    return spec.model_copy(update={"slides": keep})


def _apply_theme_title_override(spec: SlidesSpec) -> SlidesSpec:
    """Deterministic title-keyword override for theme selection.

    Runs AFTER schema validation succeeds so ``spec.theme`` is always a
    valid theme (either LLM-chosen or palette-derived). If ``spec.title``
    matches a high-confidence keyword pattern, force the corresponding
    theme.

    Idempotent and pure: same input → same output, no I/O beyond logging.
    No-op when title has no match (preserves LLM choice).
    """
    title = (spec.title or "").strip()
    if not title:
        return spec

    for pattern, target_theme in _THEME_TITLE_OVERRIDES:
        if pattern.search(title):
            if spec.theme != target_theme:
                logger.info(
                    "Theme title-override: '%s' matched %r → "
                    "switching theme %s → %s",
                    title, pattern.pattern, spec.theme, target_theme,
                )
                spec.theme = target_theme
            else:
                logger.debug(
                    "Theme title-override: '%s' matched %r, "
                    "theme already %s (no-op)",
                    title, pattern.pattern, target_theme,
                )
            return spec

    return spec


# Round 3 PRIMARY V4 signal: "label: description" bullet pattern.
#
# Matches CJK 2-6 char label + (half- or full-width) colon + non-empty tail.
# Tuned conservatively: requires the label to be entirely CJK so bullets like
# "Token 消耗降低" (mixed Latin) don't false-trigger.
_LABEL_BULLET_RE = re.compile(
    r"^\s*[一-鿿]{2,6}\s*[:：]\s*\S.*$",
)

# Fraction of bullets that must match _LABEL_BULLET_RE for the
# content-pattern V4 path to fire. 0.7 = 3 of 4, or 2 of 3.
_LABEL_PATTERN_THRESHOLD = 0.7

# V2: numeric-content regex. Matches percentages, big numbers, F1 scores,
# and sample sizes (N=xxx). When chunks_text matches AND spec has zero
# stat_callout slides, we missed a visual opportunity for a key statistic.
_NUMERIC_CONTENT_RE = re.compile(
    r"\d+(\.\d+)?\s*[%％]|\d{4,}|F1[-\s]?score|N\s*=\s*\d+",
    re.IGNORECASE,
)

# Cap on LLM-proposed changes. The whole point of this pass is "surgical
# layout-only edit" — letting the LLM rewrite half the deck defeats the
# purpose and risks breaking content that the original generate-step got
# right. 3 changes is enough to fix V1+V2 on a typical 9-slide deck.
LAYOUT_REBALANCE_MAX_CHANGES = 3


@dataclass(frozen=True)
class LayoutViolation:
    """One audit finding from `_audit_layout_distribution`.

    Hard violations (V1, V2) trigger the rebalance LLM call. Soft
    violations (V3, V4) are reported on candidates so the LLM has guidance
    on WHICH slides to re-layout; firing alone they don't trigger a call.
    """

    kind: str  # "V1" | "V2" | "V3" | "V4_CONTENT" | "V4_TITLE"
    severity: Literal["hard", "soft", "hint"]
    slide_indices: list[int] = field(default_factory=list)
    detail: str = ""


def _audit_layout_distribution(
    spec: SlidesSpec,
    chunks_text: str,
) -> list[LayoutViolation]:
    """Deterministic audit of layout distribution on a validated SlidesSpec.

    Pure function (no I/O, no LLM call). Walks the slides once and emits
    violations per rule:

    - V1 (hard): standard layout > 60% of slides.
    - V2 (hard): chunks contain numeric content (percentages, F1-score,
      N=xxx, big numbers) AND no slide uses `stat_callout`.
    - V3 (soft): 3 or more consecutive `standard` slides. Each run becomes
      its own violation, with the first slide of the run as the candidate
      for re-layout.
    - V4 (soft): slide title matches an enumeration keyword AND has 3+
      bullets AND layout_kind is "standard". Each matching slide is its
      own violation.

    The caller decides whether to invoke `_rebalance_layouts` — typically
    only when at least one HARD violation is present. Soft violations are
    used to seed the candidate list for the LLM call.
    """
    violations: list[LayoutViolation] = []
    slides = spec.slides
    n = len(slides)
    if n == 0:
        return violations

    # ── V1: standard layout proportion ──
    standard_count = sum(1 for s in slides if s.layout_kind == "standard")
    standard_ratio = standard_count / n
    if standard_ratio > LAYOUT_STANDARD_MAX_RATIO:
        violations.append(
            LayoutViolation(
                kind="V1",
                severity="hard",
                slide_indices=[
                    i for i, s in enumerate(slides)
                    if s.layout_kind == "standard"
                ],
                detail=(
                    f"standard 比例 {standard_ratio:.0%} 超過上限 "
                    f"{LAYOUT_STANDARD_MAX_RATIO:.0%}（{standard_count}/{n}）"
                ),
            )
        )

    # ── V2: numeric content without stat_callout ──
    has_stat = any(s.layout_kind == "stat_callout" for s in slides)
    if not has_stat and chunks_text and _NUMERIC_CONTENT_RE.search(chunks_text):
        violations.append(
            LayoutViolation(
                kind="V2",
                severity="hard",
                slide_indices=[],  # no specific candidate; LLM picks
                detail=(
                    "Chunks 含關鍵數據（百分比/F1/N=…）但 spec 沒有任何 "
                    "stat_callout 投影片"
                ),
            )
        )

    # ── V5 (hard): hollow standard slide — one bullet on an otherwise empty
    # page. Reads as a placeholder; the rebalance pass may rewrite it.
    for i, s in enumerate(slides):
        if s.layout_kind == "standard" and len(s.bullets) <= 1 and i > 0:
            violations.append(
                LayoutViolation(
                    kind="V5_HOLLOW",
                    severity="hard",
                    slide_indices=[i],
                    detail=f"slide #{i}「{s.title}」只有 {len(s.bullets)} 條 bullet，整頁空心",
                )
            )

    # ── V3: consecutive standard runs ──
    run_start: int | None = None
    run_len = 0
    for i, s in enumerate(slides):
        if s.layout_kind == "standard":
            if run_start is None:
                run_start = i
                run_len = 1
            else:
                run_len += 1
        else:
            if run_start is not None and run_len >= LAYOUT_CONSECUTIVE_STANDARD_LIMIT:
                violations.append(
                    LayoutViolation(
                        kind="V3",
                        severity="soft",
                        slide_indices=list(range(run_start, run_start + run_len)),
                        detail=(
                            f"連續 {run_len} 張 standard 投影片 "
                            f"(slides {run_start}-{run_start + run_len - 1})"
                        ),
                    )
                )
            run_start = None
            run_len = 0
    # Trailing run at end of deck.
    if run_start is not None and run_len >= LAYOUT_CONSECUTIVE_STANDARD_LIMIT:
        violations.append(
            LayoutViolation(
                kind="V3",
                severity="soft",
                slide_indices=list(range(run_start, run_start + run_len)),
                detail=(
                    f"連續 {run_len} 張 standard 投影片 "
                    f"(slides {run_start}-{run_start + run_len - 1})"
                ),
            )
        )

    # ── V4: enumeration title OR label-pattern bullets + 3+ bullets ──
    #
    # Round 2 used title-keyword only. Round 3 adds a primary CONTENT signal:
    # if ≥70% of bullets follow the "<CJK label>: <description>" shape, the
    # slide is an icon_rows candidate regardless of title wording. Empirically
    # this rescues slides like 「執行摘要」 whose title carries no keyword but
    # whose bullets are textbook icon_rows material.
    #
    # Round 4 Patch R: the LLM learned to dodge V4 by emitting
    # layout_kind="image_focus" with image_kind="illustration" and no real
    # image — visually identical to the standard-with-bullets case the
    # audit was meant to catch. Treat that disguise as an audit candidate
    # too. Real diagrams (diagram_dot present) and real images (image_ref
    # present) remain exempt because they actually carry a visual asset.
    for i, s in enumerate(slides):
        is_disguise = False
        if s.layout_kind == "standard":
            pass  # original V4 path
        elif (
            s.layout_kind == "image_focus"
            and getattr(s, "image_kind", None) == "illustration"
            and not getattr(s, "image_ref", None)
            and not getattr(s, "diagram_dot", None)
        ):
            # Fake image_focus: claims to be image-led but has no real
            # image bound. Audit it with the same content rules as
            # standard so it gets rebalanced into icon_rows / etc.
            is_disguise = True
        else:
            continue
        # A disguise slide is a hollow shell no matter how few bullets it
        # has — the 2026-09-02 deck's one-bullet "流程圖" page sailed through
        # here because of the 3-bullet gate.
        if len(s.bullets) < 3 and not is_disguise:
            continue
        title_low = s.title.lower()
        matched_kw = next(
            (kw for kw in _ENUMERATION_KEYWORDS if kw.lower() in title_low),
            None,
        )
        title_match = matched_kw is not None

        # Round 3 primary signal: bullet content pattern.
        pattern_matches = sum(
            1 for b in s.bullets if _LABEL_BULLET_RE.match(str(b))
        )
        pattern_match = (
            pattern_matches / len(s.bullets) >= _LABEL_PATTERN_THRESHOLD
        )

        # Disguise slides bypass the title/pattern gate — the LLM has
        # already declared intent to dodge the audit, so the layout
        # itself is the violation regardless of bullet shape.
        if not (title_match or pattern_match or is_disguise):
            continue

        # Round 6 Patch V: split V4 by signal strength so the audit's
        # judgement aligns with what the rebalance LLM can actually act on.
        #   - STRONG (V4_CONTENT, soft): bullets ARE label:description, or the
        #     slide is an image_focus disguise. Mechanically convertible to
        #     icon_rows → actionable → triggers rebalance.
        #   - WEAK (V4_TITLE, hint): title merely contains an enumeration
        #     keyword but the bullets are flowing narrative. Forcing icon_rows
        #     would mean fabricating headings, so the LLM correctly refuses.
        #     Logged for observability but never actioned.
        # When both signals fire, content-pattern dominates (strong wins).
        layout_marker = (
            "image_focus_disguise" if is_disguise else "standard layout"
        )
        base_detail = (
            f"slide #{i}「{s.title}」: {len(s.bullets)} bullets, {layout_marker}"
        )

        if pattern_match or is_disguise:
            detail = base_detail
            if pattern_match:
                detail += f", bullet-pattern {pattern_matches}/{len(s.bullets)}"
            if title_match:
                detail += f", title-keyword '{matched_kw}'"
            violations.append(
                LayoutViolation(
                    kind="V4_CONTENT",
                    severity="soft",
                    slide_indices=[i],
                    detail=detail,
                )
            )
        else:
            # title_match only → weak hint.
            violations.append(
                LayoutViolation(
                    kind="V4_TITLE",
                    severity="hint",
                    slide_indices=[i],
                    detail=f"{base_detail}, title-keyword '{matched_kw}' (hint only)",
                )
            )

    if violations:
        logger.info(
            "[H-DIAG] audit found %d violations: %s",
            len(violations),
            [
                f"{v.kind}({v.severity})@{v.slide_indices}"
                for v in violations
            ],
        )
        for v in violations:
            logger.info("[H-DIAG]   %s: %s", v.kind, v.detail)
    else:
        logger.info("[H-DIAG] audit found no violations")
    return violations


def _should_rebalance(violations: list[LayoutViolation]) -> bool:
    """Decide whether to invoke the LLM rebalance pass.

    Round 1 only fired on hard violations (V1, V2). Round 2 broadened this
    on a count of soft V4. Round 6 Patch V re-aligns the trigger with the
    split V4 signal:

    Triggers (any one suffices):
      - Any hard violation (V1 or V2) — original behaviour
      - 1 or more V4_CONTENT violations — a single bullet-pattern (or
        disguise) slide is an obvious icon_rows candidate worth fixing.

    V4_TITLE violations are HINTS only: the title carries an enumeration
    keyword but the bullets are flowing narrative, so the LLM correctly
    refuses to restructure. They never trigger a rebalance call (the old
    `v4_count >= 2` / `soft_count >= 3` thresholds caused false-positive
    "rebalance failed" trails when the LLM rightly skipped such slides).
    """
    has_hard = any(v.severity == "hard" for v in violations)
    content_v4 = sum(1 for v in violations if v.kind == "V4_CONTENT")
    decision = has_hard or content_v4 >= 1
    logger.info(
        "[H-DIAG] should_rebalance: hard=%s v4_content=%d → %s",
        has_hard, content_v4, decision,
    )
    return decision


def _select_rebalance_candidates(
    violations: list[LayoutViolation],
) -> list[int]:
    """Pick a focused candidate set of slide indices for the LLM to re-layout.

    Strategy:
    - All V4_CONTENT slides (label-pattern bullets / disguise — actionable).
      V4_TITLE hints are excluded: the LLM would only decline them.
    - First slide of each V3 run (1 representative per consecutive-standard
      stretch — rebalancing that one slide breaks up the run).

    Order: V4_CONTENT first (most specific), then V3 starters, deduped.
    """
    seen: set[int] = set()
    ordered: list[int] = []
    for v in violations:
        if v.kind == "V5_HOLLOW":
            for idx in v.slide_indices:
                if idx not in seen:
                    seen.add(idx)
                    ordered.append(idx)
    for v in violations:
        if v.kind == "V4_CONTENT":
            for idx in v.slide_indices:
                if idx not in seen:
                    seen.add(idx)
                    ordered.append(idx)
    for v in violations:
        if v.kind == "V3" and v.slide_indices:
            first = v.slide_indices[0]
            if first not in seen:
                seen.add(first)
                ordered.append(first)
    return ordered


def _build_rebalance_prompt(
    spec_dict: dict[str, Any],
    violations: list[LayoutViolation],
    chunks_text: str,
    candidates: list[int],
) -> tuple[str, str]:
    """Render the focused (system, user) prompt for the rebalance LLM call.

    Compact view per slide — only the metadata the LLM needs to choose a
    new layout_kind. Title and bullets are read-only context; the prompt
    instructs the LLM to ONLY change layout_kind and the matching payload.
    """
    slides = spec_dict.get("slides", [])
    compact_slides = [
        {
            "slide_index": i,
            "title": s.get("title", ""),
            "layout_kind": s.get("layout_kind", "standard"),
            "bullet_count": len(s.get("bullets", []) or []),
        }
        for i, s in enumerate(slides)
    ]
    violation_lines = [
        f"- {v.kind}（{v.severity}）：{v.detail}"
        for v in violations
    ]
    system = (
        "你是 ANILA LM 的版型重新平衡助手。輸入是一份已通過 schema 驗證的 "
        "SlidesSpec，以及一份違反「版型分佈規則」的清單。\n\n"
        "**唯一任務：** 只改變指定投影片的 layout_kind 與對應 payload "
        "（icon_rows / stat / two_column 等），**絕對不要動 title、"
        "bullets、speaker_notes**。\n\n"
        "輸出 JSON 物件，只包含一個 `changes` 陣列；每個元素形如：\n"
        '  {"slide_index": int, "new_layout_kind": str, '
        '"new_payload": {...}}\n\n'
        "規則：\n"
        f"1. 最多輸出 {LAYOUT_REBALANCE_MAX_CHANGES} 個 change，挑最關鍵的。\n"
        "2. new_layout_kind 只能是：standard / section_break / "
        "stat_callout / quote / two_column / icon_rows / process / table。\n"
        "3. 改成 icon_rows 時，new_payload 必須含 `icon_rows` 欄位，"
        "至少 3 列、每列 {concept, heading, description}。\n"
        "4. 改成 stat_callout 時，new_payload 必須含 `stat` 欄位，"
        "{value, label, supporting(≥20字)}。\n"
        "5. 改成 two_column 時，new_payload 必須含 `columns` 欄位，"
        "2 個 column、每個至少 3 個 bullet。\n"
        "5a. 內容是「先…再…最後」的步驟時改成 process，new_payload 含 `steps`："
        "2-6 個 {heading, description}。\n"
        "5b. 內容是「A 有什麼、B 有什麼」的對照時改成 table，new_payload 含 `table`："
        "{columns: [2-5 欄], rows: [[…], …]}。\n"
        "6. 若違規 detail 含 `image_focus_disguise`（layout_kind=image_focus 但"
        "沒有 image_ref/diagram_dot 的偽裝）：必改成 icon_rows，把 bullets 轉成"
        " 3-4 列 {concept, heading, description}（concept 用英文），同時"
        "清掉 image_kind/image_ref/image_prompt/diagram_dot，保留 speaker_notes。\n"
        "7. 不要改的投影片直接不要出現在 changes 陣列。\n"
        "8. 違規 kind 是 V5_HOLLOW 的空心頁（整頁只有一條 bullet）例外：這一頁**可以補寫**內容——"
        "依「原始素材摘要」把它改成 process（steps 2-6 步）、icon_rows（3-4 列）或 table，"
        "或在 new_payload 給 `bullets`（3-5 條具體內容）留在 standard。其他頁仍然不准動文字。\n\n"
        "輸出第一字 {、最後字 }、不可前言、不可代碼塊。"
    )
    # Trim chunks_text — we only need the LLM to see roughly what data is
    # available, not the full retrieval payload.
    chunks_preview = (chunks_text or "")[:1500]
    user_msg = (
        f"違規清單：\n" + "\n".join(violation_lines) + "\n\n"
        f"建議優先重新選版的候選 slide_index：{candidates}\n\n"
        f"目前各投影片版型概況：\n"
        f"{json.dumps(compact_slides, ensure_ascii=False, indent=2)}\n\n"
        f"原始素材摘要（前 1500 字）：\n{chunks_preview}"
    )
    return system, user_msg


async def _call_llm_for_rebalance(
    prompt: tuple[str, str],
    *,
    bearer: str,
) -> dict[str, Any]:
    """Thin wrapper around ``_call_llm_chat`` for the rebalance pass.

    Extracted as its own helper so tests can mock the LLM round-trip
    without standing up the full csp proxy / model registry. Returns
    the parsed JSON dict (caller validates the `changes` shape).
    """
    system, user_msg = prompt
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user_msg},
    ]
    raw = await _call_llm_chat(
        bearer, SLIDES_LLM_MODEL, messages, temperature=0.2,
    )
    logger.info(
        "[H-DIAG] rebalance LLM raw response (first 2KB): %s",
        str(raw)[:2000],
    )
    try:
        extracted = _extract_json_object(raw)
        parsed = _loads_lenient(extracted)
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning("[H-DIAG] rebalance JSON parse failed: %s", exc)
        raise
    if not isinstance(parsed, dict):
        raise ValueError("rebalance LLM did not return a JSON object")
    changes = parsed.get("changes", [])
    if isinstance(changes, list):
        logger.info(
            "[H-DIAG] rebalance proposed %d changes: %s",
            len(changes),
            [
                f"slide{c.get('slide_index')}→{c.get('new_layout_kind')}"
                for c in changes
                if isinstance(c, dict)
            ],
        )
    else:
        logger.warning(
            "[H-DIAG] rebalance parsed but `changes` is not a list: %r",
            changes,
        )
    return parsed


# Payload field name keyed by layout_kind. When the LLM emits a change,
# we read the matching key out of `new_payload` and write it on the slide
# (also clearing the previous layout's payload to keep the spec clean).
_LAYOUT_PAYLOAD_FIELDS = {
    "standard": None,
    "section_break": None,
    "stat_callout": "stat",
    "quote": "quote",
    "two_column": "columns",
    "icon_rows": "icon_rows",
    "image_focus": None,
    "process": "steps",
    "table": "table",
    "sources": "sources",
}


def _apply_rebalance_change(
    spec_dict: dict[str, Any],
    change: dict[str, Any],
) -> bool:
    """Apply one LLM change to spec_dict in place. Returns True on success.

    Defensive: any malformed change (missing keys, out-of-range index,
    unknown layout_kind) is logged and skipped — we never raise from
    inside the apply loop because one bad change shouldn't tank the
    whole rebalance pass.
    """
    try:
        idx = int(change.get("slide_index", -1))
    except (TypeError, ValueError):
        logger.warning(
            "[H-DIAG] change FAILED to apply (missing slide_index): %r",
            change,
        )
        return False
    new_layout = change.get("new_layout_kind", "").strip().lower().replace("-", "_")
    new_payload = change.get("new_payload") or {}

    slides = spec_dict.get("slides", [])
    if not (0 <= idx < len(slides)):
        logger.warning(
            "[H-DIAG] change FAILED to apply (slide_index %s out of range)"
            "\n  raw change: %r",
            idx, change,
        )
        return False
    if new_layout not in _LAYOUT_PAYLOAD_FIELDS:
        logger.warning(
            "[H-DIAG] change FAILED to apply (unknown layout_kind %r)"
            "\n  raw change: %r",
            new_layout, change,
        )
        return False

    target = slides[idx]
    old_layout = target.get("layout_kind", "standard")
    target["layout_kind"] = new_layout
    # Hollow-slide rewrite (rule 8): a bullets list in the payload replaces
    # the placeholder bullet. Only accepted when it actually adds content.
    new_bullets = new_payload.get("bullets") if isinstance(new_payload, dict) else None
    if isinstance(new_bullets, list) and len([b for b in new_bullets if str(b).strip()]) >= 2:
        target["bullets"] = [str(b).strip() for b in new_bullets if str(b).strip()][:8]

    # Clear all layout-specific payload keys then set the new one. Keeping
    # leftovers around is harmless (Pydantic ignores them on the wrong
    # layout_kind) but makes the spec dict ambiguous to inspect.
    for field_name in ("stat", "quote", "columns", "icon_rows", "steps", "table", "sources"):
        target.pop(field_name, None)

    payload_field = _LAYOUT_PAYLOAD_FIELDS[new_layout]
    if payload_field is not None:
        # Accept either the named field nested in new_payload or
        # new_payload itself being the payload object.
        value = new_payload.get(payload_field, new_payload)
        target[payload_field] = value
    logger.info(
        "[H-DIAG] applied change to slide %d: %s → %s",
        idx, old_layout, new_layout,
    )
    return True


async def _rebalance_layouts(
    spec_dict: dict[str, Any],
    violations: list[LayoutViolation],
    chunks_text: str,
    *,
    bearer: str,
) -> dict[str, Any]:
    """Run the focused LLM rebalance pass and return an updated spec_dict.

    Contract:
    - Caps applied changes at `LAYOUT_REBALANCE_MAX_CHANGES`.
    - Re-validates the resulting spec via `SlidesSpec.model_validate`.
      If validation fails, the original spec_dict is returned (caller
      proceeds with the un-rebalanced spec, gracefully degrades).
    - Re-audits after applying; if V1 STILL violates, logs a warning and
      returns the (best-effort) rebalanced dict anyway. User prefers a
      slightly-imperfect deck over a 502.
    """
    # Round 6 Patch V: hint violations (V4_TITLE) are informational only.
    # Listing them in the prompt just makes the LLM waste tokens explaining
    # why it won't restructure flowing-narrative bullets. Drop them, and if
    # nothing actionable remains, skip the LLM call entirely.
    actionable = [v for v in violations if v.severity != "hint"]
    if not actionable:
        logger.info(
            "[H-DIAG] rebalance skipped: only hint violations present"
        )
        return spec_dict

    candidates = _select_rebalance_candidates(actionable)
    prompt = _build_rebalance_prompt(
        spec_dict, actionable, chunks_text, candidates,
    )
    logger.info(
        "[H-DIAG] rebalance LLM call: prompt_len=%d, n_actionable=%d",
        len(prompt[0]) + len(prompt[1]), len(actionable),
    )
    try:
        result = await _call_llm_for_rebalance(prompt, bearer=bearer)
    except Exception as exc:  # noqa: BLE001
        logger.warning("rebalance LLM call failed: %s", exc)
        return spec_dict

    raw_changes = result.get("changes")
    if not isinstance(raw_changes, list):
        logger.warning("rebalance: response missing `changes` list: %r", result)
        return spec_dict

    # Cap before applying — we don't even want to evaluate changes beyond
    # the cap, in case a malformed-but-valid change in slot 4 wastes log
    # noise.
    capped = raw_changes[:LAYOUT_REBALANCE_MAX_CHANGES]
    if len(raw_changes) > LAYOUT_REBALANCE_MAX_CHANGES:
        logger.info(
            "rebalance: capping %d proposed changes to %d",
            len(raw_changes), LAYOUT_REBALANCE_MAX_CHANGES,
        )

    # Apply on a deep copy so a Pydantic-validation failure leaves the
    # original spec_dict intact for the caller's fallback path.
    candidate_dict = json.loads(json.dumps(spec_dict))
    applied = 0
    for change in capped:
        if not isinstance(change, dict):
            continue
        if _apply_rebalance_change(candidate_dict, change):
            applied += 1

    if applied == 0:
        logger.info("rebalance: no changes applied (LLM returned empty / invalid set)")
        # Even with 0 applied changes, fall through to the post-audit so
        # callers see the warning path consistently.

    try:
        new_spec = SlidesSpec.model_validate(candidate_dict)
    except ValidationError as exc:
        logger.warning(
            "rebalance: post-apply spec validation failed (%s);"
            " keeping original spec",
            exc,
        )
        return spec_dict

    # Re-audit. V1 STILL violated → log and continue. We deliberately
    # don't raise — degrading gracefully is the explicit product choice.
    # Note: this call re-enters _audit_layout_distribution so position-1
    # [H-DIAG] log will appear a second time in the trail. Time order in
    # the log makes the post-rebalance pass obvious.
    post_violations = _audit_layout_distribution(new_spec, chunks_text)
    pre_content = sum(1 for v in violations if v.kind == "V4_CONTENT")
    post_content = sum(1 for v in post_violations if v.kind == "V4_CONTENT")
    post_hint = sum(1 for v in post_violations if v.kind == "V4_TITLE")
    logger.info(
        "[H-DIAG] post-rebalance audit: V4_CONTENT %d→%d, V4_TITLE %d (hints)",
        pre_content, post_content, post_hint,
    )
    if any(v.kind == "V1" for v in post_violations):
        logger.warning(
            "rebalance: V1 (standard > %d%%) still violates after %d changes"
            " — proceeding with rebalanced spec anyway",
            int(LAYOUT_STANDARD_MAX_RATIO * 100),
            applied,
        )

    return new_spec.model_dump(mode="json")
