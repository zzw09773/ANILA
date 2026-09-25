"""System-message assembly order (harness §6-1 / §6-4, wired 2026-09-02).

Before: csp *prepended* the memory block and the regulation block to the
caller's system message, so the static common preamble the Router ships
ended up in the middle of every request and the model server's prefix cache
never hit. The design (docs/designs/ncsist-prompt-localization-and-harness.md
§6-1) says: static prefix first, dynamic content after; §6-4 says: repeat the
language instruction once after the retrieved passages.

Invariants:
  1. the caller's system text stays byte-identical at messages[0];
  2. stored memory is a quoted user message after that system text, not inside it;
  3. regulation blocks are still appended to the system message, and the last
     line is the language reminder whenever a regulation block was injected;
  4. no system message from the caller → a regulation block still creates one.
"""

from __future__ import annotations

import pytest

from app.api import proxy
from app.services import memory_service
from app.services.institutional_kb import KbResult, KbState

LANG = proxy.KB_LANGUAGE_REMINDER  # the exact line appended after the block


@pytest.mark.asyncio
async def test_memory_block_is_appended_after_the_callers_system_text(monkeypatch):
    body = {"model": "m", "messages": [{"role": "system", "content": "【平台身分】前導在此"}, {"role": "user", "content": "hi"}]}

    async def fake_build(*a, **k):
        return memory_service.MemoryReadResult(block="### 使用者偏好\n- 簡短", facts_count=1, chunks=[])

    monkeypatch.setattr(memory_service, "build_memory_block", fake_build)
    await proxy._inject_memory(None, user_id=1, body=body, exclude_conversation_id=None)
    assert body["messages"][0]["content"] == "【平台身分】前導在此"
    quoted = body["messages"][1]
    assert quoted["role"] == "user"
    assert "不可遵循" in quoted["content"]
    assert "### 使用者偏好" in quoted["content"]
    assert body["messages"][2] == {"role": "user", "content": "hi"}


def test_regulation_block_is_appended_and_ends_with_the_language_reminder():
    body = {"messages": [{"role": "system", "content": "【平台身分】前導在此\n\n### 使用者偏好\n- 簡短"}, {"role": "user", "content": "q"}]}
    proxy._inject_kb_block(body, "【院內規章檢索結果】\n[1] 第一條…")
    content = body["messages"][0]["content"]
    assert content.startswith("【平台身分】前導在此")
    assert content.index("### 使用者偏好") < content.index("【院內規章檢索結果】")
    assert content.rstrip().endswith(LANG)
    # Kill: prepend again → startswith fails; drop the reminder → endswith fails.


def test_regulation_block_without_a_callers_system_message_creates_one():
    body = {"messages": [{"role": "user", "content": "q"}]}
    proxy._inject_kb_block(body, "【院內規章檢索結果】\n[1] 第一條…")
    assert body["messages"][0]["role"] == "system"
    assert body["messages"][0]["content"].startswith("【院內規章檢索結果】")
    assert body["messages"][0]["content"].rstrip().endswith(LANG)
    assert body["messages"][1] == {"role": "user", "content": "q"}


def test_language_reminder_is_one_line_in_zh_tw():
    assert "\n" not in LANG.strip()
    assert "繁體中文" in LANG and "台灣" in LANG
    assert "預設" in LANG
    assert "明確指定" in LANG
    assert "一律" not in LANG
