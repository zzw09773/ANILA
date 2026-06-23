from anila_core.memory.contract import (
    AGENT_REPLY_BEGIN,
    AGENT_REPLY_END,
    sanitize_agent_reply,
)


def test_agent_reply_sentinels_distinct_nonempty():
    assert AGENT_REPLY_BEGIN and AGENT_REPLY_END
    assert AGENT_REPLY_BEGIN != AGENT_REPLY_END


def test_sanitize_agent_reply_strips_sentinels():
    dirty = f"a {AGENT_REPLY_BEGIN} b {AGENT_REPLY_END} c"
    out = sanitize_agent_reply(dirty)
    assert AGENT_REPLY_BEGIN not in out and AGENT_REPLY_END not in out


def test_sanitize_handles_none():
    assert sanitize_agent_reply(None) == ""
