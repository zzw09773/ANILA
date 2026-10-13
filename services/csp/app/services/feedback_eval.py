"""把「已修好」的原問句轉成一筆 golden-set 回歸題。

只產生一行。不寫入 tools/eval/golden_set.jsonl，也不擴充題庫。
"""
from __future__ import annotations

import json

DEFAULT_CHECKS = ["not_refusal", "no_simplified", "no_prc_phrases"]


def build_regression_case(*, message_id: int, prompt: str) -> dict:
    if isinstance(message_id, bool) or not isinstance(message_id, int) or message_id < 1:
        raise ValueError("message_id 必須是正整數")
    text = (prompt or "").strip()
    if not text:
        raise ValueError("原問句是空的，不能轉成回歸題")
    return {
        "id": f"fb-{message_id}",
        "category": "feedback-regression",
        "prompt": text,
        "system_mode": "preamble",
        "checks": list(DEFAULT_CHECKS),
        "note": "FROM_CLOSED_FEEDBACK",
    }


def case_to_jsonl(case: dict) -> str:
    line = json.dumps(case, ensure_ascii=False, separators=(", ", ": "))
    if "\n" in line or "\r" in line:
        raise ValueError("回歸題必須是單行")
    return line
