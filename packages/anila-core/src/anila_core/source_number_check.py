"""回答裡的數字、金額、天數、條號，對照送進模型的來源片段。

比對是程式做的。呼叫端要用這個結果蓋掉模型自己帶的 ``source_check``，
不能把模型的宣告當成核對結果。這裡不查翻譯、否定詞或單位換算。
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal

from anila_core.ingestion.citation_extractor import cjk_to_int

SOURCE_MISMATCH_MARKER = "這段對不上來源，以原文卡片為準"

_MAX_CHARS = 100_000
_MAX_CLAIMS = 80
_MAX_UNMATCHED = 12
_MAX_SOURCE_ARTICLES = 120
_MAX_SOURCE_NUMBERS = 400

_CJK_NUM = r"[〇零一二三四五六七八九十百千兩廿卅]"
_NUM = rf"(?:[0-9]+(?:\.[0-9]+)?|{_CJK_NUM}+)"
_MONEY_NUM = rf"(?:[0-9]+(?:\.[0-9]+)?|[〇零一二三四五六七八九十百千兩廿卅萬億]+)"

_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`[^`\n]{0,200}`")
_CITE_RE = re.compile(r"\[(?:\d{1,3})\]")
_LIST_RE = re.compile(
    r"(?m)^[ \t]{0,3}(?:\d{1,3}|[一二三四五六七八九十]{1,3})[.)、．]\s+"
)
_PROTOCOL_RE = re.compile(
    r"(?m)^[ \t]{0,3}(?:ROUND|DISPATCH|RECALL|ASK\*|ASK)\s*:.*$"
)
_DATE_RE = re.compile(
    r"(?P<y>\d{2,4})\s*年\s*(?P<mo>\d{1,2})\s*月\s*(?P<d>\d{1,2})\s*日"
)
_ART_RE = re.compile(
    rf"第\s*(?P<n>{_NUM})\s*(?P<unit>條|項|款|目|點)"
    rf"(?:\s*之\s*(?P<suf>{_NUM}))?"
)
_CUR_RE = re.compile(
    rf"(?:新臺幣|新台幣|臺幣|台幣|NT\$|\$)\s*(?P<n>{_MONEY_NUM})"
    rf"(?:\s*(?P<scale>萬|億))?(?:\s*(?:元|圓))?"
)
_YUAN_RE = re.compile(
    rf"(?P<n>{_MONEY_NUM})\s*(?P<scale>萬|億)?\s*(?:元|圓)"
)
_DAYS_RE = re.compile(
    rf"(?P<n>{_NUM})\s*(?:個)?\s*(?:工作日|日曆天|日曆日|工作天|天|日)"
)
_PCT_WORD_RE = re.compile(rf"百分之\s*(?P<n>{_NUM})")
_PCT_SIGN_RE = re.compile(r"(?P<n>[0-9]+(?:\.[0-9]+)?)\s*%")
_CJK_COUNT_RE = re.compile(
    rf"(?P<n>{_CJK_NUM}+)\s*(?:個)?\s*"
    r"(?P<unit>小時|個月|週|周|年|月|人|名|位|次|倍|件|份|歲)"
)
_ARABIC_RE = re.compile(r"\d+(?:\.\d+)?")
_CJK_BARE_RE = re.compile(rf"{_CJK_NUM}{{2,8}}")
_THOUSANDS_RE = re.compile(r"(?<=\d),(?=\d{3}(?!\d))")


@dataclass(frozen=True)
class _Claim:
    kind: str
    text: str
    value: str

    def as_dict(self) -> dict:
        return {"kind": self.kind, "text": self.text, "value": self.value}


@dataclass
class _Index:
    articles: set[str]
    numbers: set[str]


def _normalize(text: str) -> str:
    raw = text or ""
    if len(raw) > _MAX_CHARS:
        raw = raw[:_MAX_CHARS]
    folded = unicodedata.normalize("NFKC", raw).replace("，", ",").replace("﹐", ",")
    previous = None
    while previous != folded:
        previous = folded
        folded = _THOUSANDS_RE.sub("", folded)
    return folded


def _blank(match: re.Match) -> str:
    return " " * (match.end() - match.start())


def _mask_answer(text: str) -> str:
    """排版與協定行不是在引用來源。數字只從剩下的回答裡抽。"""
    masked = _FENCE_RE.sub(_blank, text)
    masked = _INLINE_CODE_RE.sub(_blank, masked)
    masked = _CITE_RE.sub(_blank, masked)
    masked = _LIST_RE.sub(_blank, masked)
    return _PROTOCOL_RE.sub(_blank, masked)


def _parse_cjk_scaled(text: str) -> int | None:
    """一萬五千、三十萬、三億這類帶萬／億的中文數目。"""
    total = 0
    rest = text
    for unit, scale in (("億", 100_000_000), ("萬", 10_000)):
        if unit not in rest:
            continue
        left, rest = rest.split(unit, 1)
        if left == "":
            count = 1
        else:
            count = cjk_to_int(left)
            if count is None:
                return None
        total += int(count) * scale
    if rest:
        tail = cjk_to_int(rest)
        if tail is None:
            return None
        total += tail
    return total


_CJK_DIGIT_CHAR = {
    "〇": "0", "零": "0", "一": "1", "二": "2", "三": "3", "四": "4",
    "五": "5", "六": "6", "七": "7", "八": "8", "九": "9",
}


def _digit_run(text: str) -> str | None:
    """二〇二四、一一三這種逐字數字。有十／百／千的仍走位值解析。"""
    if len(text) < 2 or any(char not in _CJK_DIGIT_CHAR for char in text):
        return None
    return "".join(_CJK_DIGIT_CHAR[char] for char in text)


def _canonical_number(raw: str | None, scale: str | None = None) -> str | None:
    if not raw:
        return None
    text = raw.strip().replace(",", "")
    if not text:
        return None
    if any(char in text for char in "萬億"):
        parsed = _parse_cjk_scaled(text)
        if parsed is None:
            return None
        value = Decimal(parsed)
        scale = None
    elif re.fullmatch(r"\d+(?:\.\d+)?", text):
        value = Decimal(text)
    else:
        digits = _digit_run(text)
        if digits is not None:
            value = Decimal(digits)
        else:
            parsed = cjk_to_int(text)
            if parsed is None:
                return None
            value = Decimal(parsed)
    if scale == "萬":
        value *= Decimal(10000)
    elif scale == "億":
        value *= Decimal(100000000)
    if value == value.to_integral_value():
        return str(value.to_integral_value())
    rendered = format(value.normalize(), "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or None


def _article_key(unit: str, number: str, suffix: str | None) -> str:
    if suffix:
        return f"{unit}:{number}之{suffix}"
    return f"{unit}:{number}"


def _overlaps(spans: list[tuple[int, int]], start: int, end: int) -> bool:
    return any(not (end <= span_start or start >= span_end) for span_start, span_end in spans)


def _add_claim(
    claims: list[_Claim],
    spans: list[tuple[int, int]],
    index: _Index | None,
    *,
    kind: str,
    text: str,
    value: str | None,
    start: int,
    end: int,
    extra_numbers: tuple[str, ...] = (),
) -> None:
    if not value or _overlaps(spans, start, end):
        return
    spans.append((start, end))
    claims.append(_Claim(kind, text.strip(), value))
    if index is None:
        return
    if kind == "article":
        index.articles.add(value)
    else:
        index.numbers.add(value)
    for number in extra_numbers:
        index.numbers.add(number)


def _extract(text: str, *, mask: bool, index: _Index | None) -> list[_Claim]:
    body = _mask_answer(text) if mask else text
    claims: list[_Claim] = []
    spans: list[tuple[int, int]] = []

    for match in _DATE_RE.finditer(body):
        for group in ("y", "mo", "d"):
            value = _canonical_number(match.group(group))
            piece = match.group(group)
            start = match.start(group)
            _add_claim(
                claims, spans, index,
                kind="number", text=piece, value=value,
                start=start, end=start + len(piece),
            )

    for match in _ART_RE.finditer(body):
        number = _canonical_number(match.group("n"))
        suffix = _canonical_number(match.group("suf")) if match.group("suf") else None
        if not number:
            continue
        key = _article_key(match.group("unit"), number, suffix)
        extras = (number,) if index is not None else ()
        if suffix and index is not None:
            extras = (number, suffix)
        _add_claim(
            claims, spans, index,
            kind="article", text=match.group(0), value=key,
            start=match.start(), end=match.end(),
            extra_numbers=extras,
        )

    for pattern, kind in ((_CUR_RE, "amount"), (_YUAN_RE, "amount")):
        for match in pattern.finditer(body):
            value = _canonical_number(match.group("n"), match.group("scale"))
            _add_claim(
                claims, spans, index,
                kind=kind, text=match.group(0), value=value,
                start=match.start(), end=match.end(),
            )

    for match in _DAYS_RE.finditer(body):
        value = _canonical_number(match.group("n"))
        _add_claim(
            claims, spans, index,
            kind="days", text=match.group(0), value=value,
            start=match.start(), end=match.end(),
        )

    for pattern in (_PCT_WORD_RE, _PCT_SIGN_RE):
        for match in pattern.finditer(body):
            value = _canonical_number(match.group("n"))
            _add_claim(
                claims, spans, index,
                kind="number", text=match.group(0), value=value,
                start=match.start(), end=match.end(),
            )

    for match in _CJK_COUNT_RE.finditer(body):
        value = _canonical_number(match.group("n"))
        _add_claim(
            claims, spans, index,
            kind="number", text=match.group(0), value=value,
            start=match.start(), end=match.end(),
        )

    for match in _ARABIC_RE.finditer(body):
        value = _canonical_number(match.group(0))
        _add_claim(
            claims, spans, index,
            kind="number", text=match.group(0), value=value,
            start=match.start(), end=match.end(),
        )

    if index is not None and not mask:
        for match in _CJK_BARE_RE.finditer(body):
            if _overlaps(spans, match.start(), match.end()):
                continue
            value = _canonical_number(match.group(0))
            if value:
                index.numbers.add(value)
    return claims


def _index_sources(sources: list[str]) -> _Index:
    index = _Index(set(), set())
    for source in sources:
        if not isinstance(source, str) or not source.strip():
            continue
        _extract(_normalize(source), mask=False, index=index)
    return index


def _dedupe(claims: list[_Claim]) -> list[_Claim]:
    seen: set[tuple[str, str]] = set()
    kept: list[_Claim] = []
    for claim in claims:
        key = (claim.kind, claim.value)
        if key in seen:
            continue
        seen.add(key)
        kept.append(claim)
    return kept


def _matches(claim: _Claim, index: _Index) -> bool:
    if claim.kind == "article":
        return claim.value in index.articles
    return claim.value in index.numbers


def _public_claims(claims: list[_Claim]) -> list[dict]:
    return [claim.as_dict() for claim in claims[:_MAX_CLAIMS]]


def _sort_number(value: str) -> tuple:
    try:
        return (0, Decimal(value))
    except Exception:
        return (1, value)


def _result(
    *,
    checked: bool,
    claims: list[_Claim],
    index: _Index | None,
    reason: str | None = None,
) -> dict:
    unmatched = [claim for claim in claims if index is None or not _matches(claim, index)]
    if not checked:
        unmatched = []
    mismatch = bool(checked and unmatched)
    # 對不上的放前面，截斷時才不會把幻覺數字先丟掉。
    ordered = unmatched + [claim for claim in claims if claim not in unmatched]
    payload = {
        "checked": checked,
        "mismatch": mismatch,
        "unmatched": [claim.as_dict() for claim in unmatched[:_MAX_UNMATCHED]],
        "claims": _public_claims(ordered),
        "source_articles": sorted(index.articles)[:_MAX_SOURCE_ARTICLES] if index else [],
        "source_numbers": sorted(index.numbers, key=_sort_number)[:_MAX_SOURCE_NUMBERS] if index else [],
    }
    if len(unmatched) > _MAX_UNMATCHED:
        payload["unmatched_truncated"] = True
    if mismatch:
        payload["marker"] = SOURCE_MISMATCH_MARKER
    if reason and not checked:
        payload["reason"] = reason
    return payload


def check_answer_against_sources(answer: str | None, sources: list[str] | None) -> dict:
    """回傳要寫進 message metadata 的 ``source_check``。

    沒有來源片段時不標「對不上」：沒有原文卡片，這句話無從對照。
    有來源時，回答裡抽到的數字、金額、天數或條號只要沒出現在片段裡就標。
    """
    passages = [item for item in (sources or []) if isinstance(item, str) and item.strip()]
    claims = _dedupe(_extract(_normalize(answer or ""), mask=True, index=None))
    if not passages:
        return _result(checked=False, claims=claims, index=None, reason="no_sources")
    index = _index_sources(passages)
    return _result(checked=True, claims=claims, index=index)


def _claims_from_payload(payload: dict) -> list[_Claim]:
    raw = payload.get("claims")
    if not isinstance(raw, list) or not raw:
        raw = payload.get("unmatched")
    claims: list[_Claim] = []
    if not isinstance(raw, list):
        return claims
    for item in raw:
        if not isinstance(item, dict):
            continue
        kind = item.get("kind")
        text = item.get("text")
        value = item.get("value")
        if not isinstance(kind, str) or not isinstance(value, str):
            continue
        claims.append(_Claim(kind, text if isinstance(text, str) else value, value))
    return claims


def _index_from_payloads(payloads: list[dict], extra_texts: list[str] | None) -> _Index:
    index = _Index(set(), set())
    for payload in payloads:
        for article in payload.get("source_articles") or []:
            if isinstance(article, str) and article:
                index.articles.add(article)
        for number in payload.get("source_numbers") or []:
            if isinstance(number, str) and number:
                index.numbers.add(number)
    if extra_texts:
        extra = _index_sources(extra_texts)
        index.articles.update(extra.articles)
        index.numbers.update(extra.numbers)
    return index


def reconcile_source_checks(
    prior,
    newer,
    extra_texts: list[str] | None = None,
) -> dict:
    """多輪合併：各輪的數字用全部來源再對一次。

    後一輪的 metadata 不能把前一輪已對過的來源蓋掉，也不能讓模型
    在其中一輪自己寫 ``mismatch: false``。
    """
    payloads = [item for item in (prior, newer) if isinstance(item, dict)]
    texts = [item for item in (extra_texts or []) if isinstance(item, str) and item.strip()]
    if not payloads:
        return check_answer_against_sources("", texts)
    claims = _dedupe(_claims_from_payloads_many(payloads))
    had_sources = any(item.get("checked") is True for item in payloads)
    index = _index_from_payloads(payloads, texts)
    if index.articles or index.numbers:
        had_sources = True
    if not had_sources:
        return _result(checked=False, claims=claims, index=None, reason="no_sources")
    return _result(checked=True, claims=claims, index=index)


def _claims_from_payloads_many(payloads: list[dict]) -> list[_Claim]:
    claims: list[_Claim] = []
    for payload in payloads:
        claims.extend(_claims_from_payload(payload))
    return claims
