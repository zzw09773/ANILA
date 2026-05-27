"""P2-15 / claude-code §4.23 — stripSignatureBlocks。

claude-code-src ``src/utils/messages.ts`` 在發新 LLM call 之前會把訊息裡的
「signature block」剝掉。signature block 是某些 provider(尤其是 Anthropic 的
extended thinking)在 model output 上掛的不透明簽章(``thinking`` block 對應的
``signature`` 欄位,以及 redacted_thinking 的 ``data`` 欄位)。

為什麼要剝:
- **Cache-safe re-render**:同一條 prompt 在 retry / fork sub-agent 時,只要
  prefix bytes 全等就能命中 prompt cache(P0-2 / P0-3);signature 是 turn 內
  動態值,留著會破 prefix。
- **Token saving**:signature blob 大(數千 char base64),sub-agent 用不到。
- **Replay safety**:signature 只對原 turn 的 nonce 有效;replay 到別的 turn 會
  被 server 拒收。

本模組對齊 claude-code 的剝除規則 + 一些 ANILA 自家擴充(redacted、tool_use
signature 等),走 pure-function、不 mutate 輸入。

不引新 dep,所有操作都是 dict / list copy。
"""

from __future__ import annotations

from typing import Any

# 共同的 ``signature`` 欄位 key —— 出現任一即剝掉。
SIGNATURE_FIELDS: frozenset[str] = frozenset(
    {
        "signature",  # Anthropic thinking signature
        "signature_block",  # 某些 wire 用 underscore 別名
        "thinking_signature",
        "cache_signature",  # 某些 prov cache key
        # redacted_thinking 的 ``data`` 欄位本身就是 server-side opaque,
        # 對 cache prefix 同樣是 noise。
    }
)

# 整個 block ``type`` 等於這些時,該 block 直接整塊移除(留 placeholder 或不留)。
DROPPABLE_BLOCK_TYPES: frozenset[str] = frozenset(
    {
        "redacted_thinking",
    }
)

# Message 跟 ``memory.compaction`` 對齊。
Message = dict[str, Any]


def strip_signature_from_block(block: dict[str, Any]) -> dict[str, Any]:
    """回傳新 dict:把 ``SIGNATURE_FIELDS`` key 移除。

    block 本身不被 mutate。若 block 含巢狀結構(例如 tool_use 內含
    ``content`` list),會遞迴處理。
    """
    cleaned: dict[str, Any] = {}
    for key, value in block.items():
        if key in SIGNATURE_FIELDS:
            continue
        if isinstance(value, dict):
            cleaned[key] = strip_signature_from_block(value)
        elif isinstance(value, list):
            cleaned[key] = [
                strip_signature_from_block(item) if isinstance(item, dict) else item
                for item in value
            ]
        else:
            cleaned[key] = value
    return cleaned


def is_droppable_block(block: Any) -> bool:
    """block 的 ``type`` 落在 :data:`DROPPABLE_BLOCK_TYPES` 即視為整塊應移除。"""
    if not isinstance(block, dict):
        return False
    return block.get("type") in DROPPABLE_BLOCK_TYPES


def strip_signature_blocks(messages: list[Message]) -> list[Message]:
    """回傳新 list:把所有 message 的 signature 欄位與 droppable block 移除。

    處理規則:
    1. ``content`` 為 ``str`` —— passthrough。
    2. ``content`` 為 ``list[dict]`` —— 過濾 droppable block,其餘 block
       對每一個套 :func:`strip_signature_from_block`。
    3. ``message`` 本身若有 top-level signature 欄位(罕見,但 LiteLLM Bedrock
       wire 上見過)也一併剝。

    Args:
        messages: 任意 wire 格式的 list。

    Returns:
        新 list,不 mutate 輸入。即使輸入為空 list 也回新空 list。
    """
    cleaned: list[Message] = []
    for msg in messages:
        cleaned.append(_strip_message(msg))
    return cleaned


def _strip_message(msg: Message) -> Message:
    """單一 message 的剝除:處理 top-level + content list。"""
    if not isinstance(msg, dict):
        return msg

    new_msg: Message = {}
    for key, value in msg.items():
        if key in SIGNATURE_FIELDS:
            continue
        if key == "content":
            new_msg[key] = _strip_content(value)
        elif isinstance(value, dict):
            new_msg[key] = strip_signature_from_block(value)
        else:
            new_msg[key] = value
    return new_msg


def _strip_content(content: Any) -> Any:
    """content 欄位的剝除:可能是 str / list / dict / None。"""
    if isinstance(content, str) or content is None:
        return content
    if isinstance(content, list):
        cleaned: list[Any] = []
        for block in content:
            if is_droppable_block(block):
                # 完全 drop,不留 placeholder —— 與 claude-code 的行為一致。
                continue
            if isinstance(block, dict):
                cleaned.append(strip_signature_from_block(block))
            else:
                cleaned.append(block)
        return cleaned
    if isinstance(content, dict):
        if is_droppable_block(content):
            return None
        return strip_signature_from_block(content)
    return content


def has_signature(messages: list[Message]) -> bool:
    """快速檢查 messages 是否含任何 signature 欄位 / droppable block。

    給 cache prefix 檢查 / regression test 用 —— 若某段 prompt prefix 沒先
    strip 就送進 model,prompt-cache hit rate 會崩。
    """
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        if any(k in msg for k in SIGNATURE_FIELDS):
            return True
        content = msg.get("content")
        if isinstance(content, list):
            for block in content:
                if is_droppable_block(block):
                    return True
                if isinstance(block, dict) and _block_has_sig(block):
                    return True
        elif isinstance(content, dict):
            if is_droppable_block(content):
                return True
            if _block_has_sig(content):
                return True
    return False


def _block_has_sig(block: dict[str, Any]) -> bool:
    """遞迴判斷 block 是否含 signature 欄位。"""
    if any(k in block for k in SIGNATURE_FIELDS):
        return True
    for value in block.values():
        if isinstance(value, dict) and _block_has_sig(value):
            return True
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict) and _block_has_sig(item):
                    return True
    return False


__all__ = [
    "DROPPABLE_BLOCK_TYPES",
    "Message",
    "SIGNATURE_FIELDS",
    "has_signature",
    "is_droppable_block",
    "strip_signature_blocks",
    "strip_signature_from_block",
]
