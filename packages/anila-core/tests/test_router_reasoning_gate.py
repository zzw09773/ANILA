"""Router 只在 CSP 這次回應明確說 reveal=1 時才送原文。

頭缺席或是 0：不送 anila.reasoning，meta／trace 裡的原文也不送，中文摘要要在。
頭是 1：原文、巢狀 reasoning、trace detail 裡的原文都在。
"""
from __future__ import annotations

import json

import httpx
import pytest
import respx

from anila_core.api import router_server as rs
from anila_core.config import settings
from anila_core.http_pool import aclose_http_client
from anila_core.memory import MemorySession, close_all_connections
from anila_core.registry.remote_agent_manifest import RemoteAgentManifest

CSP_URL = f"{settings.csp_base_url}/v1/chat/completions"
MARKER = "ANILA_RAW_MARKER_7f3c9e"
SUMMARY = "正在整理題目。"
ANSWER = "這是正式回答。"


class _AnyAgent:
    def get(self, _api_key: str, agent_id: str):
        return RemoteAgentManifest(
            agent_id=agent_id,
            name=agent_id,
            description_for_router="demo",
            endpoint_url="http://agents.invalid",
        )


def _sse(header: str | None) -> httpx.Response:
    reasoning = MARKER + " padding so the chunk is long enough for a summary."
    body = (
        'data: {"choices":[{"delta":{"reasoning_content":'
        + json.dumps(reasoning, ensure_ascii=False)
        + "}}]}\n\n"
        'data: {"choices":[{"delta":{"anila_thinking_summary":'
        + json.dumps(SUMMARY, ensure_ascii=False)
        + "}}]}\n\n"
        'data: {"choices":[{"delta":{"content":'
        + json.dumps(ANSWER, ensure_ascii=False)
        + '},"finish_reason":"stop"}]}\n\n'
        "data: [DONE]\n\n"
    )
    headers = {"Content-Type": "text/event-stream"}
    if header is not None:
        headers["X-ANILA-Reveal-Reasoning"] = header
    return httpx.Response(200, content=body.encode(), headers=headers)


def _json_completion(header: str | None) -> httpx.Response:
    payload = {
        "choices": [{
            "message": {
                "role": "assistant",
                "content": ANSWER,
                "reasoning_content": MARKER + " padding for the non-stream field.",
                "anila_thinking_summary": SUMMARY,
            },
            "finish_reason": "stop",
        }],
        "anila_meta": {
            "reasoning": MARKER,
            "trace": [
                {"label": "直接回答", "detail": "無需分派 agent"},
                {"label": "步驟", "detail": f"<think>{MARKER}</think>步驟說明"},
                {"label": "短", "detail": "d"},
                {"label": "複本", "detail": MARKER + " padding for the non-stream field."},
            ],
            "nested": {"reasoning_content": MARKER},
        },
    }
    headers = {}
    if header is not None:
        headers["X-ANILA-Reveal-Reasoning"] = header
    return httpx.Response(200, json=payload, headers=headers)


async def _drive_stream() -> str:
    await aclose_http_client()
    rs._reset_reasoning_gate()
    parts: list[str] = []
    try:
        async for line in rs._router_streaming(
            "sk",
            [{"role": "user", "content": "題目"}],
            [{"role": "user", "content": "題目"}],
            registry=_AnyAgent(),
            base_trace=[],
            started_at=0.0,
            session=MemorySession("reasoning-gate"),
            route_signal=rs._ROUTE_DIRECT,
        ):
            parts.append(line)
        return "".join(parts)
    finally:
        rs._reset_reasoning_gate()
        await close_all_connections()


def test_make_event_hides_reasoning_unless_reveal_is_explicitly_true():
    rs._reset_reasoning_gate()
    try:
        assert rs._make_event("anila.reasoning", {"delta": MARKER}) == ""
        rs._REVEAL_REASONING.set(False)
        assert rs._make_event("anila.reasoning", {"delta": MARKER}) == ""
        meta = rs._make_event(
            "anila.meta",
            {
                "reasoning": MARKER,
                "trace": [{"detail": f"<think>{MARKER}</think>步驟說明"}, {"detail": "無需分派 agent"}],
            },
        )
        assert MARKER not in meta
        assert "步驟說明" in meta
        assert "無需分派 agent" in meta
        rs._note_summary_line(SUMMARY)
        stamped = rs._make_event("anila.meta", {"trace": []})
        assert SUMMARY in stamped
        assert MARKER not in stamped
        rs._REVEAL_REASONING.set(True)
        assert MARKER in rs._make_event("anila.reasoning", {"delta": MARKER})
    finally:
        rs._reset_reasoning_gate()


def test_make_event_hides_reasoning_when_the_header_was_never_set():
    rs._reset_reasoning_gate()
    try:
        assert MARKER not in rs._make_event("anila.reasoning", {"delta": MARKER})
        traced = rs._make_event(
            "anila.trace",
            {"label": "步驟", "detail": f"<think>{MARKER}</think>步驟說明"},
        )
        assert MARKER not in traced
        assert "步驟說明" in traced
        assert "d" in rs._make_event("anila.trace", {"detail": "d"})
    finally:
        rs._reset_reasoning_gate()


@pytest.mark.asyncio
@respx.mock
async def test_router_stream_hides_marker_and_forwards_summary_when_header_is_zero():
    respx.post(CSP_URL).mock(return_value=_sse("0"))
    body = await _drive_stream()
    assert MARKER not in body
    assert "anila.reasoning" not in body
    assert "anila.thinking_summary" in body
    assert SUMMARY in body
    assert ANSWER in body
    assert '"reasoning"' not in body


@pytest.mark.asyncio
@respx.mock
async def test_router_stream_hides_reasoning_when_header_is_absent():
    respx.post(CSP_URL).mock(return_value=_sse(None))
    body = await _drive_stream()
    assert "anila.reasoning" not in body
    assert MARKER not in body
    assert SUMMARY in body
    assert ANSWER in body


@pytest.mark.asyncio
@respx.mock
async def test_router_stream_forwards_reasoning_when_header_is_one():
    respx.post(CSP_URL).mock(return_value=_sse("1"))
    body = await _drive_stream()
    assert "anila.reasoning" in body
    assert MARKER in body
    assert SUMMARY in body


@pytest.mark.asyncio
@respx.mock
async def test_router_stream_omits_reasoning_event_when_upstream_sends_none():
    body = (
        'data: {"choices":[{"delta":{"content":'
        + json.dumps(ANSWER, ensure_ascii=False)
        + '},"finish_reason":"stop"}]}\n\n'
        "data: [DONE]\n\n"
    )
    respx.post(CSP_URL).mock(return_value=httpx.Response(
        200,
        content=body.encode(),
        headers={"Content-Type": "text/event-stream"},
    ))
    streamed = await _drive_stream()
    assert "anila.reasoning" not in streamed
    assert ANSWER in streamed


@pytest.mark.asyncio
@respx.mock
async def test_router_non_stream_strips_meta_reasoning_when_header_is_zero():
    respx.post(CSP_URL).mock(return_value=_json_completion("0"))
    await aclose_http_client()
    rs._reset_reasoning_gate()
    try:
        result = await rs._call_llm_non_stream(
            "sk", [{"role": "user", "content": "題目"}],
        )
        full = rs._make_full_response(result["content"], "anila-router", result["anila_meta"])
        blob = json.dumps(full, ensure_ascii=False)
        assert MARKER not in blob
        assert result["reasoning"] is None
        assert "reasoning" not in (result["anila_meta"] or {})
        assert full["anila_meta"]["thinking_summaries"][-1]["text"] == SUMMARY
        assert ANSWER in full["choices"][0]["message"]["content"]
    finally:
        rs._reset_reasoning_gate()


def _assert_nested_hidden(blob: str) -> None:
    assert MARKER not in blob
    assert "步驟說明" in blob
    assert "無需分派 agent" in blob
    assert "reasoning_content" not in blob


@pytest.mark.asyncio
@respx.mock
async def test_router_non_stream_hides_reasoning_when_header_is_absent():
    respx.post(CSP_URL).mock(return_value=_json_completion(None))
    await aclose_http_client()
    rs._reset_reasoning_gate()
    try:
        result = await rs._call_llm_non_stream(
            "sk", [{"role": "user", "content": "題目"}],
        )
        full = rs._make_full_response(result["content"], "anila-router", result["anila_meta"])
        blob = json.dumps(full, ensure_ascii=False)
        assert result["reasoning"] is None
        _assert_nested_hidden(blob)
        assert ANSWER in full["choices"][0]["message"]["content"]
    finally:
        rs._reset_reasoning_gate()


@pytest.mark.asyncio
@respx.mock
async def test_router_non_stream_keeps_nested_reasoning_when_header_is_one():
    respx.post(CSP_URL).mock(return_value=_json_completion("1"))
    await aclose_http_client()
    rs._reset_reasoning_gate()
    try:
        result = await rs._call_llm_non_stream(
            "sk", [{"role": "user", "content": "題目"}],
        )
        full = rs._make_full_response(result["content"], "anila-router", result["anila_meta"])
        blob = json.dumps(full, ensure_ascii=False)
        assert result["reasoning"] and MARKER in result["reasoning"]
        assert MARKER in blob
        assert "步驟說明" in blob
    finally:
        rs._reset_reasoning_gate()


_CJK_ANSWER = "這是一段足夠長的中文回答用來通過密度檢查讓閘門把英文分析留在伺服器"


def _payload(frame: str) -> dict:
    return json.loads(frame.split("data: ", 1)[1])


def test_trace_detail_hides_unseen_thought_without_prior_deltas():
    """trace detail 裡的思考不靠先前看過的原文。reasoning*／thinking* 鍵一律拿掉。"""
    rs._reset_reasoning_gate()
    try:
        assert not (rs._SEEN_RAW.get() or [])
        raw = MARKER + " the model is analyzing the question in english only."
        frame = rs._make_event(
            "anila.trace",
            {
                "label": "步驟",
                "detail": f"thought:\n{raw}\n{_CJK_ANSWER}",
                "thinking": raw,
                "reasoning_text": raw,
                "nested": {"reasoning_content": raw},
                "thinking_summaries": [{"text": SUMMARY}],
                "usage": {"reasoning_tokens": 4, "reasoning_tokens_source": "reported"},
            },
        )
        parsed = _payload(frame)
        assert MARKER not in frame
        assert _CJK_ANSWER in parsed["detail"]
        assert "thinking" not in parsed
        assert "reasoning_text" not in parsed
        assert "reasoning_content" not in parsed["nested"]
        assert parsed["thinking_summaries"][0]["text"] == SUMMARY
        assert parsed["usage"]["reasoning_tokens"] == 4
        assert parsed["label"] == "步驟"
    finally:
        rs._reset_reasoning_gate()


def test_seen_raw_still_strips_a_copied_detail_when_hidden():
    rs._reset_reasoning_gate()
    try:
        raw = MARKER + " padding that was already seen."
        rs._note_seen_raw(raw)
        frame = rs._make_event(
            "anila.trace",
            {"label": "步驟", "detail": f"前文 {raw} 後文"},
        )
        assert MARKER not in frame
        assert "前文" in frame
        assert "後文" in frame
    finally:
        rs._reset_reasoning_gate()


def test_other_named_events_hide_raw_keys_and_think_spans():
    """隱藏時每個具名事件都剝原文，不只 anila.meta／anila.trace。"""
    rs._reset_reasoning_gate()
    try:
        raw = MARKER + " span attribute"
        frame = rs._make_event(
            "anila.spans",
            {
                "spans": [{
                    "span_id": "span-1",
                    "attributes": {
                        "reasoning": raw,
                        "reasoning_content": raw,
                        "detail": f"<think>{raw}</think>步驟說明",
                        "text": f"keep <think>{raw}</think>可見",
                        "message": f"<thinking>{raw}</thinking>訊息",
                        "target": "agent-a",
                    },
                }],
            },
        )
        parsed = _payload(frame)
        attrs = parsed["spans"][0]["attributes"]
        assert MARKER not in frame
        assert attrs["detail"] == "步驟說明"
        assert "可見" in attrs["text"]
        assert "訊息" in attrs["message"]
        assert attrs["target"] == "agent-a"
        assert "reasoning" not in attrs
        assert "reasoning_content" not in attrs
        summary = rs._make_event("anila.thinking_summary", {"delta": SUMMARY})
        assert SUMMARY in summary
        assert MARKER not in summary
        rs._REVEAL_REASONING.set(True)
        shown = rs._make_event(
            "anila.spans",
            {"attributes": {"reasoning_content": MARKER, "detail": f"<think>{MARKER}</think>"}},
        )
        assert MARKER in shown
    finally:
        rs._reset_reasoning_gate()
