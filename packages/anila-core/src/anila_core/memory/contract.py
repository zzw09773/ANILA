"""舊的 agent 回覆哨兵。

新的外來內容一律走 ``anila_core.security.external_content``。這裡只留下
哨兵常數與清除函式，避免舊回覆裡的標記還能把包裝提前關掉。
本模組不匯入其他東西，避免循環依賴。
"""
from __future__ import annotations

AGENT_REPLY_BEGIN = "<<<ANILA_AGENT_REPLY"
AGENT_REPLY_END = "ANILA_AGENT_REPLY>>>"


def sanitize_agent_reply(text: str) -> str:
    """Strip agent-reply sentinels from the (untrusted) agent reply before it is
    wrapped for re-composition, so the agent can't close the wrapper early."""
    return (text or "").replace(AGENT_REPLY_BEGIN, "").replace(AGENT_REPLY_END, "")
