"""疑似拒答樣式偵測（只量測，不擋輸出）。

設計姿態（false-positive stance）
================================
**保守：寧可漏報，不要誤報。**

院內合法國防／技術 QA 被模型過度拒答是真實風險（見
``docs/designs/ncsist-prompt-localization-and-harness.md`` §6-9），但
「資料中查無／段落不足」這類 grounding 誠實回答**不是**拒答——若把它們
算進去，監測面板會充滿噪音，反而沒人看。

因此本偵測器只用一小撮高置信的雙語固定片語／窄 regex；不掃「抱歉」、
不掃「無法確定」、不掃一般否定句。漏掉模糊拒答可以接受；把正常回答
標成拒答不可接受。

掛接方式：呼叫端在推論稽核寫入路徑上取 ``looks_like_refusal(text)``，
把布林寫進既有 metadata（log-only）。本模組本身無 I/O、不 raise。
"""

from __future__ import annotations

import re

# 高置信片語（子字串比對，大小寫不敏感的英文另處理）。
_LITERAL_ZH = (
    "我無法協助",
    "我不能提供",
    "我無法提供",
    "我不能協助",
    "恕無法協助",
)
_LITERAL_EN = (
    "cannot assist",
    "can't assist",
    "cannot help with",
    "can't help with",
    "as an ai",
    "as an artificial intelligence",
    "i'm not able to help",
    "i am not able to help",
)

# 窄 regex：中文「違反…政策」須貼近拒答動詞，避免合規說明誤報。
# 英文維持原形（常見拒答開場；未見同等合規說明誤報）。
_ZH_REFUSAL_VERB = r"(?:無法|不能|恕難|礙難|拒絕)"
_REGEX = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        # 拒答動詞在「違反…政策」前後短窗內（≤24 字）才算。
        rf"(?:{_ZH_REFUSAL_VERB}.{{0,24}}違反.{{0,12}}政策|違反.{{0,12}}政策.{{0,24}}{_ZH_REFUSAL_VERB})",
        r"violat(?:es|ing)?\b.{0,40}\bpolicy",
        r"against\s+(?:my|our|the)\s+(?:usage\s+)?policy",
    )
)


def looks_like_refusal(text: str | None) -> bool:
    """若文字呈現典型安全拒答樣式則 True；``None``／空 → False。"""
    if not text or not str(text).strip():
        return False
    sample = str(text)
    # 只看前段：拒答幾乎都開場表明；長文中段的「無法」多半是 grounding。
    head = sample[:800]
    head_lower = head.lower()

    for phrase in _LITERAL_ZH:
        if phrase in head:
            return True
    for phrase in _LITERAL_EN:
        if phrase in head_lower:
            return True
    for rx in _REGEX:
        if rx.search(head):
            return True
    return False
