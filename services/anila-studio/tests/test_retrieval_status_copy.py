"""Tripwire on the retrieval-failure copy: it may not claim a search ran.

The pipeline tests compare ``status.warning == RETRIEVAL_FAILED_WARNING``,
which is self-referential: rewrite the constant back into the very lie
this package removed — 「本次未檢索到相關段落」— and they all stay green.
This file exists so that edit goes red.

What it checks
--------------
One thing, in one direction: the copy must not contain a phrasing that
**claims the retrieval ran** — completed, completed-but-empty, returned
zero, or found nothing matching. There is no requirement that the copy
say anything in particular.

That asymmetry is the whole design, and it was arrived at the hard way.
Earlier versions also required the text to *state a failure*, matching an
enumerated failure vocabulary. Measured over four acceptance rounds, that
positive half caught nothing the negative half did not already catch, and
it was responsible for **every** false rejection — including 「很抱歉，
這次搜尋知識庫時出了錯…」, rejected because the particle 了 split 出錯.
Widening the vocabulary to fix each false rejection made the rule looser
without making it more capable, and reopened holes elsewhere. It is gone.
An enumerated rule cannot decide whether a sentence is honest; it can
only spot a handful of sentences that are not.

How far this goes — read before trusting it
--------------------------------------------
Not far, and the residual gap is wide. What it is good for: **pasting any
of the four historical copy versions back into either constant makes
something go red** (``_HISTORICAL`` pins exactly that). Beyond that it is
best-effort, and these bypasses are known and open:

  * **Punctuation.** ``_SAME_CLAUSE`` treats 。，、；： as boundaries, so a
    comma between the retrieval word and the completion word defeats the
    completion pattern.
  * **Our own escape hatch.** The ``(?![沒未無不非])`` lookahead exists so
    our honest 「搜尋沒有成功執行」 is not misread as a success claim.
    「向量搜尋無誤地完成了」 walks in through exactly that door, and is
    listed in ``_KNOWN_LEAKS`` rather than quietly omitted.
  * **Vocabulary outside the list.** The patterns know 檢索/搜尋/查詢/比對;
    they do not know 查閱, 調閱, or whatever the next person writes.
  * **Everything nobody enumerated.** Which is most of the language.

So this file cannot tell you the copy is honest — only that these
particular wordings did not come back. Whoever edits the copy still has
to read it. The behaviour (a failed retrieval reaching the prompt and the
job status *as a failure*) is guarded by
``test_retrieval_failure_declared.py``, which exercises the pipeline
rather than the prose. That is the load-bearing test; this is a tripwire.

Why it is written to under-reject
----------------------------------
A guard that rejects true sentences gets deleted, and deleting it would
be the correct response: this project has already lost 25 days of work to
a system that kept getting stricter until nobody wanted to maintain it.
So the rule only ever says what the copy may not *claim*. Across the two
honest corpora below (ours, and the phrasings the acceptance passes wrote
independently) it currently rejects none of them — a measurement on those
sentences, not a proof about sentences nobody has written yet.
"""
from __future__ import annotations

import re

import pytest

from app.services.retrieval_status import (
    RETRIEVAL_FAILED_PROMPT_NOTE,
    RETRIEVAL_FAILED_WARNING,
)


# Clause boundary. Brackets are deliberately NOT boundaries.
_SAME_CLAUSE = r"[^。，,、；;：:]"

# Phrasings that assert the search RAN. None may appear. This is the
# entire rule — there is no positive counterpart.
_FALSE_CLAIMS = (
    # 未／沒有／無 … 檢索到／搜尋到／查到／命中／找到
    re.compile(
        rf"(未|沒有?|無){_SAME_CLAUSE}{{0,8}}(檢索到|搜尋到|搜索到|查到|命中|找到)"
    ),
    # 無／沒有 … 檢索結果／搜尋結果／查詢結果
    re.compile(rf"(無|沒有){_SAME_CLAUSE}{{0,4}}(檢索|搜尋|查詢)結果"),
    # 檢索/搜尋/比對 … 完成／成功／順利／結束，且中間沒有否定詞
    # （否則我們自己那句誠實的「搜尋沒有成功執行」會被誤判）
    re.compile(
        rf"(檢索|搜尋|搜索|查詢|查找|比對)(?:(?![沒未無不非]){_SAME_CLAUSE}){{0,6}}"
        rf"(完成|完畢|成功|順利|已執行|執行完|跑完|結束|回傳)"
    ),
    # 反向語序：已完成/執行了 … 檢索/搜尋/比對
    re.compile(
        rf"(已完成|完成了|已執行|執行了|跑完了){_SAME_CLAUSE}{{0,6}}"
        rf"(檢索|搜尋|搜索|查詢|比對)"
    ),
    # 命中 0 筆 / 段落數為零
    re.compile(rf"(0|零)\s*{_SAME_CLAUSE}{{0,2}}(筆|個|則|項|條)"),
    re.compile(rf"(數|量|數量){_SAME_CLAUSE}{{0,2}}(為|是|＝|=)\s*(0|零)"),
    # 結果為空／空集合／空清單
    re.compile(r"(結果|清單|列表|集合)[^。]{0,4}(為空|是空|空集合|空清單|空列表)"),
    re.compile(r"(回傳|返回|得到)[^。]{0,4}(空集合|空清單|空列表|空結果)"),
    # 「查無相符」型:宣稱語料庫裡沒有東西,等於宣稱搜尋跑過了。
    #
    # ⚠ 不要改成拿「段落」去擴充上面第一條 —— 那會把誠實的
    # 「本次未取用任何文件段落」一起擋掉(實測:那種寫法 hit,本條 miss)。
    # 要打的是「查無/未發現/並無」撞上「相符/相關」這個語意,不是「段落」。
    re.compile(
        rf"(查無|查不到|未發現|並無|均無|皆無){_SAME_CLAUSE}{{0,4}}"
        rf"(相符|相關|符合|對應|可用|適用)"
    ),
)


def copy_problems(text: str) -> list[str]:
    """Return every false claim ``text`` makes about the retrieval running."""
    problems: list[str] = []
    for pattern in _FALSE_CLAIMS:
        match = pattern.search(text)
        if match is not None:
            problems.append(f"宣稱搜尋跑過了: {match.group(0)!r}")
    return problems


_TEXTS = {
    "RETRIEVAL_FAILED_WARNING": RETRIEVAL_FAILED_WARNING,
    "RETRIEVAL_FAILED_PROMPT_NOTE": RETRIEVAL_FAILED_PROMPT_NOTE.format(
        where="speaker_notes"
    ),
}


@pytest.mark.parametrize("name", sorted(_TEXTS))
def test_shipped_copy_makes_no_false_claim(name: str) -> None:
    problems = copy_problems(_TEXTS[name])
    assert not problems, f"{name}: {problems} — {_TEXTS[name]!r}"


@pytest.mark.parametrize("name", sorted(_TEXTS))
def test_shipped_copy_is_not_empty(name: str) -> None:
    """The rule only forbids; an empty string would satisfy it vacuously."""
    assert len(_TEXTS[name].strip()) >= 10


# The four wordings this package replaced — one per site. Pasting any of
# them back is the regression this file exists to catch, and it is the
# only thing it promises to catch.
_HISTORICAL = {
    "slides": (
        "（本次未檢索到相關段落；請依使用者輸入直接發揮，"
        "並在末尾的 speaker_notes 內提醒「本草稿未取得文件支撐」。）"
    ),
    "infographic": (
        "（本次未檢索到相關段落；請依使用者輸入直接發揮，"
        "並在 takeaway 中提醒「本草稿未取得文件支撐」。）"
    ),
    "datatable": (
        "（無檢索結果 — 你可以基於 collection 名稱 + preset 給出合理的"
        "空白範本,並在 notes 註明資料來源不足。）"
    ),
    "anilalm_chat": "本次查詢在向量檢索中沒有命中相似度 ≥ 0.3 的段落。",
}


@pytest.mark.parametrize("site", sorted(_HISTORICAL))
def test_historical_copy_is_rejected(site: str) -> None:
    assert copy_problems(_HISTORICAL[site]), (
        f"{site} 的舊文案貼回來竟然過關: {_HISTORICAL[site]!r}"
    )


# Written from the shape of the attack — "assert the search completed" —
# rather than from a list of known-passing phrasings. The third entry is
# the sentence quoted in the round-3 acceptance report; the rest are ours.
_ADVERSARIAL = [
    "本次未檢索到相關段落，請依使用者輸入直接發揮。",
    "本次查詢在知識庫中查無相符的段落，請調整問題後再試。",
    "知識庫查詢已完成，但相符的段落數為零；由於無法引用文件，以下內容僅供參考。",
    "向量搜尋順利執行，未達相似度門檻；因無法取得佐證，回答僅供參考。",
    "已搜尋知識庫，沒有命中任何段落；本次回答無法引用文件。",
    "檢索已結束，未取得可用段落；由於系統無法確認來源，請自行查證。",
    "知識庫沒有找到相關內容，模型未能提供佐證。",
    "本次檢索結果為空，內容未經文件佐證。",
    "查詢流程正常結束，惟知識庫中無對應段落，故本回覆未能引用文件。",
    "系統已完成向量比對，命中 0 筆；以下內容未取得文件支撐。",
    "本次搜尋未命中任何段落，請重試。",
    "知識庫查詢完畢，無相符內容，回答僅依模型知識。",
    "搜尋執行成功但結果為空集合；由於無法佐證，請謹慎採用。",
    "檢索管線回傳空清單，未能提供引用。",
]


@pytest.mark.parametrize("text", _ADVERSARIAL)
def test_adversarial_paraphrases_are_rejected(text: str) -> None:
    assert copy_problems(text), f"這句話宣稱搜尋跑完了,守衛卻放行: {text!r}"


# Named, not hidden. These claim a completed search and the rule lets them
# through; they are here so nobody has to rediscover the gap, and so the
# docstring's bypass list stays honest. Do NOT "fix" them by bolting on a
# literal for each — that is how a tripwire turns into a checklist.
_KNOWN_LEAKS = [
    "向量搜尋無誤地完成了，但沒有可用的內容。",   # (?![沒未無不非]) escape hatch
    "檢索。完成了，未取得任何內容。",              # punctuation boundary
    "已查閱知識庫，未取得任何內容。",              # 查閱 outside the vocabulary
]


@pytest.mark.parametrize("text", _KNOWN_LEAKS)
def test_known_leaks_stay_documented(text: str) -> None:
    """Fails if a leak silently closes — then update the docstring."""
    assert not copy_problems(text), (
        f"這句以前是已知漏網,現在被擋住了 —— 請更新 docstring 的繞法清單: {text!r}"
    )


# Honest sentences that must be ACCEPTED. Two corpora on purpose: ours,
# and the phrasings the acceptance passes wrote independently (marked).
# The independent set is what caught the earlier rule matching our own
# vocabulary back at us.
_HONEST_OURS = [
    RETRIEVAL_FAILED_WARNING,
    _TEXTS["RETRIEVAL_FAILED_PROMPT_NOTE"],
    "向量檢索服務目前不可用，本則回答未參照任何文件。",
    "這次沒能連上知識庫（服務重啟中），以下回答沒有文件依據。",
    "知識庫查閱作業中途中止，內容僅依模型既有知識，請自行查證。",
    "系統暫時無法讀取您的資料庫，本次回覆未經文件佐證。",
    "本次回答沒有引用文件——調閱知識庫時發生錯誤。",
    "抱歉，知識庫這邊出了點狀況（連線失敗），這則回答不是根據您的文件寫的。",
    "本次問答未使用知識庫段落，因為系統無法在時限內完成檢索。",
]

# Written by the acceptance passes, quoted from their reports. Rounds 3
# and 4 wrongly rejected the first four; round 5 the fifth and sixth.
_HONEST_ACCEPTANCE = [
    "檢索逾時",
    "檢索服務暫時無回應",
    "知識庫連線異常",
    "本次未能取得知識庫資料",
    "很抱歉，這次搜尋知識庫時出了錯，以下回答沒有引用您的文件。",
    "本次未取用任何文件段落",
]


@pytest.mark.parametrize("text", _HONEST_OURS + _HONEST_ACCEPTANCE)
def test_honest_phrasings_are_accepted(text: str) -> None:
    problems = copy_problems(text)
    assert not problems, (
        f"誠實的寫法被誤判 —— 該改的是守衛,不是這句話: {text!r} → {problems}"
    )


def test_prompt_note_keeps_its_placement_placeholder() -> None:
    """Each pipeline fills ``{where}`` with its own disclosure field."""
    assert "{where}" in RETRIEVAL_FAILED_PROMPT_NOTE
    assert "speaker_notes" in RETRIEVAL_FAILED_PROMPT_NOTE.format(
        where="speaker_notes"
    )


def test_copy_carries_no_interpolation_slot_for_runtime_detail() -> None:
    """The copy must stay a constant, not become a format string.

    Narrow on purpose. These two values are literals, so an IP or a
    traceback cannot structurally reach them today — the runtime leak is
    covered by the pipeline tests, which assert the exception text never
    reaches the prompt or the job warning. What this catches is the one
    edit that would change that: turning the copy into a template that
    splices the exception in. It does NOT check the runtime path.
    """
    assert "{" not in RETRIEVAL_FAILED_WARNING
    slots = set(re.findall(r"\{(\w*)\}", RETRIEVAL_FAILED_PROMPT_NOTE))
    assert slots == {"where"}, f"提示詞多了可插值的欄位: {slots}"
