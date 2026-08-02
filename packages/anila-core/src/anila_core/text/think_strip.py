"""剝除模型回應中內嵌的 think / thinking 區塊。

部分 serving 鏈會把推理塞進 ``message.content`` 的
``<think>...</think>``／``<thinking>...</thinking>``，而不是獨立的
``reasoning_content`` 欄位。落庫與回傳前必須清掉，避免英文推理進到
使用者可見／持久化內容。

純函式、無 I/O；呼叫端負責 fail-open。
"""

from __future__ import annotations

import re

# 最內層成對標籤：內容不得再含開標籤，避免非貪婪誤吃外層前半。
# 反覆套用即可處理「看起來巢狀」的情況。
_INNERMOST_CLOSED_RE = re.compile(
    r"<think(?:ing)?\b[^>]*>(?:(?!<think(?:ing)?\b).)*?</think(?:ing)?>",
    re.IGNORECASE | re.DOTALL,
)
# 未閉合：從開標籤一路吃到字串結尾。
_UNCLOSED_RE = re.compile(
    r"<think(?:ing)?\b[^>]*>.*\Z",
    re.IGNORECASE | re.DOTALL,
)


def strip_inline_think(text: str | None) -> tuple[str, int]:
    """移除內嵌 ``<think>``／``<thinking>`` 區塊。

    Returns:
        ``(clean_text, removed_chars)``。``None``／空字串皆安全；
        ``None`` 正規成空字串。``removed_chars`` 為移除的字元數
        （以原字串長度差計算）。
    """
    if text is None:
        return "", 0
    if text == "":
        return "", 0

    original_len = len(text)
    cleaned = text
    while True:
        nxt = _INNERMOST_CLOSED_RE.sub("", cleaned)
        if nxt == cleaned:
            break
        cleaned = nxt
    cleaned = _UNCLOSED_RE.sub("", cleaned)
    return cleaned, original_len - len(cleaned)
