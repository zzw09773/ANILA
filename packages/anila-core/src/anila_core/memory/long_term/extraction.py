"""Fact extraction prompt + tolerant response parser.

Pure functions. No HTTP / DB. The CSP backend wires these up to the
platform LLM client and writes results via :class:`MemoryAdapter`.

The prompt's example block uses abstract ``<placeholder>`` text on
purpose — earlier iterations used realistic example values like
姓名/龔修穎 and small models started parroting them as if they were
real extractions. The placeholder + an explicit "do not echo
placeholders" instruction reproduces best across our local 4–5B
models without a JSON-schema-constrained decoder.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)


EXTRACTION_SYSTEM_PROMPT = """你是事實萃取器。從以下對話片段抽出穩定的個人事實
（範例類別：姓名、職稱、長期偏好、長期目標、工作範圍）。
忽略短期狀態（當下情緒、今日天氣、剛才的問題）。

回傳 JSON 陣列，每個物件包含 key/value/confidence。
若無事實可萃，回 [] 空陣列。

範例輸出格式（key 與 value 都是抽象佔位，請以實際抽取的內容替換；
絕對不要在輸出中保留 `<...>` 佔位符或範例文字本身）：
[
  {"key": "<fact_category>", "value": "<concrete_value>", "confidence": <0.0-1.0>}
]

只輸出 JSON，不要前言、不要解釋、不要 ```json 代碼塊。"""


# Allow extractor LLMs that emit ``<think>...</think>`` reasoning
# preambles before the JSON. The greedy ``\[.*\]`` with DOTALL grabs
# the first complete-looking JSON array; downstream validation
# rejects malformed entries.
_JSON_ARRAY_RE = re.compile(r"\[.*\]", re.DOTALL)


def format_transcript_for_extraction(
    user_message: str,
    assistant_message: str,
) -> str:
    """Compose the extractor's input from one Q&A turn.

    Same shape both Phase 1 (CSP local) and Phase 2 (anila-core
    cutover) feed the LLM, so any prompt tuning lands here once.
    """
    return f"使用者：{user_message}\n\n助理：{assistant_message}"


MEMORY_REFRESH_SYSTEM_PROMPT = """你是記憶整理器。讀完對話後只輸出一個 JSON 物件，不要前言。

summary：這段對話裡使用者想做什麼、最後決定或結論是什麼。
不要逐字抄寫助理的回答，不要寫路徑、主機、設定名稱或錯誤訊息。
facts：只有使用者親口講的、關於自己的穩定事實或偏好（單位、職稱、長期偏好）。
助理說的話不是事實。短期狀態不要收。沒有就給空陣列。

格式（尖括號是佔位，不要原樣輸出）：
{"summary":"<短摘要>","facts":[{"key":"<類別>","value":"<使用者原話裡的內容>","confidence":0.0}]}
"""


def parse_memory_refresh_response(raw: str) -> dict[str, Any]:
    """把整理模型的輸出收成 ``{"summary", "facts"}``。壞掉就當這次沒有產出。"""
    empty = {"summary": "", "facts": []}
    if not isinstance(raw, str) or not raw.strip():
        return empty
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end <= start:
        return empty
    try:
        parsed = json.loads(raw[start : end + 1])
    except json.JSONDecodeError:
        logger.warning(
            "anila_core.memory.user: refresh returned non-JSON: %r",
            raw[:200],
        )
        return empty
    if not isinstance(parsed, dict):
        return empty
    summary = parsed.get("summary")
    if not isinstance(summary, str):
        summary = ""
    summary = summary.strip()
    facts_raw = parsed.get("facts")
    if not isinstance(facts_raw, list):
        facts_raw = []
    # 與逐輪萃取同一套欄位檢查，佔位符與超長 key 都在這裡丟掉。
    facts = parse_extraction_response(json.dumps(facts_raw, ensure_ascii=False))
    return {"summary": summary, "facts": facts}


def parse_extraction_response(raw: str) -> list[dict[str, Any]]:
    """Tolerant parser for the extractor LLM output.

    Validation rules (all must pass for an item to survive):

    * Top level must be a JSON array (after extracting it from any
      reasoning preamble).
    * Each item must be a dict with string ``key`` and string
      ``value``. Items missing those are silently dropped.
    * Items whose ``key`` or ``value`` starts with ``<`` are dropped
      — the model echoed the placeholder template back.
    * ``confidence`` is parsed as float, clamped to ``[0.0, 1.0]``,
      defaults to 1.0 when missing or non-numeric.
    * ``key`` is truncated to 120 chars (the storage column width).
    """
    match = _JSON_ARRAY_RE.search(raw)
    if not match:
        return []
    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError:
        logger.warning(
            "anila_core.memory.user: extractor returned non-JSON: %r",
            raw[:200],
        )
        return []
    if not isinstance(parsed, list):
        return []

    valid: list[dict[str, Any]] = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        key = item.get("key")
        value = item.get("value")
        if not isinstance(key, str) or not isinstance(value, str):
            continue
        if key.startswith("<") or value.startswith("<"):
            continue
        key = key.strip()[:120]
        value = value.strip()
        if not key or not value:
            continue
        confidence = item.get("confidence", 1.0)
        try:
            conf_f = float(confidence)
        except (TypeError, ValueError):
            conf_f = 1.0
        conf_f = max(0.0, min(1.0, conf_f))
        valid.append({"key": key, "value": value, "confidence": conf_f})
    return valid
