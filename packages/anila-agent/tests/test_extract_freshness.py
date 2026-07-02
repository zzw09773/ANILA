"""memdir 去敏抽取 + 新鮮度標記。"""

from __future__ import annotations

import pytest

from anila_agent.memory.extract import extract_memories, redact
from anila_agent.memory.freshness import freshness_caveat, is_stale, relative_age, tag
from anila_agent.memory.taxonomy import MemoryType, coerce_type

pytestmark = pytest.mark.unit


# ---- redact ----

def test_redact_strips_secrets_and_identity():
    text = (
        "token csk-abc123DEF and sk-secretkey99\n"
        "X-ANILA-User-Email: someone@mil.tw\n"
        "Authorization: Bearer eyJxxx.yyy.zzz"
    )
    out = redact(text)
    assert "csk-abc123DEF" not in out
    assert "sk-secretkey99" not in out
    assert "someone@mil.tw" not in out
    assert "eyJxxx" not in out
    assert "[REDACTED]" in out


def test_redact_json_quoted_identity_and_bare_email_and_jwt():
    text = (
        '{"X-ANILA-User-Email": "agent@mil.tw", "note": "聯絡 user2@example.com"}\n'
        "raw jwt eyJhbGciOi.JzdWIiOiJ.SflKxwRJSM here"
    )
    out = redact(text)
    assert "agent@mil.tw" not in out
    assert "user2@example.com" not in out  # bare email 也遮
    assert "eyJhbGciOi.JzdWIiOiJ.SflKxwRJSM" not in out  # bare JWT 也遮


# ---- extract_memories ----

async def test_extract_filters_invalid_and_redacts_body():
    async def extractor(redacted, manifest):
        return [
            {"name": "ok-one", "description": "d", "type": "project", "body": "leak csk-zzz here"},
            {"name": "bad", "description": "d", "type": "not_a_type"},  # 不合法 type → 丟棄
            {"name": "", "description": "d", "type": "user"},  # 無 name → 丟棄
        ]

    mems = await extract_memories("transcript", {}, extractor=extractor)
    assert len(mems) == 1
    assert mems[0].name == "ok-one"
    assert "csk-zzz" not in mems[0].body  # body 也去敏


async def test_extract_fail_closed():
    async def boom(redacted, manifest):
        raise RuntimeError("extractor down")

    assert await extract_memories("t", {}, extractor=boom) == []


# ---- freshness ----

def test_relative_age_buckets():
    now = 1_000_000_000.0
    assert relative_age(now, now) == "今天"
    assert relative_age(now - 3 * 86400, now) == "3 天前"
    assert relative_age(now - 14 * 86400, now) == "2 週前"
    assert relative_age(now - 60 * 86400, now) == "2 個月前"
    assert relative_age(now - 800 * 86400, now) == "2 年前"


def test_staleness_and_caveat():
    now = 1_000_000_000.0
    assert is_stale(now - 100 * 86400, now) is True
    assert is_stale(now - 10 * 86400, now) is False
    assert "驗證" in freshness_caveat(now - 100 * 86400, now)
    assert freshness_caveat(now - 1 * 86400, now) == ""


def test_tag_combines_age_and_caveat():
    now = 1_000_000_000.0
    fresh = tag("mem-x", now - 1 * 86400, now)
    assert "mem-x" in fresh and "天前" in fresh and "驗證" not in fresh
    stale = tag("mem-y", now - 200 * 86400, now)
    assert "驗證" in stale  # 過時帶 caveat


# ---- taxonomy ----

def test_coerce_type_valid_and_invalid():
    assert coerce_type("feedback") is MemoryType.FEEDBACK
    assert coerce_type(" Project ") is MemoryType.PROJECT  # 容忍空白/大小寫
    with pytest.raises(ValueError):
        coerce_type("bogus")
