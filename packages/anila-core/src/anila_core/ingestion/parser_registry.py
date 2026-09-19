"""Document parsers for the AgenticRAG pipeline.

Supports: .txt, .md, .pdf, .docx, .doc, .odt, .rtf, plus standalone
image files (.png, .jpg, .jpeg, .webp, .gif, .bmp).

Each parser implements the DocumentParser Protocol:

    parse(file_path: str) -> ParsedDocument

Parsers are synchronous and VLM-free. Images embedded inside a document
are extracted into ``ParsedDocument.images`` and a placeholder of the
form ``[[IMAGE:<id>]]`` is inserted at the corresponding position in
``content``. The IngestionService later asks a VisionProvider to caption
each image and rewrites the placeholders in-place.

ParserRegistry selects the correct parser by file extension.
"""

from __future__ import annotations

import logging
import mimetypes
import os
import re
import unicodedata
import uuid
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


@dataclass
class ImageRef:
    """An image extracted from (or equal to) a document."""

    image_id: str
    image_bytes: bytes
    mime: str = "image/png"
    page: int | None = None       # 1-based page number for PDFs, None otherwise
    alt_text: str = ""             # alt / title attribute if available
    caption: str = ""               # filled in by the VLM step


@dataclass
class ParsedDocument:
    """Result of parsing a document file.

    ``content`` may contain placeholders of the form ``[[IMAGE:<id>]]``
    where ``<id>`` is a key in ``images``. The IngestionService resolves
    these placeholders before chunking.
    """

    content: str
    metadata: dict = field(default_factory=dict)
    source_path: str = ""
    format: str = ""
    images: dict[str, ImageRef] = field(default_factory=dict)


@runtime_checkable
class DocumentParser(Protocol):
    """Protocol for document parsers."""

    def parse(self, file_path: str) -> ParsedDocument:
        """Parse a document file and return its text content with metadata."""
        ...


def _new_image_id() -> str:
    return f"img_{uuid.uuid4().hex[:10]}"


# ──────────────────────────────────────────────────────────────────────
# Plain text / markdown / rtf
# ──────────────────────────────────────────────────────────────────────

# Binary / UTF-16LE mis-decoded as UTF-8 yields NULs and U+FFFD. Strip NULs
# (Postgres text rejects U+0000) and refuse high replacement ratios so a
# renamed binary is not billed/injected as "ok" garbage.
_REPLACEMENT_CHAR = "\ufffd"
_MAX_REPLACEMENT_RATIO = 0.10  # >10% U+FFFD → not clean UTF-8
_MIN_RATIO_LENGTH = 200  # below this, do not hard-reject on ratio alone
# Raw NUL density gate: high NUL without a BOM is refused (no speculative
# UTF-16). BOM'd UTF-16 goes through the UTF-8 readability checks PLUS the
# ASCII-dominance contract below — strictly stricter, never looser.
_MAX_RAW_NUL_RATIO = 0.005  # 0.5%
_MAX_CONTROL_RATIO = 0.05  # reject C0-heavy clean UTF-8 / UTF-16 decodes
# Legacy 8-bit encodings (Big5/CP950/GBK/cp1252/latin-1) are deliberately
# NOT supported — owner decision 2026-08-01 (docs/OWNER-QUESTIONS.md Q24):
# Prefer a visible unsupported failure over wrong text.
#
# UTF-16 SUPPORT CONTRACT (owner ruling 2026-08-02, rounds 8-13):
#   UTF-16 is supported for ASCII-dominant documents. A UTF-16 body whose
#   decoded text is predominantly non-ASCII must be re-saved as UTF-8.
# This is STRICTER than the UTF-8 path, deliberately. Twelve rounds tried
# to separate "real CJK prose" from "binary decoded through a BOM into CJK
# scalars" and failed in one direction or the other every time, because the
# two are the same measurement. The contract removes the question instead of
# answering it: a binary read as UTF-16 yields predominantly non-ASCII
# scalars and is refused without any judgement about whether text is "real",
# and a CJK-dominant UTF-16 document is refused with an actionable sentence.
# UTF-8 is unaffected at every length and in every script.
# Do NOT re-add vocabulary-diversity / punctuation / Unicode-band / prose-
# structure predicates here; that family is the thirteen-round failure mode.
_UTF16_ENCODINGS = ("utf-16-le", "utf-16-be")
# Advice sentences. Each refusal must carry the advice its cause implies —
# a body that is not text at all must not be told to "re-save as UTF-8".
_NOT_TEXT_MESSAGE = "此檔案不是可讀的文字檔（偵測到大量無效位元組）。"
_UTF16_BOMLESS_MESSAGE = (
    "此檔案似乎是沒有 BOM 的 UTF-16 文字檔，目前僅支援 UTF-8"
    "（以及 ASCII 為主的 UTF-16）。請另存為 UTF-8 後再上傳。"
)
_UTF16_NON_ASCII_MESSAGE = (
    "此檔案可解讀為 UTF-16，但內容以非 ASCII 文字（例如中文）為主；"
    "UTF-16 僅支援以 ASCII 為主的檔案。請另存為 UTF-8 後再上傳。"
)
# Valid UTF-8 whose decoded scalars do not clear the readability / lane
# structure contract: a text reading plainly exists, so "this is not a text
# file" would be a lie. Name the limit instead (I3, round 16). This is the
# refusal that space-free two-byte-script runs and heavy combining-mark
# prose now receive, after the two round-15 exemptions were removed.
# ⚠ Owner-chosen wording (2026-08-02). The first draft said the platform
# supports "Traditional Chinese and English", which is NARROWER than what
# this code accepts — Japanese, Korean, Thai and spaced Cyrillic/Greek all
# still pass. Understating is still a claim that does not match behaviour;
# it just fails in the direction of people not bothering to try. Describe
# the SHAPES that fail rather than enumerating supported languages: any
# such list goes stale the moment a threshold moves, and is wrong again.
_UNSUPPORTED_TEXT_CONTENT_MESSAGE = (
    "此檔案可解讀為 UTF-8，但內容無法可靠解讀為文字。"
    "本平台支援中文、英文、日文、韓文等一般文件；"
    "少數文字系統，以及大量使用組合符號或完全不含空白的長段落，目前可能無法解讀。"
    "請確認檔案編碼，或改以中文／英文提供內容後再上傳。"
)
# C0 always exempt from _control_ratio. ESC / FF / VT are NOT blanket-
# exempt: lone ESC pads binaries and bills invisible tokens; FF/VT are
# rare in real listings (≪5%) so they count toward the ordinary cap.
# ANSI CSI/SGR (ESC [ ... final) keeps its leading ESC exempt — see
# _control_ratio. (Mutating back to frozenset("\n\r\t\f\v\x1b") reopens
# the pure-ESC / ESC-dilution hole.)
_TEXTUAL_C0 = frozenset("\n\r\t")
# ECMA-48 CSI: ESC [ + parameter/intermediate bytes + final byte @-~.
_CSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
# Positive text-likeness (decoded scalars): letters / digits / punct /
# CJK-kana-hangul / ASCII-graphic / textual whitespace.
_MIN_READABLE_RATIO = 0.85
_MIN_TEXT_LIKE_LENGTH = 50
# Parity-lane smuggle (F1): one lane carries printable text bytes while
# the other is binary/filler. After BOM-less UTF-16 acceptance was removed,
# a one-sided printable shape is never a legitimate encoding we accept.
_MIN_PARITY_PRINTABLE = 0.80
# Opposite-lane printable ceiling. ZIP_STORED headers are ~0.68 printable
# when used as a payload lane — must sit below this. Genuine UTF-8 keeps
# both lanes near 1.0; ELF/int32 payload lanes stay ≪ 0.50.
_MAX_PARITY_OTHER_PRINTABLE = 0.75
# F3 residual-C0: clustering + scale-aware density. ZIP headers arrive in
# bursts; a long listing may have many uniformly-scattered stray bytes.
_MAX_RESIDUAL_CONTROL_DENSITY = 0.002  # 0.2% — 100 strays in 96 KB still pass
_RESIDUAL_CLUSTER_GAP = 64
_RESIDUAL_CLUSTER_FRACTION = 0.50


def _strip_nuls(text: str) -> str:
    return text.replace("\x00", "")


def _replacement_ratio(text: str) -> float:
    if not text:
        return 0.0
    return text.count(_REPLACEMENT_CHAR) / len(text)


def _is_readable_char(ch: str) -> bool:
    """True for scalars that RENDER as readable text (category-level).

    Visible / countable as text evidence:
      - ASCII graphic + textual C0 whitespace (``\\n\\r\\t\\f\\v``)
      - ``L*`` / ``N*`` — letters and numbers (incl. CJK ``Lo``)
      - ``P*`` — punctuation
      - ``S*`` — symbols, including ``So`` (box drawing, ●★✓℃°)
      - ``Zs`` — space separators (NBSP, ideographic U+3000)

    Not readable (do not render as content glyphs / are format controls):
      - U+FFFD (replacement) — evidence of FAILED decoding, never of text
      - DEL, other ``Cc`` / ``C1``, ``Cf`` (ZWSP, IAA…), ``Cs``, ``Co``,
        ``Zl`` / ``Zp``. Private-use is excluded explicitly.
      - ``M*`` combining marks. A mark has no glyph of its own; it renders
        on whatever base happens to precede it, so its presence is not by
        itself evidence that the body is text. Decided here, per
        character, exactly like every other category — ``_readable_ratio``
        adds no positional rule on top (round 16).
    Justification is the Unicode general category of the scalar — not a
    code-point allow-list — so new box-drawing / checklist glyphs stay
    visible without per-character patches.
    """
    if ch == _REPLACEMENT_CHAR:
        return False
    o = ord(ch)
    if ch in "\n\r\t\f\v":
        return True
    if 0x20 <= o <= 0x7E:
        return True
    if 0x7F <= o <= 0x9F:  # DEL + C1
        return False
    cat = unicodedata.category(ch)
    if cat == "Cs" or cat == "Co" or 0xE000 <= o <= 0xF8FF:
        return False
    if cat[0] in "LN" or cat.startswith("P") or cat.startswith("S") or cat == "Zs":
        return True
    return False


def _readable_ratio(text: str) -> float:
    """Fraction of scalars that render as a glyph of their own.

    THE RULE (round 16): every scalar is decided by ``_is_readable_char``
    and by nothing else. A combining mark never counts, wherever it sits —
    no "behind a readable base" case, no look-back. Round 15's positional
    rule let one readable scalar pull an arbitrarily long run of marks to
    1.0; owner ruling 2026-08-02 was to remove it, not narrow it.

    Cost, stated: prose that is 18-41% marks (Thai, Devanagari, Hebrew
    with niqqud, Arabic with harakat, NFD Vietnamese) falls under the 0.85
    floor and is refused — by name of the limit, see
    ``_UNSUPPORTED_TEXT_CONTENT_MESSAGE``. Counting marks unconditionally
    instead is worse: an int32 array read as UTF-16 is 16% unattached
    marks and rises 0.68 → 0.86, earning a binary "re-save as UTF-8".
    """
    if not text:
        return 0.0
    return sum(1 for ch in text if _is_readable_char(ch)) / len(text)


def _ascii_textual_ratio(text: str) -> float:
    """Fraction of ASCII graphic + textual whitespace (word-structure proxy)."""
    if not text:
        return 0.0
    return sum(
        1 for c in text if 0x20 <= ord(c) <= 0x7E or c in "\n\r\t\f\v"
    ) / len(text)


def _is_ascii_dominant(text: str) -> bool:
    """The UTF-16 support contract: most decoded scalars are ASCII.

    "Dominant" is a strict majority — the definition of the word, not a
    tuned threshold. There is nothing to calibrate here and nothing about
    the *content* of the non-ASCII part is inspected: which scripts appear,
    how varied they are, and whether they read as prose are all irrelevant.

    Why this is the whole UTF-16 content gate: reading arbitrary bytes as
    UTF-16 maps almost every 16-bit unit outside ASCII (only 96 of 65 536
    units are ASCII), so a binary is refused as a matter of arithmetic. The
    only bodies that clear a strict ASCII majority are bodies that really do
    carry ASCII text in UTF-16's NUL-padded lane layout.
    """
    if not text:
        return False
    return sum(1 for c in text if ord(c) < 0x80) * 2 > len(text)


def _is_text_like(text: str) -> bool:
    """Positive text-likeness of decoded scalars — closed under filler sub.

    Any single-byte (or short repeating) filler interleaved into a binary
    yields decoded scalars that are either non-readable, mono-symbol,
    or residual non-textual C0 from container headers. Legitimate UTF-8 /
    UTF-16 text passes without reference to a pad-byte denylist.

    Length must not open a hole: readable_ratio == 0 rejects at any
    length (I-C). The ≥85% / residual-clustering floors apply to longer
    bodies. Parity-lane filler weaves are refused by
    ``_parity_lanes_show_filler_smuggle`` on the UTF-8 path; BOM'd UTF-16
    additionally requires the readable-ratio floor at every length and
    ``_is_ascii_dominant``.
    """
    if not text:
        return False
    n = len(text)
    rr = _readable_ratio(text)
    if rr == 0:
        return False
    if n >= _MIN_TEXT_LIKE_LENGTH:
        if rr < _MIN_READABLE_RATIO:
            return False
        # Residual non-textual C0: clustering + density (I-4'), not a
        # fixed absolute count shared by 1 KB and 50 MB files.
        if _soft_residual_controls_reject(text):
            return False
    # Mono non-letter/non-digit (───, ●●●, NBSP×N) is pad, not prose.
    # Mono letters/digits (中×N, a×N) stay allowed — boring but real text.
    body = [c for c in text if c not in " \t\n\r\f\v"]
    if len(body) >= 8:
        ch, cnt = Counter(body).most_common(1)[0]
        if cnt / len(body) >= 0.98:
            cat = unicodedata.category(ch)
            if cat[0] not in "LN" and not (0x20 <= ord(ch) <= 0x7E):
                return False
    # NOTE (round 15): a "short cycle spanning Hangul+Cyrillic/other" clause
    # lived here and was deleted. Measured with and without it over 8 712
    # binary × filler × ratio × BOM cases (13 payloads incl. ELF/ZIP/PNG/
    # int32/uint16-band/urandom, 21 filler alphabets incl. SPACE and NUL,
    # six unequal ratios, both interleave orders): the accepted set was
    # byte-identical. The parity, control and NUL gates carry all of the
    # binary refusal; this clause carried none of it, while being the sole
    # cause of refusing bullet lists, arrow maps and │digit│ tables. It also
    # judged decoded scalars by script, which is the forbidden family.
    return True


def _control_ratio(text: str) -> float:
    """C0 density with CSI-aware ESC exemption.

    Newlines/tabs never count. ESC that opens a CSI/SGR sequence does
    not count (ANSI-coloured logs). Lone ESC, form-feed, vertical-tab,
    and other C0 bytes do — so a 100% ESC blob or ESC-diluted binary
    cannot measure control_ratio == 0.
    """
    if not text:
        return 0.0
    csi_esc = {m.start() for m in _CSI_ESCAPE_RE.finditer(text)}
    controls = 0
    for i, c in enumerate(text):
        o = ord(c)
        if o >= 32 or c in _TEXTUAL_C0:
            continue
        if c == "\x1b" and i in csi_esc:
            continue
        controls += 1
    return controls / len(text)


def _soft_residual_control_indices(text: str) -> list[int]:
    """Indices of residual non-textual C0 (FF/VT/CSI-ESC exempt).

    Form-feed / vertical-tab are ignored so a Fortran page-break plus one
    stray ``\\xff`` can still soft-accept. Lone ESC continues to count
    (must not reopen the pure-ESC hole via the soft floor).
    """
    csi_esc = {m.start() for m in _CSI_ESCAPE_RE.finditer(text)}
    out: list[int] = []
    for i, c in enumerate(text):
        o = ord(c)
        if o >= 32 or c in _TEXTUAL_C0 or c in "\f\v":
            continue
        if c == "\x1b" and i in csi_esc:
            continue
        out.append(i)
    return out


def _soft_residual_controls_reject(text: str) -> bool:
    """I-4': reject clustered or dense residual C0, allow scattered strays.

    Distinguishes a Fortran listing with uniformly-scattered stray bytes
    (gaps ≫ header size) from NUL-interleaved ZIP / container headers
    whose residual C0 arrives in bursts at record boundaries. Density
    scales with length so 100 strays in 10 MB pass while the same count
    in 1 KB does not.
    """
    idxs = _soft_residual_control_indices(text)
    if not idxs:
        return False
    n = len(text)
    if len(idxs) / n > _MAX_RESIDUAL_CONTROL_DENSITY:
        return True
    if len(idxs) < 3:
        return False
    gaps = [idxs[i + 1] - idxs[i] for i in range(len(idxs) - 1)]
    clustered = sum(1 for g in gaps if g <= _RESIDUAL_CLUSTER_GAP)
    return clustered / len(gaps) >= _RESIDUAL_CLUSTER_FRACTION



def _utf16_readable_text(raw: bytes, encoding: str) -> str | None:
    """Strict UTF-16 decode + NUL strip; None if undecodable or not readable.

    The readable-ratio floor applies at every length: a BOM'd binary slice
    (e.g. ``/bin/ls`` at FE FF) can decode to a handful of private-use
    scalars that would otherwise ride the short-text soft floor. Every
    check here is per-character renderability or C0 density — none of them
    asks whether readable text is "real".

    ASCII dominance is deliberately NOT applied here: this function answers
    "is there a text reading at all", which is what the refusal *message*
    needs. Acceptance additionally requires ``_is_ascii_dominant``.
    """
    try:
        text = _strip_nuls(raw.decode(encoding))
    except UnicodeDecodeError:
        return None
    if not text or _REPLACEMENT_CHAR in text:
        return None
    if _control_ratio(text) >= _MAX_CONTROL_RATIO:
        return None
    if not _is_text_like(text):
        return None
    if _readable_ratio(text) < _MIN_READABLE_RATIO:
        return None
    return text


def _lane_printable_ratio(lane: bytes) -> float:
    """Fraction of bytes that can carry visible text under any alphabet.

    ASCII graphic + textual C0 + any high byte (``≥ 0x80``). Closed under
    alphabet substitution across ASCII ∪ Latin-1 ∪ CJK.
    """
    if not lane:
        return 0.0
    return sum(
        1 for x in lane if x in (9, 10, 11, 12, 13) or 32 <= x <= 126 or x >= 0x80
    ) / len(lane)


def _parity_lanes_show_filler_smuggle(raw: bytes) -> bool:
    """I-1': one parity lane carries text; the opposite lane is non-text.

    Decision is carrier-vs-other. Ordinary UTF-8 keeps similar printable
    ratios on both lanes (~1.0 / ~1.0). Binary⊗filler weaves are one-sided.
    """
    if len(raw) < 64:
        return False
    even, odd = raw[0::2], raw[1::2]
    if not even or not odd:
        return False
    pe, po = _lane_printable_ratio(even), _lane_printable_ratio(odd)
    if pe >= _MIN_PARITY_PRINTABLE and po < _MAX_PARITY_OTHER_PRINTABLE:
        return True
    if po >= _MIN_PARITY_PRINTABLE and pe < _MAX_PARITY_OTHER_PRINTABLE:
        return True
    # Tiny filler alphabet on one lane (≤32 distinct) opposite a rich lane.
    # No exemption (round 16): round 15 exempted "valid UTF-8, no sequence
    # wider than two bytes" so space-free Cyrillic/Greek/Hebrew/Arabic runs
    # would accept; review showed it also admits machine-generated byte
    # patterns, and the owner ruled removal over narrowing. Cost, stated: a
    # whitespace-free run of two-byte-script letters is refused from 64
    # bytes up — with the limit named, not as "not a text file". Nothing
    # here decodes: the decision is distinct byte values per parity lane.
    ue, uo = len(set(even)), len(set(odd))
    if pe >= 0.50 and po >= 0.50:
        if ue <= 32 and uo >= max(24, ue * 3):
            return True
        if uo <= 32 and ue >= max(24, uo * 3):
            return True
    # ZIP local/central/end headers survive on one parity lane after weave.
    if pe >= _MIN_PARITY_PRINTABLE and (
        b"PK\x03\x04" in odd or b"PK\x05\x06" in odd or b"PK\x01\x02" in odd
    ):
        return True
    if po >= _MIN_PARITY_PRINTABLE and (
        b"PK\x03\x04" in even or b"PK\x05\x06" in even or b"PK\x01\x02" in even
    ):
        return True
    return False


def _utf16_reading(raw: bytes) -> str | None:
    """The readable UTF-16 reading of ``raw``, or None if there is none.

    Endianness comes from the BOM when present; otherwise both lanes are
    tried. This is a *messaging* probe as well as the acceptance decode —
    the caller decides what to do with the reading. A body with no reading
    at all is not text and must never be told to "re-save as UTF-8".
    """
    if len(raw) < 2 or len(raw) % 2:
        return None
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return _utf16_readable_text(raw, "utf-16")
    for enc in _UTF16_ENCODINGS:
        text = _utf16_readable_text(raw, enc)
        if text is not None:
            return text
    return None


def _unsupported_user_message(raw: bytes) -> str:
    """The refusal sentence whose advice matches the actual cause.

    Causes, in the order they are decided:
      1. no readable reading in any supported encoding → the body is not
         text; say exactly that (never "re-save as UTF-8", which is
         useless advice for an ELF or a PNG);
      2. a readable UTF-16 reading that is ASCII-dominant but has no BOM →
         it is a UTF-16 text file we cannot safely detect;
      3. a legacy 8-bit encoding decodes into readable text → name it;
      4. a readable but non-ASCII-dominant UTF-16 reading → the support
         contract, stated so the user can act on it.
    Only the sentence — acceptance is decided elsewhere. Nothing here asks
    whether readable text is "real"; cause 4 fires on any non-ASCII-dominant
    reading, mojibake or prose alike, and 8-bit naming (3) wins over it so a
    Big5 file keeps its precise diagnosis.
    """
    reading = _utf16_reading(raw)
    if reading is not None and _is_ascii_dominant(reading):
        # BOM + ASCII-dominant would have been accepted, so this is BOM-less.
        return _UTF16_BOMLESS_MESSAGE
    # The UTF-16 probe is exact at any length; the 8-bit sniff below is a
    # guess, and guessing an encoding from a dozen bytes is noise.
    if len(raw) < 16:
        return _UTF16_NON_ASCII_MESSAGE if reading is not None else _NOT_TEXT_MESSAGE
    utf8_view = raw.decode("utf-8", errors="replace")
    utf8_repl = (
        utf8_view.count(_REPLACEMENT_CHAR) / len(utf8_view) if utf8_view else 1.0
    )
    high_bytes = sum(1 for x in raw if x >= 0x80) / len(raw)
    # GBK before Big5: shared byte sequences also "decode" as Big5 into
    # 假傳統. A BOM is the file declaring its own encoding, so it outranks
    # an 8-bit guess; without one, naming the 8-bit encoding is the more
    # precise diagnosis (both end in the same "re-save as UTF-8" action).
    if not raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        for enc, label in (("gbk", "GBK"), ("big5", "Big5"), ("cp950", "CP950")):
            try:
                text = raw.decode(enc)
            except UnicodeDecodeError:
                continue
            if _readable_ratio(text) >= 0.50 and high_bytes >= 0.10:
                return (
                    f"此檔案似乎是 {label} 編碼的文字檔，目前僅支援 UTF-8／UTF-16。"
                    f"請另存為 UTF-8 後再上傳。"
                )
    if reading is not None:
        return _UTF16_NON_ASCII_MESSAGE
    if utf8_repl >= 0.05 and high_bytes >= 0.05:
        for enc, label in (("cp1252", "Windows-1252"), ("latin-1", "Latin-1")):
            try:
                text = raw.decode(enc)
            except UnicodeDecodeError:
                continue
            if _readable_ratio(text) >= 0.80:
                return (
                    f"此檔案似乎是 {label} 編碼的文字檔，目前僅支援 UTF-8／UTF-16。"
                    f"請另存為 UTF-8 後再上傳。"
                )
    return _NOT_TEXT_MESSAGE


def _clean_utf8_refusal_message(text: str, control_ratio: float) -> str:
    """Refusal sentence for a body that IS valid UTF-8 but fails a gate.

    Two causes (I3): nothing renders at all / dense C0 / container-header
    C0 bursts → it is not text, say so. Anything else → the scalars do
    render and the platform simply does not accept this content
    (space-free two-byte-script runs, mark-heavy prose, filler weaves);
    "not a readable text file" would be false, so name the limit.
    Messaging only — the caller already decided. No script inspection.
    """
    plausible_text = (
        _readable_ratio(text) > 0
        and control_ratio < _MAX_CONTROL_RATIO
        and not _soft_residual_controls_reject(text)
    )
    return _UNSUPPORTED_TEXT_CONTENT_MESSAGE if plausible_text else _NOT_TEXT_MESSAGE


def _decode_text_bytes(raw: bytes) -> str:
    """Decode text bytes: UTF-8, or BOM'd ASCII-dominant UTF-16.

    BOM-less UTF-16 is never speculated into acceptance. A body that
    decodes cleanly as UTF-8 with zero U+FFFD is never re-decoded as
    anything else. UTF-16 additionally requires ASCII dominance — see the
    contract note at the top of this module.

    Raises ``ParseError`` with ``E_PARSE_FORMAT_UNSUPPORTED`` when every
    candidate fails (binary-as-text / legacy 8-bit / undecodable).
    """
    from anila_core.ingestion.errors import ParseError

    if not raw:
        return ""

    # Keep NULs in the UTF-8 view so control_ratio cannot be gamed by
    # stripping first (ASCII UTF-16LE is valid UTF-8 + dense NULs).
    decoded_utf8 = raw.decode("utf-8", errors="replace")
    utf8_text = _strip_nuls(decoded_utf8)
    nul_ratio = raw.count(0) / len(raw)
    if _REPLACEMENT_CHAR not in decoded_utf8:
        ctrl_with_nul = _control_ratio(decoded_utf8)
        if (
            ctrl_with_nul < _MAX_CONTROL_RATIO
            and _is_text_like(utf8_text)
            and not _parity_lanes_show_filler_smuggle(raw)
        ):
            # T1 invariant: clean UTF-8 is never re-decoded as UTF-16.
            return utf8_text
        # High C0 density or non-text scalars. Low NUL → refuse here;
        # high NUL falls through to the BOM-less UTF-16 refusal below.
        if nul_ratio <= _MAX_RAW_NUL_RATIO:
            raise ParseError.format_unsupported(
                user_message=_clean_utf8_refusal_message(
                    utf8_text, ctrl_with_nul,
                ),
                details={
                    "replacement_ratio": 0.0,
                    "threshold": _MAX_REPLACEMENT_RATIO,
                    "nul_ratio": round(nul_ratio, 4),
                    "control_ratio": round(ctrl_with_nul, 4),
                    "readable_ratio": round(_readable_ratio(utf8_text), 4),
                },
            )

    # BOM → endianness only. The decoded text must be readable AND
    # ASCII-dominant (the support contract); a BOM cannot buy acceptance
    # for content that would be refused without it.
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        text = _utf16_readable_text(raw, "utf-16")
        if text is not None and _is_ascii_dominant(text):
            return text
        raise ParseError.format_unsupported(
            user_message=_unsupported_user_message(raw),
            details={
                "replacement_ratio": round(_replacement_ratio(utf8_text), 4),
                "threshold": _MAX_REPLACEMENT_RATIO,
                "nul_ratio": round(nul_ratio, 4),
                "hint": (
                    "utf16_bom_not_ascii_dominant"
                    if text is not None
                    else "utf16_bom_unreadable"
                ),
            },
        )

    # High NUL density without a BOM: looks like UTF-16LE/BE ASCII padding.
    # Refuse — do not speculate. (Clean UTF-8 with zero U+FFFD already
    # returned above; reaching here means the UTF-8 view failed gates.)
    # The message must still separate "UTF-16 without a BOM" from "not
    # text at all": NUL density alone cannot tell an ELF from a .ps1.
    if nul_ratio > _MAX_RAW_NUL_RATIO:
        raise ParseError.format_unsupported(
            user_message=_unsupported_user_message(raw),
            details={
                "replacement_ratio": round(_replacement_ratio(utf8_text), 4),
                "threshold": _MAX_REPLACEMENT_RATIO,
                "nul_ratio": round(nul_ratio, 4),
                "hint": "high_nul_no_bom",
            },
        )

    # Short files: best-effort for mostly-readable text with a bad byte
    # (M2: ``a,b,c\\xff\\n``). Require substantial ASCII/textual structure
    # so urandom-with-some-ASCII cannot pass on a lone readable scalar;
    # allow a slightly higher replacement budget than the long soft floor
    # (one U+FFFD in a 7-char line is ~14%).
    if (
        utf8_text
        and len(utf8_text) < _MIN_RATIO_LENGTH
        and _ascii_textual_ratio(utf8_text) >= 0.65
        and _readable_ratio(utf8_text) >= _MIN_READABLE_RATIO
        and _replacement_ratio(utf8_text) <= 0.25
        and not _parity_lanes_show_filler_smuggle(raw)
    ):
        return utf8_text

    # Long files with only a few U+FFFD and residual C0 that F3 would
    # still accept (scattered, under density) — e.g. ASCII source with
    # legacy-8bit comments, or a Fortran listing with page-break FF plus
    # one stray ``\\xff``. ZIP headers keep clustered C0 and still fail
    # ``_soft_residual_controls_reject``.
    if (
        utf8_text
        and _replacement_ratio(utf8_text) <= _MAX_REPLACEMENT_RATIO
        and not _soft_residual_controls_reject(utf8_text)
        and not _parity_lanes_show_filler_smuggle(raw)
        and _is_text_like(utf8_text)
    ):
        return utf8_text

    # BOM-less UTF-16 prose (nul≈0, high U+FFFD under UTF-8), legacy 8-bit
    # encodings, and plain binary: refuse with the message its cause implies.
    raise ParseError.format_unsupported(
        user_message=_unsupported_user_message(raw),
        details={
            "replacement_ratio": round(_replacement_ratio(utf8_text), 4),
            "threshold": _MAX_REPLACEMENT_RATIO,
            "nul_ratio": round(nul_ratio, 4),
            "hint": "no_supported_encoding",
        },
    )



class PlainTextParser:
    """Parser for .txt and other utf-8 text-class files."""

    def parse(self, file_path: str) -> ParsedDocument:
        path = Path(file_path)
        content = _decode_text_bytes(path.read_bytes())
        return ParsedDocument(
            content=content,
            metadata={"title": path.stem},
            source_path=file_path,
            format="txt",
        )


class MarkdownParser:
    """Parser for .md files. Preserves heading structure in metadata."""

    def parse(self, file_path: str) -> ParsedDocument:
        path = Path(file_path)
        content = _decode_text_bytes(path.read_bytes())

        title = path.stem
        for line in content.splitlines():
            if line.startswith("# "):
                title = line[2:].strip()
                break

        return ParsedDocument(
            content=content,
            metadata={"title": title},
            source_path=file_path,
            format="md",
        )


class JsonParser:
    """Parser for .json files — pretty-print so keys/values stay searchable."""

    def parse(self, file_path: str) -> ParsedDocument:
        import json

        path = Path(file_path)
        raw = _decode_text_bytes(path.read_bytes())
        try:
            content = json.dumps(json.loads(raw), ensure_ascii=False, indent=2)
        except (ValueError, TypeError):
            content = raw  # 非合法 JSON → 當純文字收
        return ParsedDocument(
            content=content,
            metadata={"title": path.stem},
            source_path=file_path,
            format="json",
        )


class HtmlParser:
    """Parser for .html/.htm — stdlib tag-strip → text(不依賴 docling)。

    保留 <title> 當標題、丟掉 <script>/<style>、收斂空白。版面感知的 HTML
    (表格/圖片)需要 docling(``DOC_PARSER=docling``);這是永遠可用的原生
    fallback,讓 .html 上傳在預設 native 模式下也能解析。
    """

    def parse(self, file_path: str) -> ParsedDocument:
        import re
        from html.parser import HTMLParser as _HTMLParser

        # 跳過「不可見/非內容」子樹 —— 不只 script/style,還有 template、noscript、
        # head,以及帶 hidden / aria-hidden / display:none 的元素。否則 HTML 裡藏的
        # 不可見文字會被抽進 RAG context → prompt injection / knowledge poisoning。
        SKIP_TAGS = frozenset({"script", "style", "template", "noscript", "head"})
        VOID = frozenset({
            "br", "img", "input", "hr", "meta", "link", "area", "base",
            "col", "embed", "source", "track", "wbr",
        })
        BLOCK = frozenset({"p", "br", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5"})

        class _Extract(_HTMLParser):
            def __init__(self) -> None:
                super().__init__(convert_charrefs=True)
                self.parts: list[str] = []
                self.title = ""
                self._skip: list[str] = []  # 被跳過子樹的標籤堆疊
                self._in_title = False

            def handle_starttag(self, tag, attrs):
                if tag == "title":
                    self._in_title = True
                    return
                attrd = {k.lower(): (v or "") for k, v in attrs}
                hidden = (
                    "hidden" in attrd
                    or attrd.get("aria-hidden") == "true"
                    or "display:none" in attrd.get("style", "").replace(" ", "").lower()
                )
                if tag in SKIP_TAGS or hidden:
                    if tag not in VOID:  # void 元素無對應 end tag,不入堆疊
                        self._skip.append(tag)
                    return
                if self._skip:
                    return
                if tag in BLOCK:
                    self.parts.append("\n")

            def handle_endtag(self, tag):
                if tag == "title":
                    self._in_title = False
                elif self._skip and self._skip[-1] == tag:
                    self._skip.pop()

            def handle_data(self, data):
                if self._in_title:
                    self.title += data  # title 即使在被跳過的 <head> 內也要抓
                    return
                if self._skip:
                    return
                self.parts.append(data)

        path = Path(file_path)
        raw = _decode_text_bytes(path.read_bytes())
        ex = _Extract()
        ex.feed(raw)
        text = re.sub(r"[ \t]+", " ", "".join(ex.parts))
        text = re.sub(r"\n[ \t]*\n[ \t]*", "\n\n", text).strip()
        return ParsedDocument(
            content=text,
            metadata={"title": ex.title.strip() or path.stem},
            source_path=file_path,
            format="html",
        )


class RtfParser:
    """Parser for .rtf files using striprtf (pure Python, no system deps)."""

    def parse(self, file_path: str) -> ParsedDocument:
        try:
            from striprtf.striprtf import rtf_to_text  # type: ignore[import]
        except ImportError as exc:
            raise ImportError(
                "striprtf is required for RTF parsing. "
                "Install with: pip install 'agentic-rag[rag]'"
            ) from exc

        path = Path(file_path)
        raw = _decode_text_bytes(path.read_bytes())
        content = rtf_to_text(raw, errors="ignore")

        return ParsedDocument(
            content=content,
            metadata={"title": path.stem},
            source_path=file_path,
            format="rtf",
        )


# ──────────────────────────────────────────────────────────────────────
# PDF (with embedded image extraction)
# ──────────────────────────────────────────────────────────────────────

class PdfParser:
    """Parser for .pdf files.

    Text is extracted with pymupdf4llm (PDF → Markdown). Embedded images
    are extracted with pymupdf (fitz) and appended at the end of each
    page's markdown as ``[[IMAGE:<id>]]`` placeholders so the
    HierarchicalChunker can attach them under the surrounding heading.

    When digital extraction yields little or mostly ``<?>`` placeholders
    (font-subsetted PDFs), an optional OCR backend is invoked. The
    backend is constructed lazily on first need from environment
    variables — see ``ingestion.ocr.build_ocr_backend_from_env``.

    ⚠ The ``[[IMAGE:<id>]]`` tokens this parser inserts are **not** text.
    ``needs_ocr_fallback`` strips them before measuring; do not hand it a
    string whose placeholders have already been rewritten into captions,
    or a captioned scan will read as a text document.

    ⚠ Taking the OCR result **replaces** the native extraction, which
    throws several things away at once. That policy is not decided here,
    but every loss it causes is measured into ``metadata['ocr_losses']``
    and logged — see ``_describe_ocr_losses``.
    """

    _ocr_backend = None  # type: ignore[var-annotated]
    _ocr_initialised = False

    @classmethod
    def _get_ocr_backend(cls):
        if not cls._ocr_initialised:
            from .ocr import build_ocr_backend_from_env
            cls._ocr_backend = build_ocr_backend_from_env()
            cls._ocr_initialised = True
        return cls._ocr_backend

    def parse(self, file_path: str) -> ParsedDocument:
        try:
            import pymupdf4llm  # type: ignore[import]
            import fitz  # type: ignore[import]  # pymupdf
        except ImportError as exc:
            raise ImportError(
                "pymupdf4llm and pymupdf are required for PDF parsing. "
                "Install with: pip install 'agentic-rag[rag]'"
            ) from exc

        path = Path(file_path)

        # Per-page markdown lets us interleave image placeholders with text.
        page_chunks = pymupdf4llm.to_markdown(file_path, page_chunks=True)

        images: dict[str, ImageRef] = {}
        doc = fitz.open(file_path)
        try:
            # Everything below numbers pages by the extractor's own output:
            # ``page_chunks`` decides the count and ``doc[pno - 1]`` trusts
            # the index to line up with the physical document. Both the
            # ``has_page_boundaries`` check and the chunker's guard compare
            # the extractor to *itself*, so an extractor that silently drops
            # pages is self-consistent and invisible to them — a real 5-page
            # PDF read as 3 would tell the reader 「第 3 頁／共 3 頁」 and
            # every mechanism downstream would agree. ``doc`` is already open,
            # so the physical count is free; this is the only comparison
            # against ground truth in the whole path.
            if len(page_chunks) != doc.page_count:
                logger.warning(
                    "PDF %s: text extractor returned %d page(s) but the "
                    "document has %d — page numbers derived from this "
                    "extraction will be wrong",
                    path.name, len(page_chunks), doc.page_count,
                )

            parts: list[str] = []
            # Kept separately from ``parts``: this is the per-page extraction
            # before any placeholder is appended, which is what the OCR
            # trigger must measure. Deriving it back out of ``content`` by
            # splitting on ``\f`` does not work — see the note at the join.
            page_texts: list[str] = []
            for pno, page_entry in enumerate(page_chunks, start=1):
                page_md = (
                    page_entry.get("text", "")
                    if isinstance(page_entry, dict)
                    else str(page_entry)
                )
                # ``\f`` is the page delimiter (see the join below), so it
                # must not survive *inside* a page. PDF text streams can
                # legitimately carry U+000C; leaving it in splits one real
                # page into two and shifts every later citation by one.
                page_md = page_md.replace("\f", "\n")
                # Sanitised, pre-placeholder text — what the OCR trigger
                # measures. ``\f``→``\n`` is whitespace either way, so the
                # substitution does not move that decision.
                page_texts.append(page_md)
                # One element per page — image placeholders are appended to
                # this same string, never as separate ``parts`` entries.
                page_parts: list[str] = [page_md.rstrip()]

                page = doc[pno - 1]
                for img_info in page.get_images(full=True):
                    xref = img_info[0]
                    try:
                        image_bytes, mime = _extract_pdf_image(doc, xref)
                    except Exception as exc:
                        logger.warning(
                            "Skip image xref=%s on page %d: %s", xref, pno, exc
                        )
                        continue

                    img_id = _new_image_id()
                    images[img_id] = ImageRef(
                        image_id=img_id,
                        image_bytes=image_bytes,
                        mime=mime,
                        page=pno,
                    )
                    page_parts.append(f"\n[[IMAGE:{img_id}]]\n")

                parts.append("".join(page_parts))
        finally:
            doc.close()

        # Page join uses ``\f\n`` (form-feed) so consumers that need
        # per-page boundaries (e.g. the central ingestion-worker's
        # ``pdf-page`` chunker) can split on the marker without
        # re-parsing the PDF. Markdown-style consumers ignore it as
        # whitespace.
        #
        # ⚠ INVARIANT — of the join expression on the next line, and only
        # from there until the OCR fallback below:
        # ``"\f\n".join(parts)`` contains exactly ``len(page_chunks) - 1``
        # form feeds, and its Nth ``\f``-separated field is real PDF page N.
        # Three things enforce it and all three are load-bearing — reverting
        # any one alone turns a test in test_pdf_page_markers.py red:
        #   * ``parts`` holds exactly one entry per page — image
        #     placeholders go *inside* their page's entry. Appending them
        #     as separate parts made an N-page PDF with M images per page
        #     report N×(1+M) pages, so on a 10-page/3-image spec real
        #     page 10 was cited to the user as "page 37 of 40".
        #   * empty pages are NOT filtered out. A blank page is still a
        #     page; dropping it renumbers every page after it.
        #   * in-page ``\f`` is rewritten to ``\n`` above (:896-900). A form
        #     feed carried by the PDF's own text stream is indistinguishable
        #     from one we inserted, and splits a real page in two.
        #
        # ⚠ It is NOT an invariant of what this function RETURNS. The OCR
        # fallback below reassigns ``content`` wholesale to backend output
        # that carries no page structure of its own, so an OCR'd document
        # typically returns zero ``\f`` while ``metadata["pages"]`` still
        # says N — and a backend whose text happens to contain ``\f`` would
        # return a count that is wrong rather than absent. ``ocr_used``
        # distinguishes the two cases. Nothing downstream may infer page
        # structure from ``metadata["pages"]`` alone: ``extract_text``
        # re-derives ``has_page_boundaries`` by counting the fields and
        # comparing, and the ``pdf-page`` chunker warns when they disagree.
        content = "\f\n".join(parts)
        # Snapshot before the OCR fallback may replace ``content`` wholesale;
        # the loss report below diffs the two to say what OCR destroyed.
        native_content = content
        ocr_used = False
        ocr_losses: dict[str, Any] | None = None

        # Optional OCR fallback for scanned / font-subsetted PDFs.
        backend = self._get_ocr_backend()
        if backend is not None:
            from .ocr import needs_ocr_fallback
            if needs_ocr_fallback(content, page_texts=page_texts):
                logger.info(
                    "PDF %s text extraction looks unusable — running OCR fallback",
                    path.name,
                )
                try:
                    ocr_text = backend.extract(file_path)
                    if ocr_text.strip():
                        content = ocr_text
                        ocr_used = True
                        ocr_losses = _describe_ocr_losses(
                            native_content,
                            content,
                            page_count=len(page_chunks),
                            backend_max_pages=getattr(backend, "max_pages", None),
                        )
                        _log_ocr_losses(path.name, ocr_losses, len(page_chunks))
                except Exception as exc:
                    logger.warning(
                        "OCR fallback failed for %s: %s — keeping native extraction",
                        path.name, exc,
                    )

        metadata: dict[str, Any] = {
            "title": path.stem,
            "pages": len(page_chunks),
            "embedded_images": len(images),
            "ocr_used": ocr_used,
        }
        if ocr_losses is not None:
            # ``ocr_used: True`` on its own reads as unqualified success.
            # These two are added exactly when that claim is made, so no
            # consumer can render "OCR applied" without the losses sitting
            # in the same dict. Absent when no OCR ran — a document that
            # never went near the backend keeps byte-identical metadata.
            metadata["ocr_lossy"] = ocr_losses["lossy"]
            metadata["ocr_losses"] = ocr_losses

        return ParsedDocument(
            content=content,
            metadata=metadata,
            source_path=file_path,
            format="pdf",
            images=images,
        )


def _describe_ocr_losses(
    native: str,
    replacement: str,
    page_count: int,
    backend_max_pages: int | None,
) -> dict[str, Any]:
    """Report, in structured form, what taking the OCR result throws away.

    ``PdfParser`` currently *replaces* the native extraction with the OCR
    result. Whether that should instead be a merge, or a refusal, is an open
    policy question and deliberately not decided here — but every loss the
    current policy causes is measured and reported, because each one
    surfaces in a different subsystem and all of them look like success:

    * ``native_text_chars_dropped`` — pages the OCR never covered (the page
      cap) had their extracted text discarded with everything else.
    * ``page_boundaries_lost`` — the OCR result carries no ``\\f``, so the
      ``pdf-page`` chunker sees one page and "see page 4 of doc.pdf" breaks.
    * ``image_placeholders_dropped`` — ``ParsedDocument.images`` still holds
      the refs, but the anchors they were meant to be rewritten into are
      gone, so the captioning step captions into nothing.
    * ``pages_not_ocred`` — how many pages the backend's cap skipped.

    Every field is derived by comparing the two strings, so the report stays
    truthful if the replace/merge policy changes: under a merge, ``native``
    survives inside ``replacement`` and the counts fall to zero on their own.
    """
    from .ocr import strip_image_placeholders

    native_survives = native in replacement
    native_real_chars = len("".join(strip_image_placeholders(native).split()))
    pages_not_ocred = (
        max(0, page_count - backend_max_pages) if backend_max_pages else 0
    )

    losses: dict[str, Any] = {
        "native_text_chars_dropped": 0 if native_survives else native_real_chars,
        "page_boundaries_lost": "\f" in native and "\f" not in replacement,
        "image_placeholders_dropped": max(
            0, native.count("[[IMAGE:") - replacement.count("[[IMAGE:")
        ),
        "pages_not_ocred": pages_not_ocred,
    }
    losses["lossy"] = bool(
        losses["native_text_chars_dropped"]
        or losses["page_boundaries_lost"]
        or losses["image_placeholders_dropped"]
        or losses["pages_not_ocred"]
    )
    return losses


def _log_ocr_losses(name: str, losses: dict[str, Any], page_count: int) -> None:
    """Say out loud what the OCR result cost, one line per kind of loss."""
    if not losses["lossy"]:
        return
    if losses["pages_not_ocred"]:
        # The only loss that destroys text the user can read in the original.
        logger.error(
            "OCR of %s covered %d of its %d pages — the remaining %d were not "
            "OCR'd AND their extracted text was discarded by the replacement. "
            "The built-in OCR page cap must be large enough to cover the document "
            "AND small enough that the job fits the worker's job_timeout; when both "
            "cannot hold, do not OCR this document.",
            name,
            page_count - losses["pages_not_ocred"],
            page_count,
            losses["pages_not_ocred"],
        )
    if losses["native_text_chars_dropped"]:
        logger.warning(
            "OCR of %s replaced the native extraction: %d characters of "
            "natively extracted text discarded.",
            name, losses["native_text_chars_dropped"],
        )
    if losses["page_boundaries_lost"]:
        logger.warning(
            "OCR of %s dropped the \\f page markers — the pdf-page chunker "
            "will see one page and page-number citations will be wrong.",
            name,
        )
    if losses["image_placeholders_dropped"]:
        logger.warning(
            "OCR of %s dropped %d [[IMAGE:…]] anchor(s) while keeping the "
            "image refs — captions will have nowhere to be written back to.",
            name, losses["image_placeholders_dropped"],
        )


def _extract_pdf_image(doc, xref: int) -> tuple[bytes, str]:
    """Return (bytes, mime) for a PDF image xref, preferring the native format."""
    import fitz  # type: ignore[import]

    info = doc.extract_image(xref)
    if info and info.get("image"):
        ext = (info.get("ext") or "png").lower()
        mime = f"image/{'jpeg' if ext == 'jpg' else ext}"
        return info["image"], mime

    pix = fitz.Pixmap(doc, xref)
    try:
        if pix.n - pix.alpha >= 4:  # CMYK / DeviceN → convert to RGB
            pix = fitz.Pixmap(fitz.csRGB, pix)
        return pix.tobytes("png"), "image/png"
    finally:
        pix = None


# ──────────────────────────────────────────────────────────────────────
# DOCX (with embedded image extraction)
# ──────────────────────────────────────────────────────────────────────

class DocxParser:
    """Parser for .docx files using python-docx.

    Paragraphs are emitted as Markdown with heading levels preserved.
    Embedded images are extracted per paragraph (when a run contains a
    ``w:drawing`` element) and inserted inline as ``[[IMAGE:<id>]]``.
    Tables are rendered as pipe-separated rows, **at their true position
    in the document** — a table's lead-in sentence ("各項規格如下表：") has
    to stay next to the table, otherwise chunking splits the two apart and
    retrieval returns one without the other.
    """

    _NS_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
    _NS_R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
    _NS_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

    def parse(self, file_path: str) -> ParsedDocument:
        try:
            from docx import Document  # type: ignore[import]
        except ImportError as exc:
            raise ImportError(
                "python-docx is required for DOCX parsing. "
                "Install with: pip install 'agentic-rag[rag]'"
            ) from exc

        path = Path(file_path)
        doc = Document(file_path)

        parts: list[str] = []
        images: dict[str, ImageRef] = {}
        title = path.stem

        skipped_with_content: Counter[str] = Counter()
        for kind, block in self._iter_body_blocks(doc, skipped_with_content):
            if kind == "table":
                rows = []
                for row in block.rows:
                    cells = [cell.text.strip() for cell in row.cells]
                    rows.append(" | ".join(cells))
                if rows:
                    parts.append("\n".join(rows))
                continue

            para = block
            text = para.text.strip()
            para_images = self._collect_para_images(para, doc, images)

            if not text and not para_images:
                continue

            style = para.style.name if para.style is not None else ""
            if style.startswith("Heading 1"):
                parts.append(f"# {text}")
                if title == path.stem and text:
                    title = text
            elif style.startswith("Heading 2"):
                parts.append(f"## {text}")
            elif style.startswith("Heading 3"):
                parts.append(f"### {text}")
            elif style.startswith("Heading 4"):
                parts.append(f"#### {text}")
            elif text:
                parts.append(text)

            for img_id in para_images:
                parts.append(f"[[IMAGE:{img_id}]]")

        metadata = {"title": title, "embedded_images": len(images)}
        skipped_n = int(sum(skipped_with_content.values()))
        if skipped_n:
            metadata["docx_wrapped_skipped"] = skipped_n
        return ParsedDocument(
            content="\n\n".join(parts),
            metadata=metadata,
            source_path=file_path,
            format="docx",
            images=images,
        )

    def _iter_body_blocks(
        self,
        doc: Any,
        skipped_with_content: Counter[str] | None = None,
    ) -> Iterator[tuple[str, Any]]:
        """Yield ``("paragraph" | "table", obj)`` in true document order.

        ``doc.paragraphs`` and ``doc.tables`` are two *independent*
        collections: iterating one after the other necessarily emits every
        table after every paragraph, no matter where the tables actually sit.
        The body element is the only place that records the interleaving, so
        walk it directly. This yields exactly the same objects as those two
        collections (top-level ``w:p`` / ``w:tbl`` children) — only the order
        differs — so nothing that used to be captured can be lost here.

        Body children that are neither ``w:p`` nor ``w:tbl`` are skipped, as
        they always were. Most are inert (``w:sectPr``, bookmarks, comments).
        But some — content controls (``w:sdt``) and tracked insertions
        (``w:ins``) — *wrap* real paragraphs and tables, and their text has
        never been extracted by this parser. That is silent data loss on
        exactly the kind of template an organisation standardises on, so
        count those and say so once per document: a number in the worker log
        beats text quietly going missing. Detection is by content (does this
        child contain a ``w:p`` / ``w:tbl`` anywhere below it?), not by a
        list of known wrapper tags, so an unanticipated wrapper still counts.
        """
        from docx.table import Table
        from docx.text.paragraph import Paragraph

        skipped = skipped_with_content if skipped_with_content is not None else Counter()

        for child in doc.element.body.iterchildren():
            tag = child.tag
            if not isinstance(tag, str):
                continue  # lxml comment / processing instruction
            if tag == f"{self._NS_W}p":
                yield "paragraph", Paragraph(child, doc)
            elif tag == f"{self._NS_W}tbl":
                yield "table", Table(child, doc)
            elif (
                child.find(f".//{self._NS_W}p") is not None
                or child.find(f".//{self._NS_W}tbl") is not None
            ):
                skipped[tag.rpartition("}")[2]] += 1

        if skipped:
            logger.warning(
                "DOCX: skipped %d body element(s) that wrap text but are not "
                "top-level w:p / w:tbl (%s); their content is NOT extracted. "
                "These are usually content controls (w:sdt) or tracked "
                "insertions (w:ins) — accept the revisions or convert the "
                "content controls to plain text before uploading.",
                sum(skipped.values()),
                ", ".join(
                    f"w:{tag}={count}"
                    for tag, count in sorted(skipped.items())
                ),
            )

    def _collect_para_images(
        self,
        paragraph,
        doc,
        images: dict[str, ImageRef],
    ) -> list[str]:
        """Find inline images inside a paragraph and register them."""
        ids: list[str] = []
        for run in paragraph.runs:
            for blip in run._element.iter(f"{self._NS_A}blip"):
                rid = blip.get(f"{self._NS_R}embed")
                if not rid:
                    continue
                try:
                    image_part = doc.part.related_parts[rid]
                    image_bytes = image_part.blob
                    mime = getattr(image_part, "content_type", "image/png")
                except Exception as exc:
                    logger.warning("DOCX image rId=%s extraction failed: %s", rid, exc)
                    continue

                img_id = _new_image_id()
                images[img_id] = ImageRef(
                    image_id=img_id,
                    image_bytes=image_bytes,
                    mime=mime,
                )
                ids.append(img_id)
        return ids


# ──────────────────────────────────────────────────────────────────────
# Legacy .doc (antiword)
# ──────────────────────────────────────────────────────────────────────

class DocParser:
    """Parser for legacy .doc files using antiword CLI."""

    def parse(self, file_path: str) -> ParsedDocument:
        import subprocess

        path = Path(file_path)
        try:
            # L7: 用 ``--`` 分隔 option 與 positional arg，避免 file_path
            # 開頭為 ``-`` 時被 antiword 解析成 flag（argument injection）。
            result = subprocess.run(
                ["antiword", "--", file_path],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            content = result.stdout
            if not content and result.returncode != 0:
                raise RuntimeError(f"antiword failed: {result.stderr}")
        except FileNotFoundError as exc:
            raise RuntimeError(
                "antiword is required for .doc parsing. Install with: apt install antiword"
            ) from exc

        return ParsedDocument(
            content=content,
            metadata={"title": path.stem},
            source_path=file_path,
            format="doc",
        )


# ──────────────────────────────────────────────────────────────────────
# ODT
# ──────────────────────────────────────────────────────────────────────

class OdtParser:
    """Parser for .odt files using odfpy."""

    def parse(self, file_path: str) -> ParsedDocument:
        try:
            from odf.opendocument import load as odf_load  # type: ignore[import]
            from odf.teletype import extractText  # type: ignore[import]
        except ImportError as exc:
            raise ImportError(
                "odfpy is required for ODT parsing. "
                "Install with: pip install 'agentic-rag[rag]'"
            ) from exc

        path = Path(file_path)
        doc = odf_load(file_path)

        parts: list[str] = []
        title = path.stem

        for element in doc.text.childNodes:
            text = extractText(element).strip()
            if not text:
                continue
            tag = element.qname[1] if hasattr(element, "qname") else ""
            if tag == "h":
                outline = element.getAttribute("text:outline-level") or "1"
                prefix = "#" * int(outline)
                parts.append(f"{prefix} {text}")
                if title == path.stem and int(outline) == 1:
                    title = text
            else:
                parts.append(text)

        return ParsedDocument(
            content="\n\n".join(parts),
            metadata={"title": title},
            source_path=file_path,
            format="odt",
        )


# ──────────────────────────────────────────────────────────────────────
# Standalone image files
# ──────────────────────────────────────────────────────────────────────

_IMAGE_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
}


class ImageParser:
    """Parser for standalone image files.

    The parsed document contains a single ``[[IMAGE:<id>]]`` placeholder
    which the IngestionService turns into a VLM caption.
    """

    def parse(self, file_path: str) -> ParsedDocument:
        path = Path(file_path)
        ext = path.suffix.lower()
        mime = (
            _IMAGE_MIME.get(ext)
            or mimetypes.guess_type(file_path)[0]
            or "application/octet-stream"
        )
        image_bytes = path.read_bytes()

        img_id = _new_image_id()
        images = {
            img_id: ImageRef(
                image_id=img_id,
                image_bytes=image_bytes,
                mime=mime,
            )
        }

        return ParsedDocument(
            content=f"# {path.stem}\n\n[[IMAGE:{img_id}]]",
            metadata={"title": path.stem, "embedded_images": 1},
            source_path=file_path,
            format="image",
            images=images,
        )


# ──────────────────────────────────────────────────────────────────────
# Registry
# ──────────────────────────────────────────────────────────────────────

class ParserRegistry:
    """Select the appropriate parser based on file extension.

    By default uses the lightweight native parsers (pymupdf4llm, python-docx,
    odfpy, ...). Set ``DOC_PARSER=docling`` in the environment to route
    PDF/DOCX/PPTX/XLSX/HTML through Docling instead — see
    ``ingestion.docling_parser`` for the trade-offs.
    """

    _PARSERS: dict[str, DocumentParser] = {
        ".txt": PlainTextParser(),
        ".md": MarkdownParser(),
        # Common alias for Markdown; same parser as .md (picker used to
        # offer .markdown while the registry only knew .md → upload 202
        # then worker E_PARSE_FORMAT_UNSUPPORTED).
        ".markdown": MarkdownParser(),
        ".json": JsonParser(),
        ".html": HtmlParser(),
        ".htm": HtmlParser(),
        ".rtf": RtfParser(),
        ".pdf": PdfParser(),
        ".docx": DocxParser(),
        ".doc": DocParser(),
        ".odt": OdtParser(),
        # text-class / source formats — decoded by ``_decode_text_bytes``
        # (strict UTF-8, or BOM'd ASCII-dominant UTF-16; anything else is
        # refused). NOT errors="replace": that was the base-version bug
        # this package exists to close.
        ".py": PlainTextParser(),
        ".csv": PlainTextParser(),
        ".tsv": PlainTextParser(),
        ".log": PlainTextParser(),
        ".yaml": PlainTextParser(),
        ".yml": PlainTextParser(),
        ".toml": PlainTextParser(),
        ".ini": PlainTextParser(),
        ".xml": PlainTextParser(),
        # .svg stays image-only (frontend inline); path markup burns tokens.
        ".sh": PlainTextParser(),
        ".bash": PlainTextParser(),
        ".sql": PlainTextParser(),
        ".js": PlainTextParser(),
        ".ts": PlainTextParser(),
        ".tsx": PlainTextParser(),
        ".jsx": PlainTextParser(),
        ".java": PlainTextParser(),
        ".go": PlainTextParser(),
        ".rs": PlainTextParser(),
        ".c": PlainTextParser(),
        ".cpp": PlainTextParser(),
        ".h": PlainTextParser(),
        ".hpp": PlainTextParser(),
        ".rb": PlainTextParser(),
        ".php": PlainTextParser(),
        ".r": PlainTextParser(),
        ".m": PlainTextParser(),
        ".tex": PlainTextParser(),
        # USAF Digital DATCOM / engineering plain-text decks (ASCII).
        # .dat/.out are also used as binary by other tools — content gates refuse those.
        ".dcm": PlainTextParser(),
        ".dat": PlainTextParser(),
        ".inp": PlainTextParser(),
        ".out": PlainTextParser(),
        # standalone images
        ".png": ImageParser(),
        ".jpg": ImageParser(),
        ".jpeg": ImageParser(),
        ".webp": ImageParser(),
        ".gif": ImageParser(),
        ".bmp": ImageParser(),
    }

    _docling_parser = None  # type: ignore[var-annotated]
    _docling_initialised = False

    @classmethod
    def _get_docling_parser(cls):
        """Return the cached Docling parser, or None when DOC_PARSER!=docling.

        The env var is consulted on every call (cheap) so switching it off
        immediately takes effect, but the parser instance is constructed at
        most once per process. Since docling went remote (2026-08) that
        instance is a ``RemoteDoclingParser`` (an HTTP client) or ``None`` —
        the in-process ``DoclingParser`` class is reference-only and is never
        built here.
        """
        if os.getenv("DOC_PARSER", "native").lower() != "docling":
            return None
        if not cls._docling_initialised:
            from .docling_parser import build_docling_parser_from_env
            # ⚠ 2026-08-17:失敗不再是「fallback 到 native」。DOC_PARSER=docling
            # 是擁有者明示的選用──選了它、建構失敗卻靜默退回較差的 native 解析,
            # 等於拿一個綠燈換一台被關掉的 docling。讓錯誤往上冒,operatator 才會
            # 看見。RemoteDoclingParser 的建構子刻意不 raise(缺 DOCLING_URL 是延遲
            # 到 parse() 才驗),所以會走到這裡 raise 的只有壞設定(如過時 timeout)。
            cls._docling_parser = build_docling_parser_from_env()
            cls._docling_initialised = True
        return cls._docling_parser

    @classmethod
    def _reset_docling_cache(cls) -> None:
        """Test hook: drop the cached Docling parser so env changes re-apply."""
        cls._docling_parser = None
        cls._docling_initialised = False

    @classmethod
    def get(cls, file_path: str) -> DocumentParser:
        """Return the parser for the given file extension."""
        ext = Path(file_path).suffix.lower()

        # Extensionless (DATCOM for005/for006, README, Makefile, …): decide by
        # content via PlainTextParser's decode guards — not by inventing a name.
        if not ext:
            return PlainTextParser()

        # Docling override for the formats it actually handles.
        from .docling_parser import DOCLING_SUPPORTED_EXTS  # local import: cheap, no model load
        if ext in DOCLING_SUPPORTED_EXTS:
            docling = cls._get_docling_parser()
            if docling is not None:
                return docling

        parser = cls._PARSERS.get(ext)
        if parser is None:
            supported = ", ".join(sorted(cls._PARSERS))
            raise ValueError(
                f"Unsupported file extension '{ext}'. Supported: {supported}"
            )
        return parser

    @classmethod
    def parse(cls, file_path: str) -> ParsedDocument:
        """Parse a document using the appropriate parser."""
        return cls.get(file_path).parse(file_path)

    @classmethod
    def supported_extensions(cls) -> list[str]:
        """Return list of supported file extensions."""
        return list(cls._PARSERS)
