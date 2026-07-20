"""Versioned deterministic input-injection detection for Router E1.

E1 is a bounded best-effort defense-in-depth signal.  It is not a completeness
guarantee and it is not an acceptance gate: deterministic matching cannot make
free-text injection detection complete.  The load-bearing routing guarantees
are the structural E2/E3/PolicyGate/CandidateFilter controls, which do not
depend on detecting injection text.  Because this signal fails in the safe
direction, it may over-deny benign security or documentation queries; that
availability cost is an accepted residual.

The implementation deliberately introduces no model, classifier, downloaded
weight, or network dependency.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from typing import Any


INJECTION_PATTERN_SET_VERSION = "e1-deterministic-v2"
MAX_REQUEST_CONTENT_SCAN_CHARS = 256 * 1024


class CanonicalizationLimitExceeded(ValueError):
    """Raised when a bounded input cannot produce a bounded scan projection."""


# These are the common Cyrillic/Greek lookalikes used to disguise the ASCII
# letters in control verbs and role/mode directives.  The map is deliberately
# narrow and is applied only to the matching projection, never to stored data.
_HOMOGLYPH_MAP = str.maketrans(
    {
        "А": "A",
        "Β": "B",
        "Е": "E",
        "Η": "H",
        "Ι": "I",
        "Κ": "K",
        "Μ": "M",
        "Ν": "N",
        "О": "O",
        "Ρ": "P",
        "Τ": "T",
        "Χ": "X",
        "Υ": "Y",
        "Ζ": "Z",
        "а": "a",
        "б": "b",
        "в": "v",
        "е": "e",
        "к": "k",
        "м": "m",
        "н": "n",
        "о": "o",
        "р": "p",
        "с": "c",
        "т": "t",
        "х": "x",
        "у": "y",
        "і": "i",
        "ј": "j",
        "Λ": "L",
        "α": "a",
        "β": "b",
        "γ": "g",
        "δ": "d",
        "ε": "e",
        "ζ": "z",
        "η": "h",
        "ι": "i",
        "κ": "k",
        "λ": "l",
        "μ": "m",
        "ν": "n",
        "ο": "o",
        "ρ": "p",
        "τ": "t",
        "υ": "y",
        "χ": "x",
    }
)


def canonicalize_for_matching(value: str) -> str:
    """Return a bounded, non-mutating canonical matching projection.

    NFKC and casefold can expand a string.  Such expansion is an enforcement
    failure, not a reason to truncate the canonical tail and continue scanning.
    """

    if not isinstance(value, str):
        return ""
    if len(value) > MAX_REQUEST_CONTENT_SCAN_CHARS:
        raise CanonicalizationLimitExceeded("input exceeds the enforcement scan cap")
    normalized = unicodedata.normalize("NFKC", value)
    if len(normalized) > MAX_REQUEST_CONTENT_SCAN_CHARS:
        raise CanonicalizationLimitExceeded("NFKC projection exceeds the enforcement scan cap")
    cleaned: list[str] = []
    for char in normalized:
        category = unicodedata.category(char)
        if category == "Cf":
            # Zero-width and bidi format controls disappear so they cannot
            # split a control verb.  They are never removed from the request.
            continue
        if category == "Cc":
            # Preserve token boundaries while neutralising ordinary controls.
            cleaned.append(" ")
            continue
        cleaned.append(char)
    canonical = "".join(cleaned).translate(_HOMOGLYPH_MAP).casefold()
    if len(canonical) > MAX_REQUEST_CONTENT_SCAN_CHARS:
        raise CanonicalizationLimitExceeded("canonical projection exceeds the enforcement scan cap")
    return canonical


_CONTROL_VERB = (
    r"(?:ignore|disregard|forget|override|supersede|bypass|discard|dismiss|"
    r"set\s*(?:-|\u2010|\u2011)?\s*aside|drop|abandon)"
)
_CONTROL_OBJECT = (
    r"(?:instructions?|directions?|rules?|polic(?:y|ies)|guardrails?|"
    r"constraints?|directives?|system\s+(?:prompt|message)|"
    r"developer\s+(?:prompt|message)|routing\s+policy)"
)
_CONTROL_VERB_RE = re.compile(
    rf"\b{_CONTROL_VERB}\b(?:\s+[^\n]{{0,96}})?\b{_CONTROL_OBJECT}\b",
    re.IGNORECASE,
)
_CONTROL_NEGATION_RE = re.compile(
    rf"\b(?:do\s+not|don't|stop|cease)\s+"
    rf"(?:follow|obey|respect|apply|observe|honou?r)\b[^\n]{{0,72}}"
    rf"\b{_CONTROL_OBJECT}\b",
    re.IGNORECASE,
)
_CONTROL_ZH_RE = re.compile(
    r"(?:忽略|無視|不理(?:會)?|拋開|擱置|跳過|繞過|取代|覆寫|覆蓋|"
    r"不遵守|不理睬)[^\n]{0,48}(?:規則|指示|指令|政策|守則|護欄|"
    r"約束|限制|系統提示|開發者訊息|路由政策|外洩|洩漏|揭露)",
    re.IGNORECASE,
)
_CONTROL_ZH_REVERSE = re.compile(
    r"(?:規則|指示|指令|政策|守則|護欄|約束|系統提示|開發者訊息|路由政策)"
    r"[^\n]{0,24}(?:忽略|無視|不理(?:會)?|拋開|擱置|跳過|繞過|取代|覆寫|覆蓋|不遵守)",
    re.IGNORECASE,
)

_ROLE_SWITCH_RE = re.compile(
    r"\b(?:you\s+are\s+now|act\s+as|pretend\s+to\s+be|switch\s+to|"
    r"change\s+to|enter|assume)\b[^\n]{0,96}\b(?:system|developer|"
    r"admin(?:istrator)?|root|unrestricted|privileged|elevated|"
    r"(?:new|special)\s+(?:role|mode)|persona|mode|role)\b",
    re.IGNORECASE,
)
_ROLE_SWITCH_ZH_RE = re.compile(
    r"(?:你現在是|你現在為|請扮演|扮演|切換到|切換為|切換成|進入|"
    r"改成|改用|以[^\n]{0,16}(?:身分|身份)|提升[^\n]{0,16}權限)"
    r"[^\n]{0,48}(?:系統|開發者|管理員|root|無限制|特權|新模式|模式|角色|身分|身份|權限)",
    re.IGNORECASE,
)
_ROLE_ASSIGNMENT_RE = re.compile(
    r"\b(?:set|switch|change|use|take)\s+(?:your\s+)?role\s*[:=]\s*"
    r"(?:system|developer|admin(?:istrator)?|root)\b",
    re.IGNORECASE,
)
_BARE_ROLE_MARKER_RE = re.compile(
    r"\brole\s*[:=]\s*(?:system|developer)\b",
    re.IGNORECASE,
)

_SYSTEM_EXFIL_RE = re.compile(
    r"\b(?:show|give|tell|print|repeat|quote|recite|reveal|expose|dump|"
    r"leak|exfiltrate|disclose)\b[^\n]{0,80}\b(?:your\s+|the\s+|this\s+)?"
    r"(?:system\s+prompt|developer\s+message|hidden\s+instructions?|"
    r"routing\s+policy|internal\s+guardrails?)\b",
    re.IGNORECASE,
)
_SYSTEM_EXFIL_ZH_RE = re.compile(
    r"(?:請|幫我|告訴我|提供|顯示|揭露|洩漏|外洩|透露|重述|列出)[^\n]{0,48}"
    r"(?:系統提示|系統訊息|開發者訊息|隱藏指示|內部規則|路由政策|護欄)",
    re.IGNORECASE,
)
_LEGACY_ROUTE_RE = re.compile(r"\bdispatch\s*:", re.IGNORECASE)
_SPECIAL_TOKEN_RE = re.compile(
    r"<\|(?:system|developer|assistant|im_start|im_end)\|>", re.IGNORECASE
)
_KNOWN_ATTACK_LABEL_RE = re.compile(r"\b(?:jailbreak|prompt\s+injection)\b", re.IGNORECASE)
_UNAPPROVED_TOOL_RE = re.compile(
    r"(?:\b(?:use|call|invoke|run)\b[^\n]{0,64}\b(?:unapproved|unauthori[sz]ed)\s+"
    r"(?:tool|agent)|未授權(?:工具|代理))",
    re.IGNORECASE,
)


def contains_injection(value: Any) -> bool:
    """Detect control intent in a bounded canonical projection.

    Mappings and sequences are traversed for provider-output scanning.  The
    caller-facing request path supplies only the user-content projection.
    """

    if isinstance(value, str):
        try:
            text = canonicalize_for_matching(value)
        except CanonicalizationLimitExceeded:
            # A request or provider field that cannot be fully scanned is
            # unsafe at this boundary.  Callers already interpret this signal
            # as a deny, so no unscanned suffix can pass through.
            return True
        if not text:
            return False
        if any(
            pattern.search(text)
            for pattern in (
                _CONTROL_VERB_RE,
                _CONTROL_NEGATION_RE,
                _CONTROL_ZH_RE,
                _CONTROL_ZH_REVERSE,
                _ROLE_SWITCH_RE,
                _ROLE_SWITCH_ZH_RE,
                _SYSTEM_EXFIL_RE,
                _SYSTEM_EXFIL_ZH_RE,
                _LEGACY_ROUTE_RE,
                _SPECIAL_TOKEN_RE,
                _KNOWN_ATTACK_LABEL_RE,
                _UNAPPROVED_TOOL_RE,
            )
        ):
            return True
        if _ROLE_ASSIGNMENT_RE.search(text):
            return True
        if _BARE_ROLE_MARKER_RE.search(text):
            return True
        return False
    if isinstance(value, dict):
        return any(
            contains_injection(key) or contains_injection(item) for key, item in value.items()
        )
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return any(contains_injection(item) for item in value)
    return False


__all__ = [
    "CanonicalizationLimitExceeded",
    "INJECTION_PATTERN_SET_VERSION",
    "MAX_REQUEST_CONTENT_SCAN_CHARS",
    "canonicalize_for_matching",
    "contains_injection",
]
