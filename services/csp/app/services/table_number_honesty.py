"""標示回答裡從解析後試算表讀來的數字。

xlsx / csv / ods（以及同性質的 xls、tsv）目前只把抽出的文字送進模型，
沒有用程式計算儲存格。若回答引用了那些文字裡的數字，由這支程式在
metadata 掛上旗標；畫面上的句子由前端常數決定，不採用模型寫的字。

這裡不做計算、不對儲存格、不讀取試算表公式的結果。
"""
from __future__ import annotations

import json
import re

TABLE_NUMBER_HONESTY_KEY = "table_number_honesty"

# 試算表副檔名。送進模型的是解析後的文字，不是算過的儲存格。
_SPREADSHEET_SUFFIXES = frozenset({".csv", ".tsv", ".xls", ".xlsx", ".ods"})

_FULLWIDTH = str.maketrans({
    "０": "0",
    "１": "1",
    "２": "2",
    "３": "3",
    "４": "4",
    "５": "5",
    "６": "6",
    "７": "7",
    "８": "8",
    "９": "9",
    "，": ",",
    "．": ".",
    "＋": "+",
    "－": "-",
    "−": "-",
    "﹣": "-",
})

# 不把數字從更長的識別碼裡切出來（A12、v2）。中文緊貼數字仍算一筆。
_NUMBER = re.compile(
    r"(?<![A-Za-z0-9_])"
    r"([+＋\-－−﹣]?"
    r"(?:[0-9０-９]{1,3}(?:[,，][0-9０-９]{3})+|[0-9０-９]+)"
    r"(?:[.．][0-9０-９]+)?)"
    r"(?![A-Za-z0-9_])"
)


def is_spreadsheet_filename(filename: str | None) -> bool:
    """只認試算表副檔名。檔名其餘部分不當指示、也不參與比對。"""
    if not isinstance(filename, str):
        return False
    name = filename.replace("\\", "/").rsplit("/", 1)[-1].strip()
    dot = name.rfind(".")
    if dot <= 0:
        return False
    return name[dot:].lower() in _SPREADSHEET_SUFFIXES


def spreadsheet_bodies(views, admitted_ids: set[int]) -> list[str]:
    """已納入這次回答、且是試算表的抽出文字。沒送進模型的不算。"""
    if not admitted_ids:
        return []
    bodies: list[str] = []
    for view in views:
        try:
            att_id = view.id
            status = view.extract_status or ""
            filename = view.filename
            text = view.extracted_text
        except AttributeError:
            continue
        if status != "ok" or att_id not in admitted_ids:
            continue
        if not isinstance(text, str) or not text:
            continue
        if not is_spreadsheet_filename(filename if isinstance(filename, str) else None):
            continue
        bodies.append(text)
    return bodies


def _normalize_number(raw: str) -> str | None:
    text = raw.translate(_FULLWIDTH).strip()
    if not text:
        return None
    if text[0] == "+":
        text = text[1:]
    negative = text.startswith("-")
    if negative:
        text = text[1:]
    text = text.replace(",", "")
    if not re.fullmatch(r"\d+(?:\.\d+)?", text):
        return None
    if "." in text:
        whole, frac = text.split(".", 1)
        frac = frac.rstrip("0")
        whole = whole.lstrip("0") or "0"
        norm = whole if not frac else f"{whole}.{frac}"
    else:
        norm = text.lstrip("0") or "0"
    if "." not in norm:
        try:
            if abs(int(norm)) < 10:
                return None
        except ValueError:
            return None
    if negative and norm != "0":
        norm = f"-{norm}"
    return norm


def numeric_tokens(text: str) -> set[str]:
    """阿拉伯數字（含千分位、小數、全形）。一位整數太常見，略過。"""
    if not text:
        return set()
    found: set[str] = set()
    for match in _NUMBER.finditer(text):
        norm = _normalize_number(match.group(1))
        if norm is not None:
            found.add(norm)
    return found


def quotes_parsed_table_numbers(answer: str | None, table_texts) -> bool:
    """回答裡有沒有數字，與送進模型的試算表文字是同一組正規化結果。"""
    if not isinstance(answer, str) or not answer or not table_texts:
        return False
    answer_nums = numeric_tokens(answer)
    if not answer_nums:
        return False
    table_nums: set[str] = set()
    for text in table_texts:
        if not isinstance(text, str) or not text:
            continue
        table_nums.update(numeric_tokens(text))
        if answer_nums & table_nums:
            return True
    return False


def apply_table_number_honesty(meta: dict, answer: str | None, table_texts) -> None:
    """平台擁有這個鍵。先清掉上游帶來的副本，再依比對結果決定要不要掛上。"""
    if not isinstance(meta, dict):
        return
    meta.pop(TABLE_NUMBER_HONESTY_KEY, None)
    if quotes_parsed_table_numbers(answer, table_texts):
        meta[TABLE_NUMBER_HONESTY_KEY] = {"quoted_from_parsed_table": True}


def _content_to_text(content) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, dict):
            text = item.get("text") or item.get("content")
            if isinstance(text, str):
                parts.append(text)
    return "".join(parts)


def assistant_text_from_completion(payload: dict | None) -> str:
    """非串流回答裡使用者看得到的文字。"""
    if not isinstance(payload, dict):
        return ""
    choices = payload.get("choices") or []
    if not isinstance(choices, list):
        return ""
    parts: list[str] = []
    for choice in choices:
        if not isinstance(choice, dict):
            continue
        message = choice.get("message")
        if not isinstance(message, dict):
            continue
        text = _content_to_text(message.get("content"))
        if text:
            parts.append(text)
    return "\n".join(parts)


def assistant_piece_from_sse_data(data: str | None) -> str:
    """一個 SSE data 行裡的助理增量。metadata 幀不要傳進來。"""
    if not data or data == "[DONE]":
        return ""
    try:
        payload = json.loads(data)
    except (json.JSONDecodeError, TypeError, ValueError):
        return ""
    if not isinstance(payload, dict):
        return ""
    choices = payload.get("choices")
    if not isinstance(choices, list):
        return ""
    parts: list[str] = []
    for choice in choices:
        if not isinstance(choice, dict):
            continue
        for key in ("delta", "message"):
            container = choice.get(key)
            if not isinstance(container, dict):
                continue
            text = _content_to_text(container.get("content"))
            if text:
                parts.append(text)
                break
    return "".join(parts)
