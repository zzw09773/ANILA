"""Live thinking summaries: one short user-facing line, fail-open, thinking off.

Astra 2026-09-17：不要把原始 reasoning 第一句當標題；CSP 分批請院內模型
產出進度摘要；失敗不擋正式答案；摘要歷程小額度另存。
"""
from __future__ import annotations

import os

os.environ.setdefault("ANILA_ALLOW_DEV_SECRET", "1")

from unittest.mock import MagicMock, patch

import pytest

from app.services.thinking_summary import (
    MAX_HISTORY,
    MAX_SUMMARY_CHARS,
    append_summary,
    build_summarize_payload,
    sanitize_summary,
    summarize_reasoning_batch,
)


def test_sanitize_keeps_a_short_progress_sentence():
    assert sanitize_summary("正在整理暗物質與暗能量的差異") == "正在整理暗物質與暗能量的差異。"


def test_sanitize_rejects_language_arbitration_and_empty():
    assert sanitize_summary("我應該用繁體中文還是依照系統指令？") is None
    assert sanitize_summary("   ") is None
    assert sanitize_summary("ok") is None


def test_sanitize_does_not_keep_a_raw_reasoning_dump():
    raw = (
        "The user asked about the universe. I need to check the language "
        "preference file and then plan a reply covering dark matter."
    )
    out = sanitize_summary(raw)
    assert out is None or len(out) <= MAX_SUMMARY_CHARS + 1


def test_append_summary_skips_duplicate_and_caps_history():
    hist = []
    hist = append_summary(hist, "探索宇宙奧秘的提問。")
    hist = append_summary(hist, "探索宇宙奧秘的提問。")
    assert [x["text"] for x in hist] == ["探索宇宙奧秘的提問。"]
    for i in range(40):
        hist = append_summary(hist, f"步驟{i}。")
    assert len(hist) == MAX_HISTORY
    assert hist[0]["text"] == "步驟16。"
    assert hist[-1]["text"] == "步驟39。"


def test_build_payload_disables_thinking_and_does_not_use_first_reasoning_line_as_title():
    payload = build_summarize_payload(
        model="gemma4",
        added="The user asked X.\nI should obey the language instruction first.",
        previous=[{"text": "探索宇宙奧秘的提問。"}],
    )
    assert payload["model"] == "gemma4"
    assert payload["chat_template_kwargs"]["enable_thinking"] is False
    assert "reasoning_effort" not in payload
    user = payload["messages"][1]["content"]
    assert "不要複述" in payload["messages"][0]["content"] or "不要" in payload["messages"][0]["content"]
    assert "探索宇宙奧秘的提問。" in user
    assert user.index("新增思考") < user.index("The user asked X") or "新增" in user


@pytest.mark.asyncio
async def test_summarize_fail_open_on_http_error():
    db = MagicMock()
    model = MagicMock()
    model.name = "gemma4"

    async def _fail(*_args, **_kwargs):
        from app.services.internal_llm import InternalCompletionError

        raise InternalCompletionError(401)

    with patch(
        "app.services.thinking_summary._summary_model",
        return_value=model,
    ), patch(
        "app.services.internal_llm.complete_chat",
        side_effect=_fail,
    ):
        assert await summarize_reasoning_batch(db, added="x" * 80, previous=[]) is None


@pytest.mark.asyncio
async def test_summarize_returns_sanitized_content_only():
    db = MagicMock()
    model = MagicMock()
    model.name = "gemma4"
    seen = {}

    async def _ok(_db, _model, body, **_kwargs):
        seen["body"] = body
        return "正在整理暗物質與暗能量的差異"

    with patch(
        "app.services.thinking_summary._summary_model",
        return_value=model,
    ), patch(
        "app.services.internal_llm.complete_chat",
        side_effect=_ok,
    ):
        out = await summarize_reasoning_batch(
            db, added="暗物質佔 27%，暗能量佔 68%。" * 4, previous=[]
        )
        assert out == "正在整理暗物質與暗能量的差異。"
        assert seen["body"]["chat_template_kwargs"]["enable_thinking"] is False


def test_summarize_endpoint_is_authenticated_and_fail_open(client, db, monkeypatch):
    from tests.conftest import login, make_user

    make_user(db, username="think_sum_user")
    token = login(client, username="think_sum_user")
    headers = {"Authorization": f"Bearer {token}"}

    async def _fake(*_a, **_k):
        return "正在整理暗物質與暗能量的差異。"

    monkeypatch.setattr(
        "app.api.thinking.summarize_reasoning_batch",
        _fake,
    )
    ok = client.post(
        "/api/thinking/summarize",
        json={"added": "暗物質佔 27%。" * 10, "previous": []},
        headers=headers,
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["summary"] == "正在整理暗物質與暗能量的差異。"

    denied = client.post(
        "/api/thinking/summarize",
        json={"added": "x" * 80, "previous": []},
    )
    assert denied.status_code in (401, 403)
