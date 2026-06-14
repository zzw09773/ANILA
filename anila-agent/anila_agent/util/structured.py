"""reasoning-model 結構化輸出的防禦性解析。

實測：gpt-oss-20b 與 gemma4 都是 reasoning 模型——回應把思考放在 ``reasoning``
欄位，``message.content`` 在推理完成前是 None；``max_tokens`` 太小會 ``finish_reason=length``
在吐出 JSON 前被截斷成空。此外 gemma4 會把 JSON 包在 ```json fence 裡。

本模組提供「永不丟例外」的 JSON 物件解析：失敗一律回退到 ``default``（fail-closed），
讓選擇器 / CitedAnswer 校驗等結構化路徑在模型不配合時安全降級而非崩潰。

模型層面的對應防護（max_tokens 下限）在 ``runtime.model``。
"""

from __future__ import annotations

import json
import re
from typing import Any

# 抓 ```json ... ``` 或 ``` ... ``` 圍欄內容（gemma4 會包，gpt-oss 不會——防禦性處理）。
_FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL | re.IGNORECASE)
# 後備：抓第一個 { 到最後一個 } 的區段。
_BRACES = re.compile(r"\{.*\}", re.DOTALL)


def strip_fences(text: str) -> str:
    """移除 markdown code fence，回傳內層內容；無 fence 則原樣回傳（去除前後空白）。"""
    if not text:
        return ""
    match = _FENCE.search(text)
    return (match.group(1) if match else text).strip()


def parse_json_object(text: str | None, default: dict[str, Any] | None = None) -> dict[str, Any]:
    """把模型輸出解析成 JSON 物件；任何失敗都回退 ``default``（fail-closed）。

    依序嘗試：直接 parse → 剝 fence 後 parse → 抓 {..} 區段後 parse。
    None / 空字串（reasoning 截斷的典型結果）直接回退。
    """
    fallback: dict[str, Any] = {} if default is None else default
    if not text or not text.strip():
        return fallback

    candidate = strip_fences(text)
    for source in (candidate, text):
        try:
            parsed = json.loads(source)
            if isinstance(parsed, dict):
                return parsed
        except (json.JSONDecodeError, TypeError):
            pass

    brace = _BRACES.search(candidate)
    if brace:
        try:
            parsed = json.loads(brace.group(0))
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
    return fallback
