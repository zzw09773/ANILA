"""第一方文字清理與固定台灣用語表；未列字元保留原文。

不提供完整簡繁轉換，不載入外部詞典。引用及 LaTeX 清理依欄位規則套用。
"""
from __future__ import annotations

import logging
import re
from typing import Any


from app.schemas.studio import (
    Column,
    Figure,
    FigureItem,
    IconRow,
    Quote,
    Slide,
    SlidesSpec,
    Stat,
    Step,
    TableSpec,
)

logger = logging.getLogger(__name__)


# Round 3 Patch I — LaTeX math-mode strip
#
# gemma4 occasionally emits LaTeX (`$\rightarrow$`) instead of the Unicode
# arrow (`→`) when reasoning about flows. The renderer prints LaTeX verbatim
# because pptxgenjs has no math support; v3 slide 12 showed raw `$\rightarrow$`.
# Catch common commands with an explicit table + a bounded generic fallback.
_LATEX_REPLACEMENTS = {
    # Arrows
    r"\$\\rightarrow\$": "→",
    r"\$\\leftarrow\$":  "←",
    r"\$\\Rightarrow\$": "⇒",
    r"\$\\Leftarrow\$":  "⇐",
    r"\$\\leftrightarrow\$": "↔",
    r"\$\\to\$":         "→",
    r"\$\\gets\$":       "←",
    r"\$\\mapsto\$":     "↦",
    # Math operators
    r"\$\\times\$":      "×",
    r"\$\\div\$":        "÷",
    r"\$\\pm\$":         "±",
    r"\$\\approx\$":     "≈",
    r"\$\\equiv\$":      "≡",
    r"\$\\neq\$":        "≠",
    r"\$\\geq\$":        "≥",
    r"\$\\leq\$":        "≤",
    r"\$\\sim\$":        "~",
    r"\$\\cdot\$":       "·",
    # Greek (common in ML papers)
    r"\$\\alpha\$":      "α",
    r"\$\\beta\$":       "β",
    r"\$\\gamma\$":      "γ",
    r"\$\\delta\$":      "δ",
    r"\$\\epsilon\$":    "ε",
    r"\$\\theta\$":      "θ",
    r"\$\\lambda\$":     "λ",
    r"\$\\mu\$":         "μ",
    r"\$\\pi\$":         "π",
    r"\$\\sigma\$":      "σ",
    r"\$\\tau\$":        "τ",
    r"\$\\phi\$":        "φ",
    r"\$\\omega\$":      "ω",
    r"\$\\Sigma\$":      "Σ",
    r"\$\\Delta\$":      "Δ",
    # Misc
    r"\$\\infty\$":      "∞",
    r"\$\\partial\$":    "∂",
    r"\$\\nabla\$":      "∇",
}

# Round 4 Patch S: JSON-eaten control-char fallbacks.
#
# When gemma4 emits LaTeX like "$\rightarrow$" in JSON string content,
# the JSON parser interprets `\r`, `\t`, `\n` as actual control chars
# (CR/TAB/LF) BEFORE this normalizer sees the text. The literal-LaTeX
# regex in _LATEX_REPLACEMENTS never matches because the backslash is
# already gone. These string-level replacements catch the post-parse
# residue.
_LATEX_BROKEN_CHAR_REPLACEMENTS = {
    # \r → CR (most common with $\rightarrow$ in JSON)
    "$\rightarrow$": "→",   # CR between $ and "ightarrow"
    "$\rightarrow":  "→",   # unclosed variant
    # \t → TAB
    "$\tightarrow$": "→",
    "$\tightarrow":  "→",
    # \n → LF
    "$\nightarrow$": "→",
    "$\nightarrow":  "→",
    # \v, \f, \b — same family
    "$\vightarrow$": "→",
    "$\fightarrow$": "→",
    "$\bightarrow$": "→",
    # Backslash-eaten Greek letters
    "$\theta$":     "θ",    # $ + TAB + "heta" + $
    "$\nu$":        "ν",    # $ + LF + "u" + $
}

# Generic fallback: strip dollar wrappers and keep inner. Bounded to avoid
# eating wide swaths of text on mismatched dollars.
_GENERIC_LATEX_RE = re.compile(r"\$([^\$\n]{1,80})\$")


# Round 4 Patch Q: RAG citation markers at end of bullets.
#
# Pattern variants observed in production:
#   "(參 [5])"       half-width parens, half-width brackets
#   "（參 [5]）"     full-width parens, half-width brackets
#   "(參 [12])"      multi-digit
#   "( 參 [5] )"     with internal whitespace
#   "(參考 [5])"     alternative wording
#
# These belong in speaker_notes (already populated by the LLM with chunk
# reference info), not on the visible slide. Strip end-anchored occurrences
# only — intra-text citations like "如 (參 [5]) 所述" are rare and harder
# to safely auto-strip, so we leave them.
_CITATION_RE = re.compile(
    r"\s*[\(（]\s*參(?:考)?\s*"
    r"[\[【]\s*\d+\s*[\]】]"                    # 第一個 [N]
    r"(?:\s*,\s*[\[【]\s*\d+\s*[\]】])*"         # Round CC: 後續可選的 , [M], [O]...
    r"\s*[\)）]\s*$",
)

# 2026-09-02 活體：模型實際寫的是裸的 `[8]`、`[5, 10]`、`【6】`，不是 prompt
# 教的「(參 [N])」，六成投影片帶著編號上台。結尾的整組數字括號一律移除；
# 句中的「如 [5] 所述」不動（那是文字的一部分，不是尾註）。
_BARE_CITATION_RE = re.compile(
    r"\s*[\[【]\s*\d+(?:\s*[,，、]\s*\d+)*\s*[\]】]\s*$",
)


def strip_inline_citations(text: str | None) -> str | None:
    """Remove RAG citation markers (e.g. '(參 [5])') from end of text.

    Up to 3 consecutive end-anchored citation tokens are stripped (some
    bullets cite multiple chunks: '...部署 (參 [5]) (參 [10])'). Idempotent.
    Safe on empty / None input.
    """
    if not text:
        return text
    text = str(text)
    for _ in range(6):
        new = _CITATION_RE.sub("", text).rstrip()
        new = _BARE_CITATION_RE.sub("", new).rstrip()
        if new == text:
            break
        text = new
    return text


def strip_latex(text: str | None) -> str | None:
    """Replace LaTeX math-mode strings with Unicode / plain equivalents.

    gemma4 occasionally emits ``$\\rightarrow$`` instead of ``→`` when
    reasoning about flows. The renderer prints LaTeX verbatim because
    pptxgenjs has no math support. Catches common commands + a generic
    fallback that strips bare dollar wrappers.

    Pure function. Safe on empty / None input.
    """
    if not text:
        return text
    text = str(text)
    # Round 4 Patch S: handle JSON-eaten control chars FIRST. Literal
    # string replacements because the broken characters are real
    # CR/TAB/LF in the data.
    for broken, fixed in _LATEX_BROKEN_CHAR_REPLACEMENTS.items():
        text = text.replace(broken, fixed)
    # Existing regex-based replacements (Round 3 Patch I).
    for pattern, replacement in _LATEX_REPLACEMENTS.items():
        text = re.sub(pattern, replacement, text)
    text = _GENERIC_LATEX_RE.sub(r"\1", text)
    return text


# 只收「在任何脈絡都不會是本義」的大陸技術用詞。有法律／行政本義的一律不收：
# 程序、項目、文件、質量、支持、應用、單位、水平、對象。
_TAIWAN_TERMS: tuple[tuple[str, str], ...] = (
    ("視頻", "影片"),
    ("軟件", "軟體"),
    ("硬件", "硬體"),
    ("網絡", "網路"),
    ("激光", "雷射"),
    ("信息", "資訊"),
    ("鼠標", "滑鼠"),
    ("屏幕", "螢幕"),
    ("分辨率", "解析度"),
    ("打印", "列印"),
    ("登錄", "登入"),
    ("默認", "預設"),
    ("內存", "記憶體"),
    ("服務器", "伺服器"),
    ("數據庫", "資料庫"),
    ("優化", "最佳化"),
    ("智能", "智慧"),
)


def apply_taiwan_terms(text: str) -> str:
    """只套用既有固定詞表，其餘字元保持原樣。"""
    for cn, tw in _TAIWAN_TERMS:
        if cn in text:
            text = text.replace(cn, tw)
    return text


def _convert(text: str | None, keep_citations: bool = False) -> str | None:
    """Apply citation/LaTeX cleanup and the existing repository Taiwan term table.

    Speaker notes keep citation markers; visible text drops trailing markers.
    None and empty input remain unchanged. Unlisted characters keep their script.
    """
    if text is None or text == "":
        return text
    if not keep_citations:
        text = strip_inline_citations(text) or ""
    text = strip_latex(text) or ""
    return apply_taiwan_terms(text)


def _normalize_slide(slide: Slide) -> Slide:
    """Return a NEW Slide with each configured string field cleaned with fixed terms.

    Pydantic models are immutable in spirit — we use `model_copy(update=...)`
    so callers can't accidentally observe a half-mutated state. Layout-
    specific payloads (stat / quote / columns / icon_rows) are recursed
    through; their own pydantic models get the same treatment.
    """
    patch: dict[str, Any] = {
        "title": _convert(slide.title),
        "bullets": [_convert(b) or "" for b in slide.bullets],
        # Speaker notes keep RAG citations for the audit trail
        # (the LLM populates these with chunk references); only visible slide
        # text gets citations stripped.
        "speaker_notes": _convert(slide.speaker_notes, keep_citations=True),
        "key_message": _convert(slide.key_message),
    }

    if slide.figure is not None:
        if slide.figure.items is not None:
            patch["figure"] = Figure(
                kind=slide.figure.kind,
                items=[
                    FigureItem(label=_convert(it.label) or "", note=_convert(it.note), parent=_convert(it.parent))
                    for it in slide.figure.items
                ],
                svg=slide.figure.svg,
            )
        else:
            patch["figure"] = slide.figure

    if slide.agenda is not None:
        patch["agenda"] = [_convert(a) or "" for a in slide.agenda]

    if slide.stat is not None:
        patch["stat"] = Stat(
            value=slide.stat.value,  # value is usually "47%" / "3.5×" — leave digits / units alone
            label=_convert(slide.stat.label) or "",
            supporting=_convert(slide.stat.supporting),
        )

    if slide.quote is not None:
        patch["quote"] = Quote(
            text=_convert(slide.quote.text) or "",
            attribution=_convert(slide.quote.attribution),
        )

    if slide.columns is not None:
        patch["columns"] = [
            Column(
                heading=_convert(c.heading) or "",
                bullets=[_convert(b) or "" for b in c.bullets],
            )
            for c in slide.columns
        ]

    if slide.icon_rows is not None:
        patch["icon_rows"] = [
            IconRow(
                # `concept` is a fixed-set semantic keyword (data_pipeline,
                # security, ...) — never localise it; that would break the
                # renderer's CONCEPT_MAP lookup.
                concept=ir.concept,
                heading=_convert(ir.heading) or "",
                description=_convert(ir.description) or "",
            )
            for ir in slide.icon_rows
        ]

    if slide.steps is not None:
        patch["steps"] = [
            Step(heading=_convert(st.heading) or "", description=_convert(st.description) or "")
            for st in slide.steps
        ]

    if slide.table is not None:
        patch["table"] = TableSpec(
            columns=[_convert(c) or "" for c in slide.table.columns],
            rows=[[_convert(cell) or "" for cell in row] for row in slide.table.rows],
        )

    if slide.sources is not None:
        # label 是檔名，不轉；note 是我們自己寫的，也不轉。
        patch["sources"] = list(slide.sources)

    return slide.model_copy(update=patch)


def normalize_spec(spec: SlidesSpec) -> SlidesSpec:
    """Normalise every user-visible string in a SlidesSpec.

    Returns a new SlidesSpec; the input is not mutated. Field-level
    decisions:
      - title (top + per-slide)         → convert
      - bullets                          → convert each
      - speaker_notes                    → convert
      - stat.value                       → DO NOT convert (numeric, units)
      - stat.label / supporting          → convert
      - quote.text / attribution         → convert
      - columns[].heading / bullets      → convert
      - icon_rows[].concept              → DO NOT convert (semantic keyword;
                                          must match renderer CONCEPT_MAP)
      - icon_rows[].heading / description→ convert
      - palette / layout_kind            → DO NOT convert (enum-like)

    Logged at info level so we can diff before/after for any title that
    actually changed; useful for telemetry on Gemma 4's CJK regressions
    after deploys.
    """
    converted_title = _convert(spec.title) or spec.title
    if converted_title != spec.title:
        logger.info(
            "Studio fixed-term normalised top-level title: %r → %r",
            spec.title, converted_title,
        )

    return spec.model_copy(
        update={
            "title": converted_title,
            "slides": [_normalize_slide(s) for s in spec.slides],
        }
    )
