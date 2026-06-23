"""Sentinel for wrapping the (untrusted) agent reply during re-composition.

The Router's reply re-composition wraps the dispatched agent reply in
``AGENT_REPLY_BEGIN``/``AGENT_REPLY_END`` and tells the model it is DATA to
rewrite, not instructions. Pure constants — no imports — so any package can
import without circular-dependency risk.

Note: the Router does NOT extract or forward the user's memory. CSP injects the
user's long-term memory into every LLM call the Router makes through it
(including the re-composition call), so no memory-block sentinel is needed here.
"""
from __future__ import annotations

AGENT_REPLY_BEGIN = "<<<ANILA_AGENT_REPLY"
AGENT_REPLY_END = "ANILA_AGENT_REPLY>>>"


def sanitize_agent_reply(text: str) -> str:
    """Strip agent-reply sentinels from the (untrusted) agent reply before it is
    wrapped for re-composition, so the agent can't close the wrapper early."""
    return (text or "").replace(AGENT_REPLY_BEGIN, "").replace(AGENT_REPLY_END, "")
