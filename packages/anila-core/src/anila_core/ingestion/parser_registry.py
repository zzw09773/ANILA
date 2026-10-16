"""Document parsers for the AgenticRAG pipeline.

Supports text, source code, PDF, DOCX/DOC, PPTX/PPT, ODT, RTF, plus standalone
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
import re
import unicodedata
import uuid
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, cast, runtime_checkable

from .text_quality import has_recovered_text

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
    metadata: dict[str, Any] = field(default_factory=dict)
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
# NOT supported — owner decision 2026-08-01:
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
    要等治理中心註冊文件解析來源；這是沒有來源時永遠可用的原生解析。
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

            def handle_starttag(
                self, tag: str, attrs: list[tuple[str, str | None]]
            ) -> None:
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

            def handle_endtag(self, tag: str) -> None:
                if tag == "title":
                    self._in_title = False
                elif self._skip and self._skip[-1] == tag:
                    self._skip.pop()

            def handle_data(self, data: str) -> None:
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
    """Convert RTF locally to preserve tables and headings."""

    def parse(self, file_path: str) -> ParsedDocument:
        _decode_text_bytes(Path(file_path).read_bytes())
        from .office_conversion import convert_legacy
        return cast(ParsedDocument, convert_legacy(file_path))


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
    (a scan or a font-subsetted PDF), parsing stops with a message that
    Docling is required. This parser does not call a vision model.

    ⚠ The ``[[IMAGE:<id>]]`` tokens this parser inserts are **not** text.
    ``needs_ocr_fallback`` strips them before measuring; do not hand it a
    string whose placeholders have already been rewritten into captions,
    or a captioned scan will read as a text document.
    """

    def parse(self, file_path: str) -> ParsedDocument:
        try:
            import pymupdf4llm  # type: ignore[import-untyped]  # no type stubs
            import fitz  # type: ignore[import-untyped]  # pymupdf fitz shim has no stubs
        except ImportError as exc:
            raise ImportError(
                "pymupdf4llm and pymupdf are required for PDF parsing. "
                "Install with: pip install 'agentic-rag[rag]'"
            ) from exc

        path = Path(file_path)

        with fitz.open(file_path) as preflight:
            if preflight.needs_pass:
                from .errors import ParseError
                raise ParseError.corrupt(
                    "此 PDF 受到密碼保護，請先解除密碼後再上傳。",
                    details={"reason": "encrypted_pdf"},
                )

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
        # Scans use the independent local OCR adapter below. The native
        # text-layer return keeps ``ocr_used=False``. Nothing downstream may
        # infer page structure from
        # ``metadata["pages"]`` alone: ``extract_text`` re-derives
        # ``has_page_boundaries`` by counting the fields.
        content = "\f\n".join(parts)
        from .ocr import needs_ocr_fallback

        if needs_ocr_fallback(content, page_texts=page_texts):
            from .local_ocr import pdf_document
            return cast(ParsedDocument, pdf_document(file_path))

        metadata: dict[str, Any] = {
            "title": path.stem,
            "pages": len(page_chunks),
            "embedded_images": len(images),
            "ocr_used": False,
        }

        return ParsedDocument(
            content=content,
            metadata=metadata,
            source_path=file_path,
            format="pdf",
            images=images,
        )


def _extract_pdf_image(doc: Any, xref: int) -> tuple[bytes, str]:
    """Return (bytes, mime) for a PDF image xref, preferring the native format."""
    import fitz

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
            from docx import Document
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

        table_blocks: list[str] = []
        from .docx_locations import DocxPageEvidence

        page_evidence = DocxPageEvidence()
        skipped_with_content: Counter[str] = Counter()
        for block_index, (kind, block) in enumerate(self._iter_body_blocks(doc, skipped_with_content)):
            if kind == "table":
                rows = []
                visited_cells: set[Any] = set()
                for row_index, row in enumerate(block.rows):
                    cells = []
                    for cell_index, cell in enumerate(row.cells):
                        text = cell.text.strip()
                        for paragraph_index, paragraph in enumerate(cell.paragraphs):
                            if cell._tc not in visited_cells:
                                page_evidence.paragraph(paragraph, {
                                    'body_block': block_index,
                                    'table_row': row_index,
                                    'table_cell': cell_index,
                                    'paragraph': paragraph_index,
                                })
                            for image_id in self._collect_para_images(paragraph, doc, images):
                                text += f"\n[[IMAGE:{image_id}]]"
                        visited_cells.add(cell._tc)
                        cells.append(text.strip())
                    rows.append(" | ".join(cells))
                if rows:
                    table_text = "\n".join(rows)
                    parts.append(table_text)
                    table_blocks.append(table_text)
                continue

            para = block
            page_evidence.paragraph(para, {'body_block': block_index})
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

        metadata = {"title": title, "embedded_images": len(images), "table_blocks": table_blocks,
                    **page_evidence.metadata()}
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
        paragraph: Any,
        doc: Any,
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
# Legacy Office conversion
# ──────────────────────────────────────────────────────────────────────

class DocParser:
    """Convert a legacy Office document with the local converter."""

    def parse(self, file_path: str) -> ParsedDocument:
        from .office_conversion import convert_legacy
        return cast(ParsedDocument, convert_legacy(file_path))


class SourceCodeParser:
    """Decode source as data and annotate intact source boundaries."""

    def parse(self, file_path: str) -> ParsedDocument:
        from .source_code import source_units, summarize_boundary_quality
        parsed = PlainTextParser().parse(file_path)
        language = "python" if Path(file_path).suffix.lower() == ".py" else "cpp"
        parsed.format = language
        units = source_units(parsed.content, language)
        parsed.metadata.update({"source_language": language, "source_units": units,
                                **summarize_boundary_quality(units)})
        return parsed


class PptxParser:
    """Lazy import avoids a registry/presentation import cycle."""

    def parse(self, file_path: str) -> ParsedDocument:
        from .presentation import PptxParser as Reader
        return Reader().parse(file_path)


# ──────────────────────────────────────────────────────────────────────
# ODT
# ──────────────────────────────────────────────────────────────────────

class OdtParser:
    """Parser for .odt files using odfpy."""

    def parse(self, file_path: str) -> ParsedDocument:
        try:
            from odf.opendocument import load as odf_load  # type: ignore[import-untyped]  # odfpy has no stubs
            from odf.teletype import extractText  # type: ignore[import-untyped]  # odfpy has no stubs
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
# .xlsx — stdlib reader (zip + XML), no openpyxl / docling needed
# ──────────────────────────────────────────────────────────────────────

_XLSX_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_XLSX_REL_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_XLSX_PKG_REL_NS = "{http://schemas.openxmlformats.org/package/2006/relationships}"
_XLSX_MAX_CELLS = 200_000
_XLSX_MAX_CHARS = 2_000_000
_XLSX_MAX_XML_BYTES = 64 * 1024 * 1024  # 解壓後單一 XML 的上限，防 zip bomb
_XLSX_COL_RE = re.compile(r"^([A-Z]+)")


def _xlsx_col_index(ref: str) -> int:
    match = _XLSX_COL_RE.match(ref or "")
    if not match:
        return -1
    index = 0
    for char in match.group(1):
        index = index * 26 + (ord(char) - 64)
    return index - 1


class XlsxParser:
    """Read cell values (cached results for formulas) sheet by sheet.

    Each sheet becomes a Markdown section with a pipe table. Values are the
    ones Excel last saved; formulas are not recalculated. Hidden sheets are
    included and marked, so nothing is silently dropped.
    """

    def parse(self, file_path: str) -> ParsedDocument:
        import xml.etree.ElementTree as ET
        import zipfile

        path = Path(file_path)
        with zipfile.ZipFile(file_path) as package:
            def read(name: str) -> bytes | None:
                try:
                    info = package.getinfo(name)
                except KeyError:
                    return None
                if info.file_size > _XLSX_MAX_XML_BYTES:
                    raise ValueError(f"{name} is too large to read")
                return package.read(name)

            workbook_xml = read("xl/workbook.xml")
            if workbook_xml is None:
                raise ValueError("not an xlsx workbook (xl/workbook.xml missing)")
            workbook = ET.fromstring(workbook_xml)
            rels: dict[str, str] = {}
            rels_xml = read("xl/_rels/workbook.xml.rels")
            if rels_xml is not None:
                for rel in ET.fromstring(rels_xml).iter(f"{_XLSX_PKG_REL_NS}Relationship"):
                    target = rel.get("Target") or ""
                    target = target.lstrip("/")
                    if not target.startswith("xl/"):
                        target = "xl/" + target
                    rels[rel.get("Id") or ""] = target
            shared: list[str] = []
            shared_xml = read("xl/sharedStrings.xml")
            if shared_xml is not None:
                for item in ET.fromstring(shared_xml).iter(f"{_XLSX_NS}si"):
                    shared.append("".join(t.text or "" for t in item.iter(f"{_XLSX_NS}t")))

            parts: list[str] = []
            total_cells = 0
            total_chars = 0
            truncated = False
            sheet_names: list[str] = []
            for index, sheet in enumerate(workbook.iter(f"{_XLSX_NS}sheet"), start=1):
                name = sheet.get("name") or f"工作表{index}"
                hidden = (sheet.get("state") or "visible") != "visible"
                rel_id = sheet.get(f"{_XLSX_REL_NS}id") or ""
                target = rels.get(rel_id) or f"xl/worksheets/sheet{index}.xml"
                sheet_xml = read(target)
                if sheet_xml is None:
                    continue
                sheet_names.append(name)
                rows: list[list[str]] = []
                for row in ET.fromstring(sheet_xml).iter(f"{_XLSX_NS}row"):
                    values: dict[int, str] = {}
                    for position, cell in enumerate(row.findall(f"{_XLSX_NS}c")):
                        value = self._cell_value(cell, shared)
                        if value == "":
                            continue
                        column = _xlsx_col_index(cell.get("r") or "")
                        values[column if column >= 0 else position] = value
                        total_cells += 1
                        total_chars += len(value)
                    if values:
                        width = max(values) + 1
                        rows.append([values.get(i, "") for i in range(width)])
                    if total_cells > _XLSX_MAX_CELLS or total_chars > _XLSX_MAX_CHARS:
                        truncated = True
                        break
                heading = f"## 工作表：{name}" + ("（隱藏）" if hidden else "")
                parts.append(heading)
                parts.append(self._table(rows) if rows else "（空白工作表）")
                if truncated:
                    parts.append("（內容過多，後面的儲存格沒有讀入。）")
                    break

        return ParsedDocument(
            content="\n\n".join(parts),
            metadata={
                "title": path.stem,
                "sheets": sheet_names,
                "truncated": truncated,
                "parser": "xlsx-native",
            },
            source_path=file_path,
            format="xlsx",
        )

    @staticmethod
    def _cell_value(cell: Any, shared: list[str]) -> str:
        kind = cell.get("t") or "n"
        if kind == "inlineStr":
            node = cell.find(f"{_XLSX_NS}is")
            if node is None:
                return ""
            return "".join(t.text or "" for t in node.iter(f"{_XLSX_NS}t")).strip()
        value_node = cell.find(f"{_XLSX_NS}v")
        raw = (value_node.text or "") if value_node is not None else ""
        if kind == "s":
            try:
                return shared[int(raw)].strip()
            except (ValueError, IndexError):
                return ""
        if kind == "b":
            return "TRUE" if raw.strip() == "1" else ("FALSE" if raw.strip() == "0" else "")
        if kind == "e":
            return raw.strip()  # 例如 #DIV/0!，照原樣保留
        return raw.strip()

    @staticmethod
    def _table(rows: list[list[str]]) -> str:
        width = max(len(row) for row in rows)

        def cell(text: str) -> str:
            return text.replace("|", "\\|").replace("\n", " ")

        lines = []
        for number, row in enumerate(rows):
            padded = row + [""] * (width - len(row))
            lines.append("| " + " | ".join(cell(value) for value in padded) + " |")
            if number == 0:
                lines.append("|" + "---|" * width)
        return "\n".join(lines)


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
    """Recognize standalone image text through the configured Docling service."""

    def parse(self, file_path: str) -> ParsedDocument:
        from .image_ocr import recognize_image, require_image_ocr
        path = Path(file_path)
        metadata: dict[str, Any] = {"title": path.stem, "ocr_used": True, "pages": 1}
        content = recognize_image(path.read_bytes(), require_image_ocr(), metadata)
        metadata["parser"] = metadata["actual_parser"]
        metadata["quality"] = "ocr_text_only"
        if not has_recovered_text(content):
            from .errors import ParseError
            raise ParseError.corrupt("圖片辨識後沒有可用文字，請確認圖片清晰且包含文字。",
                                     details={"reason": "empty_image_ocr_output"})
        image_id = _new_image_id()
        images = {image_id: ImageRef(image_id, path.read_bytes(),
                                     _IMAGE_MIME.get(path.suffix.lower(), "image/png"), page=1)}
        return ParsedDocument(content=content + f"\n\n[[IMAGE:{image_id}]]",
                              metadata=metadata,
                              source_path=file_path, format="image", images=images)


# ──────────────────────────────────────────────────────────────────────
# Registry
# ──────────────────────────────────────────────────────────────────────

class ParserRegistry:
    """Select the appropriate parser based on file extension.

    By default uses the lightweight native parsers (pymupdf4llm, python-docx,
    odfpy, ...). A registered console source can route PDF/DOCX/PPTX/XLSX/HTML
    through the remote Docling parser instead.
    """

    _NATIVE_PREFERRED: frozenset[str] = frozenset({".xlsx", ".pptx"})

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
        ".ppt": DocParser(),
        ".pptx": PptxParser(),
        ".odt": OdtParser(),
        ".xlsx": XlsxParser(),
        # text-class / source formats — decoded by ``_decode_text_bytes``
        # (strict UTF-8, or BOM'd ASCII-dominant UTF-16; anything else is
        # refused). NOT errors="replace": that was the base-version bug
        # this package exists to close.
        ".py": SourceCodeParser(),
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
        ".c": SourceCodeParser(),
        ".cpp": SourceCodeParser(),
        ".cc": SourceCodeParser(),
        ".cxx": SourceCodeParser(),
        ".c++": SourceCodeParser(),
        ".h": SourceCodeParser(),
        ".hpp": SourceCodeParser(),
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

    _docling_parser: DocumentParser | None = None
    _docling_initialised = False

    @classmethod
    def _get_docling_parser(cls) -> DocumentParser | None:
        """已註冊治理中心來源時，只聽那個來源。

        沒註冊就回 ``None``（原生解析器），不讀 ``DOC_PARSER``。
        治理中心說沒設定才回 ``None``。設定不可讀或不完整會在此回報；
        共用 parse 入口會記下原因並採用獨立的原生解析器。
        """
        from anila_core.ingestion.docling_source import (
            DoclingMisconfigured,
            DoclingSourceUnavailable,
            docling_source_registered,
            resolve_docling_endpoint,
        )
        from anila_core.ingestion.errors import ParseError, RemoteParseError

        if docling_source_registered():
            try:
                endpoint = resolve_docling_endpoint()
            except DoclingMisconfigured as exc:
                raise ParseError.bad_config(
                    "文件解析服務已啟用，但位址不完整。"
                    "請到治理中心的外部服務補上。"
                    "此份文件會嘗試院內離線解析。",
                    details={"reason": "misconfigured"},
                ) from exc
            except DoclingSourceUnavailable as exc:
                raise RemoteParseError.endpoint_unavailable(
                    "目前讀不到文件解析設定，此份文件會嘗試院內離線解析。",
                    details={"reason": "source_unavailable"},
                ) from exc
            if endpoint is None:
                cls._docling_parser = None
                cls._docling_initialised = True
                return None
            current = cls._docling_parser
            wanted_url = endpoint.base_url.rstrip("/")
            wanted_token = endpoint.token or ""
            if (
                current is not None
                and getattr(current, "_base_url", None) == wanted_url
                and getattr(current, "_token", "") == wanted_token
            ):
                return current
            from .docling_parser import build_remote_docling_parser
            cls._docling_parser = build_remote_docling_parser(
                wanted_url, wanted_token
            )
            cls._docling_initialised = True
            return cls._docling_parser

        return None

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
        # 試算表一律用本地讀取：儲存格值是確定的，不需要版面分析或 OCR，
        # 也不會因為遠端解析服務的設定問題而整份讀不到。
        from .docling_parser import DOCLING_SUPPORTED_EXTS  # local import: cheap, no model load
        if ext in DOCLING_SUPPORTED_EXTS and ext not in cls._NATIVE_PREFERRED:
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
        from .docling_parser import DOCLING_SUPPORTED_EXTS
        from .errors import ParseError
        ext = Path(file_path).suffix.lower()
        selected = None
        fallback_reason = None
        try:
            selected = cls.get(file_path)
            parsed = selected.parse(file_path)
            if (
                not has_recovered_text(parsed.content)
                and not (parsed.images and parsed.format in {"docx", "pptx"})
                and not parsed.metadata.get("unsupported_objects")
            ):
                raise ParseError.corrupt("文件解析後沒有可用文字。", details={"reason": "empty_parser_output"})
        except Exception as exc:
            native = cls._PARSERS.get(ext)
            if ext not in DOCLING_SUPPORTED_EXTS or native is None or selected is native:
                raise
            fallback_reason = getattr(exc, "code", type(exc).__name__)
            logger.warning("Document parser fallback: format=%s reason=%s", ext, fallback_reason)
            parsed = native.parse(file_path)
            if not has_recovered_text(parsed.content) and not (
                parsed.images and parsed.format in {"docx", "pptx"}
            ):
                raise ParseError.corrupt("文件解析後沒有可用文字。", details={"reason": "empty_fallback_output"})
        if parsed.metadata.get("unsupported_objects"):
            # Keep the independent native result. Only the configured, approved
            # parser may supplement unsupported presentation objects; never turn
            # an unpaged remote blob into invented slide citations.
            parsed.metadata["presentation_recovery"] = "not_configured"
            try:
                docling = cls._get_docling_parser()
                if docling is not None:
                    recovered = docling.parse(file_path)
                    if not has_recovered_text(recovered.content):
                        raise ParseError.corrupt("簡報物件復原後沒有可用文字。")
                    native_slides = parsed.content.split("\f")
                    remote_slides = recovered.content.split("\f")
                    if len(native_slides) == len(remote_slides):
                        parsed.content = "\f".join(
                            native if remote.strip() in native else native + "\n\n" + remote
                            for native, remote in zip(native_slides, remote_slides)
                        )
                        parsed.metadata["actual_parser"] = "pptx-native+docling"
                        parsed.metadata["presentation_recovery"] = "docling_supplement"
                    else:
                        parsed.metadata["presentation_recovery"] = "remote_slide_locations_unknown"
            except Exception as exc:
                parsed.metadata["presentation_recovery"] = "unavailable"
                parsed.metadata["presentation_recovery_error"] = getattr(
                    exc, "code", type(exc).__name__
                )
            # Successful remote extraction alone cannot prove every chart or
            # diagram is complete. Retain the native parser's partial notice.
            if not has_recovered_text(parsed.content) and not parsed.images:
                raise ParseError.bad_config(
                    "簡報只有尚未復原的圖表或 SmartArt，目前沒有可索引文字；請管理員確認核准的 Docling，或另存包含可讀文字的版本。",
                    details={
                        "reason": "presentation_objects_unavailable",
                        "unsupported_objects": parsed.metadata["unsupported_objects"],
                    },
                )
        parsed.metadata.setdefault("actual_parser", parsed.metadata.get("parser") or type(cls._PARSERS.get(ext)).__name__)
        if fallback_reason:
            parsed.metadata.update({"parser_fallback": True, "fallback_reason": fallback_reason,
                                    "quality": parsed.metadata.get("quality", "native_layout"),
                                    "quality_notice": parsed.metadata.get("quality_notice") or "文件解析服務無法處理，已使用院內離線解析；複雜版面請核對原檔。"})
        if parsed.images and parsed.format in {"docx", "pptx"}:
            try:
                docling = cls._get_docling_parser()
            except Exception:
                docling = None
            from .image_ocr import recognize_image
            recognized_count = 0
            for image_id, ref in parsed.images.items():
                try:
                    recognized = recognize_image(ref.image_bytes, docling)
                except Exception as exc:
                    parsed.metadata["ocr_notice"] = "部分圖片文字尚未辨識；請核對原檔或通知管理員確認離線 OCR 與 Docling 服務。"
                    parsed.metadata["image_ocr_error"] = getattr(exc, "code", type(exc).__name__)
                    continue
                if recognized:
                    marker = f"[[IMAGE:{image_id}]]"
                    replacement = recognized + "\n\n" + marker
                    parsed.content = parsed.content.replace(marker, replacement)
                    if "table_blocks" in parsed.metadata:
                        parsed.metadata["table_blocks"] = [
                            table.replace(marker, replacement)
                            for table in parsed.metadata["table_blocks"]
                        ]
                    recognized_count += 1
            parsed.metadata["embedded_image_ocr_count"] = recognized_count
            if not has_recovered_text(parsed.content):
                raise ParseError.bad_config(
                    "此文件僅含圖片，目前無法辨識圖片文字。請管理員恢復 Docling，或提供已審核的離線 Tesseract 與 chi_tra 語料。",
                    details={"reason": "image_ocr_unavailable"},
                )
        return parsed

    @classmethod
    def supported_extensions(cls) -> list[str]:
        """Return list of supported file extensions."""
        return list(cls._PARSERS)
