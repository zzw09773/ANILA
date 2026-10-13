# -*- coding: utf-8 -*-
"""送出前的回答方式。

快速短回答仍走既有的聊天串流，只在送到模型前加一句簡短指示。
完整回答由背景工作產生，完成後寫進既有的 notifications 表。
"""
from __future__ import annotations

SHORT_ANSWER_SYSTEM = (
    "請用簡短的方式回答：先給結論，必要時最多三點補充。不要展開長篇。"
)
FULL_ANSWER_SYSTEM = (
    "請完整回答這則問題：寫出必要的步驟、理由，以及同仁需要再確認的地方。"
    "使用繁體中文（台灣）。"
)
BACKGROUND_PLACEHOLDER = "完整回答正在背景進行，做好會通知你。"
BACKGROUND_FAILURE = "完整回答沒有完成。請回到對話再試一次。"

ANSWER_READY = "answer_ready"
ANSWER_FAILED = "answer_failed"


def apply_short_answer(body: dict, header_value: str | None) -> None:
    """選了快速短回答時，把簡短指示放進即將送出的 messages。

    只認標頭 ``X-ANILA-Answer-Delivery: short``。不新增模型，也不改寫
    呼叫端原本的使用者句子。已經有同一句時不重複加。
    """
    if not isinstance(body, dict):
        return
    if (header_value or "").strip().lower() != "short":
        return
    messages = body.get("messages")
    if not isinstance(messages, list):
        return
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "system":
            continue
        content = message.get("content")
        if isinstance(content, str) and SHORT_ANSWER_SYSTEM in content:
            return
    messages.insert(0, {"role": "system", "content": SHORT_ANSWER_SYSTEM})
    body["messages"] = messages
