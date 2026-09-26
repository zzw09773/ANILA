"""模型自己決定要不要再寫一輪。

最後一行是 ``ROUND: CONTINUE <下一步>`` 就再叫一次，接到同一則答案；
沒有這一行就結束。程式碼區塊與引用裡的同一行不當標記。
輪數上限預設 6（硬上限 10），每則模型呼叫預算預設 12（硬上限 30）。
兩者都在治理頁，不讀環境變數。預算用完就留下 finish_reason=length，
讓「繼續」出現；按下去是新的一則，預算重算。每一輪仍受救援與自動續寫保護。
後續輪的 ASK／DISPATCH／RECALL 是正文。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
import respx
from fastapi.testclient import TestClient

from anila_core.api import router_prompts as rp
from anila_core.api import router_server as rs
from anila_core.api.router_server import create_router_app
from anila_core.config import settings
from anila_core.memory import MemorySession, close_all_connections
from anila_core.registry.remote_agent_manifest import RemoteAgentManifest


CSP_URL = f"{settings.csp_base_url}/v1/chat/completions"
CSP_AGENTS_URL = f"{settings.csp_base_url}/v1/agents"


class _Resp:
    def __init__(self, payload: dict, status: int = 200):
        self._payload = payload
        self.status_code = status
        self.text = json.dumps(payload)

    def raise_for_status(self):
        if self.status_code >= 400:
            request = httpx.Request("POST", CSP_URL)
            response = httpx.Response(self.status_code, text=self.text, request=request)
            raise httpx.HTTPStatusError("err", request=request, response=response)
        return None

    def json(self):
        return self._payload


class _StreamResp:
    def __init__(self, lines: list[str]):
        self.status_code = 200
        self._lines = lines

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def aiter_lines(self):
        for line in self._lines:
            yield line

    async def aread(self):
        return b""


class _Client:
    def __init__(self, answers=None, streams=None):
        self.posts: list[dict] = []
        self.streams: list[dict] = []
        self._answers = list(answers or [])
        self._streams = list(streams or [])

    async def post(self, url, json=None, headers=None):
        self.posts.append(json)
        return _Resp(self._answers.pop(0))

    def stream(self, method, url, json=None, headers=None):
        self.streams.append(json)
        return _StreamResp(self._streams.pop(0))


class _AnyAgent:
    def get(self, _api_key: str, agent_id: str):
        return RemoteAgentManifest(
            agent_id=agent_id,
            name=agent_id,
            description_for_router="demo",
            endpoint_url="http://agents.invalid",
        )


def _reply(content: str, finish: str = "stop", reasoning: str = "", meta: dict | None = None) -> dict:
    message: dict = {"role": "assistant", "content": content}
    if reasoning:
        message["reasoning_content"] = reasoning
    payload = {
        "choices": [
            {"index": 0, "message": message, "finish_reason": finish}
        ]
    }
    if meta is not None:
        payload["anila_meta"] = meta
    return payload


def _stream_lines(
    content: str = "",
    finish: str = "stop",
    reasoning: str = "",
) -> list[str]:
    out: list[str] = []
    if reasoning:
        out.append(
            "data: "
            + json.dumps({"choices": [{"delta": {"reasoning_content": reasoning}}]})
        )
    if content:
        out.append("data: " + json.dumps({"choices": [{"delta": {"content": content}}]}))
    out.append("data: " + json.dumps({"choices": [{"delta": {}, "finish_reason": finish}]}))
    out.append("data: [DONE]")
    return out


def _events(body: str) -> list[tuple[str, str]]:
    events: list[tuple[str, str]] = []
    for block in body.split("\n\n"):
        name = "message"
        data = ""
        for line in block.split("\n"):
            if line.startswith("event:"):
                name = line[len("event:") :].strip()
            elif line.startswith("data:"):
                data = line[len("data:") :].strip()
        if data:
            events.append((name, data))
    return events


def _visible(body: str) -> tuple[str, dict | None]:
    text: list[str] = []
    meta = None
    for name, data in _events(body):
        if name == "anila.meta":
            meta = json.loads(data)
        elif name == "message" and data != "[DONE]":
            payload = json.loads(data)
            for choice in payload.get("choices") or []:
                piece = (choice.get("delta") or {}).get("content")
                if piece:
                    text.append(piece)
                reason = choice.get("finish_reason")
                if reason and meta is not None:
                    meta = {**meta, "_chunk_finish": reason}
                elif reason:
                    meta = {"_chunk_finish": reason}
    return "".join(text), meta


def _stage_titles(body: str) -> list[str]:
    """畫面上出現過的階段。同一筆的進行中與完成都算。"""
    titles: list[str] = []
    for name, data in _events(body):
        if name == "anila.thinking_stage":
            titles.append(json.loads(data).get("title") or "")
    return titles


def _saved_titles(meta: dict | None) -> list[str]:
    return [row["title"] for row in (meta or {}).get("thinking_stages") or []]


def _user_and_assistant(payload: dict) -> tuple[list[str], list[str]]:
    users = [
        m.get("content") or ""
        for m in payload.get("messages") or []
        if m.get("role") == "user"
    ]
    assistants = [
        m.get("content") or ""
        for m in payload.get("messages") or []
        if m.get("role") == "assistant"
    ]
    return users, assistants


def _drive(monkeypatch, streams: list[list[str]], *, session: str = "rounds") -> tuple[str, _Client]:
    client = _Client(streams=streams)
    monkeypatch.setattr(rs, "get_http_client", lambda: client)

    async def run() -> str:
        parts: list[str] = []
        async for line in rs._router_streaming(
            "sk",
            [{"role": "user", "content": "請分步做完"}],
            [{"role": "user", "content": "請分步做完"}],
            registry=_AnyAgent(),
            base_trace=[],
            started_at=0.0,
            session=MemorySession(session),
            route_signal=rs._ROUTE_DIRECT,
        ):
            parts.append(line)
        return "".join(parts)

    return asyncio.run(run()), client


def _drive_until(monkeypatch, streams: list[list[str]], stop_when) -> tuple[str, _Client]:
    client = _Client(streams=streams)
    monkeypatch.setattr(rs, "get_http_client", lambda: client)

    async def run() -> str:
        parts: list[str] = []
        agen = rs._router_streaming(
            "sk",
            [{"role": "user", "content": "請分步做完"}],
            [{"role": "user", "content": "請分步做完"}],
            registry=_AnyAgent(),
            base_trace=[],
            started_at=0.0,
            session=MemorySession("stop-round"),
            route_signal=rs._ROUTE_DIRECT,
        )
        async for line in agen:
            parts.append(line)
            if stop_when("".join(parts)):
                break
        return "".join(parts)

    return asyncio.run(run()), client


def test_round_rule_sits_on_the_non_editable_tail(monkeypatch):
    """分輪規則跟日期、不得外洩一樣附在組好的提示後面，治理中心改不到。"""
    for text in (
        rp.DEFAULT_ROUTER_SYSTEM,
        rp.DEFAULT_PLAIN_ASSISTANT,
        rp.DEFAULT_FORCED_ANSWER,
    ):
        assert "ROUND:" not in text
        assert "可以分輪" not in text
        assert "work in rounds" not in text

    zh = rs._build_system_prompt([])
    forced = rs._forced_answer_prompt()
    for text in (zh, forced):
        assert "可以分輪" in text
        assert "ROUND: CONTINUE" in text
        assert "沒有這一行" in text
        assert "簡單的問題不要用" in text
        assert "程式碼區塊" in text
        assert text.index(rp.DISCLOSURE_RULE_ZH) < text.index("STAGE:")
        assert text.index("STAGE:") < text.index("ROUND:")
        assert text.index("ROUND:") < text.rindex("今天是")
        assert text.count("可以分輪") == 1
        assert rs._stamp_router_today(text).count("可以分輪") == 1

    monkeypatch.setattr(
        rs,
        "current_router_prompts",
        lambda: {
            rp.KEY_SYSTEM: "Route the request.\n{agent_list}",
            rp.KEY_PLAIN: "Answer in English.",
            rp.KEY_FORCED: "Answer from the regulations.",
        },
    )
    for text in (rs._build_system_prompt([]), rs._forced_answer_prompt()):
        assert "ROUND: CONTINUE" in text
        assert "work in rounds" in text
        assert "No marker means you are done" in text
        assert "simple question" in text
        assert "code fence" in text
        assert "quotation" in text
        assert text.index(rp.DISCLOSURE_RULE_EN) < text.index("STAGE:")
        assert text.index("STAGE:") < text.index("ROUND:")
        assert text.index("ROUND:") < text.rindex("Today is")
        assert "可以分輪" not in text


def test_two_rounds_strip_the_marker_join_and_name_the_next_stage(monkeypatch):
    """兩輪：標記不進答案，下一輪帶著前文與「不要重複」，階段寫第 2 輪。"""
    replay = "太陽系的形成過程" * 5
    body, client = _drive(
        monkeypatch,
        [
            _stream_lines(f"這段答案在{replay}\nROUND: CONTINUE 寫水星", "stop"),
            _stream_lines(f"{replay}，然後寫下水星。", "stop"),
            _stream_lines("不該有第三輪。", "stop"),
        ],
    )
    visible, meta = _visible(body)
    assert len(client.streams) == 2
    assert "ROUND:" not in visible
    assert visible.count(replay) == 1
    assert visible.endswith("，然後寫下水星。")
    assert "不該有第三輪" not in visible
    assert "第 2 輪：寫水星" in _stage_titles(body)
    assert _saved_titles(meta) == ["第 2 輪：寫水星"]
    assert meta["thinking_stages"][0]["title"] == "第 2 輪：寫水星"
    assert meta["thinking_stages"][0]["status"] == "done"
    assert meta.get("finish_reason") != "length"
    assert meta.get("_chunk_finish") == "stop"
    users, assistants = _user_and_assistant(client.streams[1])
    assert any("不要重複" in text for text in users)
    assert any("寫水星" in text for text in users)
    assert any(replay in text and "ROUND:" not in text for text in assistants)


def test_three_rounds_keep_every_prior_answer_in_context(monkeypatch):
    body, client = _drive(
        monkeypatch,
        [
            _stream_lines("先分析需求。\nROUND: CONTINUE 計算總價", "stop"),
            _stream_lines("總價是 10。\nROUND: CONTINUE 提出建議", "stop"),
            _stream_lines("建議採用甲案。", "stop"),
        ],
    )
    visible, meta = _visible(body)
    assert len(client.streams) == 3
    assert visible == "先分析需求。\n總價是 10。\n建議採用甲案。"
    assert "ROUND:" not in visible
    assert _saved_titles(meta) == ["第 2 輪：計算總價", "第 3 輪：提出建議"]
    assert _stage_titles(body).count("第 2 輪：計算總價") >= 1
    assert _stage_titles(body).count("第 3 輪：提出建議") >= 1
    assert [row["status"] for row in meta["thinking_stages"]] == ["done", "done"]
    _users, third_assistants = _user_and_assistant(client.streams[2])
    joined = "\n".join(third_assistants)
    assert "先分析需求。" in joined
    assert "總價是 10。" in joined
    assert "ROUND:" not in joined
    third_users, _unused = _user_and_assistant(client.streams[2])
    assert any("不要重複" in text and "提出建議" in text for text in third_users)


def test_cap_defaults_to_six_and_env_lowers_it(monkeypatch):
    """輪數上限預設 6。測試把平台設定調成 2，用完就附註記並給「繼續」。"""
    monkeypatch.delenv("ANILA_ROUND_SAFETY_CAP", raising=False)
    rs.reset_router_prompt_cache()
    assert rs.round_safety_cap() == 6
    rs.apply_router_limits(round_cap=2)
    body, client = _drive(
        monkeypatch,
        [
            _stream_lines("第1段正文。\nROUND: CONTINUE 下一步1", "stop"),
            _stream_lines("第2段正文。\nROUND: CONTINUE 下一步2", "stop"),
            _stream_lines("第三輪不該出現。", "stop"),
        ],
    )
    visible, meta = _visible(body)
    assert len(client.streams) == 2
    assert "第1段正文。" in visible
    assert "第2段正文。" in visible
    assert "第三輪不該出現" not in visible
    assert "ROUND:" not in visible
    assert "已達這次回合上限" in visible
    assert "繼續" in visible
    assert meta["finish_reason"] == "length"
    assert meta["_chunk_finish"] == "length"
    rs.reset_router_prompt_cache()


def test_call_budget_is_shared_clamped_and_fresh_each_turn(monkeypatch):
    """一則回答的模型呼叫共用一個預算，含自動續寫。用完就停，並給「繼續」。

    環境變數改不了這兩個上限。超過硬上限的值會被夾住。
    下一則（按繼續是新的一則）重新計數。
    """
    monkeypatch.setenv("ANILA_ROUND_SAFETY_CAP", "99")
    monkeypatch.setenv("ANILA_MODEL_CALL_BUDGET", "99")
    rs.reset_router_prompt_cache()
    assert rs.round_safety_cap() == 6
    assert rs.model_call_budget() == 12
    rs.apply_router_limits(round_cap=99, call_budget=500)
    assert rs.round_safety_cap() == 10
    assert rs.model_call_budget() == 30

    rs.apply_router_limits(round_cap=6, call_budget=2)
    body, client = _drive(
        monkeypatch,
        [
            _stream_lines("已經寫到這裡", "length"),
            _stream_lines("。\nROUND: CONTINUE 再寫一輪", "stop"),
            _stream_lines("第三輪不該出現。", "stop"),
        ],
    )
    visible, meta = _visible(body)
    assert len(client.streams) == 2
    assert "已經寫到這裡。" in visible
    assert "第三輪不該出現" not in visible
    assert "ROUND:" not in visible
    assert "模型呼叫" in visible
    assert "繼續" in visible
    assert meta["finish_reason"] == "length"
    assert meta["_chunk_finish"] == "length"

    # 按「繼續」是新的一則。就算上一則把計數用完，這一則也重新計算。
    rs._REQUEST_MODEL_CALLS.set(99)
    rs.begin_model_call_budget()
    again, again_client = _drive(
        monkeypatch,
        [_stream_lines("接下去了。", "stop")],
        session="fresh-budget",
    )
    again_visible, _again_meta = _visible(again)
    assert len(again_client.streams) == 1
    assert "接下去了。" in again_visible
    rs.reset_router_prompt_cache()


def test_rounds_keep_citations_from_every_round(monkeypatch):
    """前一輪的院內規章來源不能被後一輪的 metadata 蓋掉。相同 citation id 只留一筆。"""
    first_meta = {
        "kb_state": "searched_hit",
        "kb_hits": [{"collection_id": 7, "document_id": 21, "content": "第三條"}],
        "citations": [
            {"id": "kb:7:21:1", "title": "人事管理規則.pdf", "snippet": "第三條"},
            {"id": "kb:shared", "title": "共用", "snippet": "舊"},
        ],
    }
    second_meta = {
        "kb_state": "partial_error",
        "kb_hits": [{"collection_id": 7, "document_id": 34, "content": "第五條"}],
        "kb_failed_collections": [9],
        "citations": [
            {"id": "kb:7:34:2", "title": "差勤作業要點.docx", "snippet": "第五條"},
            {"id": "kb:shared", "title": "共用", "snippet": "新"},
        ],
    }
    body, client = _drive(
        monkeypatch,
        [
            _stream_with_meta("先引用人事規則。\nROUND: CONTINUE 再查差勤", first_meta),
            _stream_with_meta("差勤規定如下。", second_meta),
        ],
    )
    visible, meta = _visible(body)
    assert len(client.streams) == 2
    assert "先引用人事規則。" in visible
    assert "差勤規定如下。" in visible
    ids = [row["id"] for row in meta["citations"]]
    assert ids == ["kb:7:21:1", "kb:shared", "kb:7:34:2"]
    shared = [row for row in meta["citations"] if row["id"] == "kb:shared"]
    assert len(shared) == 1
    assert shared[0]["snippet"] == "舊"
    hit_ids = [row["document_id"] for row in meta["kb_hits"]]
    assert hit_ids == [21, 34]
    assert meta["kb_state"] == "partial_error"
    assert meta["kb_failed_collections"] == [9]


def test_list_item_and_inline_code_are_not_round_markers(monkeypatch):
    """清單符號與行內程式碼裡的 ROUND 行是正文，不開下一輪。"""
    samples = [
        "* ROUND: CONTINUE 下一步\n",
        "`ROUND: CONTINUE 下一步`\n",
        "**ROUND: CONTINUE 下一步**\n",
    ]
    for text in samples:
        body, client = _drive(
            monkeypatch,
            [_stream_lines(text, "stop"), _stream_lines("不該出現。", "stop")],
        )
        visible, meta = _visible(body)
        assert len(client.streams) == 1, text
        assert visible == text, text
        assert "第 2 輪" not in "".join(_stage_titles(body))
        assert (meta or {}).get("finish_reason") != "length"


def _stream_with_meta(content: str, meta: dict, finish: str = "stop") -> list[str]:
    return [
        "data: " + json.dumps({"choices": [{"delta": {"content": content}}]}),
        "event: anila.meta",
        "data: " + json.dumps(meta, ensure_ascii=False),
        "data: " + json.dumps({"choices": [{"delta": {}, "finish_reason": finish}]}),
        "data: [DONE]",
    ]


def test_default_cap_stops_after_six_rounds(monkeypatch):
    monkeypatch.delenv("ANILA_ROUND_SAFETY_CAP", raising=False)
    rs.reset_router_prompt_cache()
    scripts = [
        _stream_lines(f"第{index}段正文。\nROUND: CONTINUE 下一步{index}", "stop")
        for index in range(1, 7)
    ]
    scripts.append(_stream_lines("第七輪不該出現。", "stop"))
    body, client = _drive(monkeypatch, scripts)
    visible, meta = _visible(body)
    assert len(client.streams) == 6
    assert "第七輪不該出現" not in visible
    assert "ROUND:" not in visible
    for index in range(1, 7):
        assert f"第{index}段正文。" in visible
    assert "已達這次回合上限" in visible
    assert meta["finish_reason"] == "length"
    assert _saved_titles(meta) == [f"第 {index} 輪：下一步{index - 1}" for index in range(2, 7)]


def test_marker_inside_a_code_fence_or_quote_does_not_start_another_round(monkeypatch):
    samples = [
        "說明。\n```python\nROUND: CONTINUE 不要跑\nprint(1)\n```\n",
        "```\nROUND: CONTINUE 不要跑\n",
        "> ROUND: CONTINUE 引用的下一步\n這是答案。\n",
        "請寫 ROUND: CONTINUE 這樣的一行才會繼續。\n這是答案。\n",
        "ROUND: CONTINUE 中間這行\n後面還有答案。\n",
    ]
    for text in samples:
        body, client = _drive(monkeypatch, [_stream_lines(text, "stop"), _stream_lines("不該出現。")])
        visible, meta = _visible(body)
        assert len(client.streams) == 1, text
        assert visible == text, text
        assert "第 2 輪" not in "".join(_stage_titles(body))
        assert (meta or {}).get("finish_reason") != "length"


def test_marker_after_a_closed_fence_still_continues(monkeypatch):
    text = "```\nROUND: CONTINUE 範例\n```\nROUND: CONTINUE 真的下一步\n"
    body, client = _drive(
        monkeypatch,
        [_stream_lines(text, "stop"), _stream_lines("下一步做完了。", "stop")],
    )
    visible, _meta = _visible(body)
    assert len(client.streams) == 2
    assert "ROUND: CONTINUE 範例" in visible
    assert "ROUND: CONTINUE 真的下一步" not in visible
    assert visible.endswith("下一步做完了。")
    assert _saved_titles(_visible(body)[1]) == ["第 2 輪：真的下一步"]


def test_no_marker_is_a_single_unchanged_round(monkeypatch):
    body, client = _drive(monkeypatch, [_stream_lines("簡單回答。", "stop")])
    visible, meta = _visible(body)
    assert len(client.streams) == 1
    assert visible == "簡單回答。"
    assert _stage_titles(body) == []
    assert (meta or {}).get("thinking_stages") in (None, [])
    assert (meta or {}).get("finish_reason") != "length"
    assert meta.get("_chunk_finish") == "stop"


@pytest.mark.parametrize(
    "follow",
    [
        "ASK:要哪一章？|甲|乙\n例子在這。",
        "DISPATCH:demo:請查規定\n不要派工。",
        "RECALL:上次的報告\n這是正文。",
    ],
)
def test_followup_round_does_not_trigger_ask_dispatch_or_recall(monkeypatch, follow):
    recalls: list[str] = []

    async def no_recall(*_args, **_kwargs):
        recalls.append("called")
        return []

    monkeypatch.setattr(rs, "_fetch_recall_hits", no_recall)
    body, client = _drive(
        monkeypatch,
        [
            _stream_lines("先寫這段。\nROUND: CONTINUE 補例子", "stop"),
            _stream_lines(follow, "stop"),
        ],
    )
    visible, _meta = _visible(body)
    assert len(client.streams) == 2
    assert follow.split("\n", 1)[0] in visible
    assert "anila.interrupt_requested" not in body
    assert "找不到助手" not in body
    assert recalls == []


def test_leading_ask_still_pauses_and_does_not_start_a_round(monkeypatch):
    body, client = _drive(
        monkeypatch,
        [
            _stream_lines("ASK:要查哪一年？|2024|2025\nROUND: CONTINUE 不該發生\n", "stop"),
            _stream_lines("不該呼叫。", "stop"),
        ],
    )
    assert len(client.streams) == 1
    asked = None
    for name, data in _events(body):
        if name == "anila.interrupt_requested":
            asked = json.loads(data)
    assert asked is not None
    assert asked["payload"]["question"] == "要查哪一年？"
    visible, _meta = _visible(body)
    assert "ROUND:" not in visible
    assert "不該呼叫" not in visible


def test_stop_between_rounds_does_not_start_the_next_call(monkeypatch):
    body, client = _drive_until(
        monkeypatch,
        [
            _stream_lines("第一輪寫完。\nROUND: CONTINUE 寫第二輪", "stop"),
            _stream_lines("第二輪不該開始。", "stop"),
        ],
        lambda text: "第 2 輪" in text,
    )
    visible, _meta = _visible(body)
    assert len(client.streams) == 1
    assert "第一輪寫完。" in visible
    assert "ROUND:" not in visible
    assert "第二輪不該開始" not in visible


def test_stop_mid_round_keeps_partial_text_and_skips_the_round_after(monkeypatch):
    body, client = _drive_until(
        monkeypatch,
        [
            _stream_lines("第一輪寫完。\nROUND: CONTINUE 寫第二輪", "stop"),
            _stream_lines("第二輪開頭還有下文。", "stop"),
            _stream_lines("第三輪不該出現。", "stop"),
        ],
        lambda text: "第二輪開頭" in text,
    )
    visible, _meta = _visible(body)
    assert len(client.streams) == 2
    assert "第一輪寫完。" in visible
    assert "第二輪開頭" in visible
    assert "第三輪不該出現" not in visible
    assert "ROUND:" not in visible


def test_each_round_still_auto_continues_a_truncated_answer(monkeypatch):
    body, client = _drive(
        monkeypatch,
        [
            _stream_lines("已經寫到這裡", "length"),
            _stream_lines("。\nROUND: CONTINUE 收尾", "stop"),
            _stream_lines("收尾完成。", "stop"),
        ],
    )
    visible, meta = _visible(body)
    assert len(client.streams) == 3
    assert "已經寫到這裡。" in visible
    assert visible.endswith("收尾完成。")
    assert "ROUND:" not in visible
    assert "繼續撰寫" in _stage_titles(body)
    assert "第 2 輪：收尾" in _stage_titles(body)
    assert meta.get("finish_reason") != "length"
    users, _assistants = _user_and_assistant(client.streams[1])
    assert any("請接續上文" in text for text in users)


def test_each_round_still_rescues_an_empty_length_answer(monkeypatch):
    body, client = _drive(
        monkeypatch,
        [
            _stream_lines("分析完畢。\nROUND: CONTINUE 給答案", "stop"),
            _stream_lines("", "length", reasoning="想了很久但沒寫出來"),
            _stream_lines("這是答案。", "stop"),
        ],
    )
    visible, meta = _visible(body)
    assert len(client.streams) == 3
    assert "分析完畢。" in visible
    assert "這是答案。" in visible
    assert "ROUND:" not in visible
    assert "第 2 輪：給答案" in _stage_titles(body)
    assert "整理答案" in _stage_titles(body)
    assert meta.get("rescue", {}).get("reason")
    users, _assistants = _user_and_assistant(client.streams[2])
    assert any("不要重複" in text or "先前思考" in text for text in users)


@pytest_asyncio.fixture
async def db_path(tmp_path: Path):
    yield tmp_path / "router-rounds.db"
    await close_all_connections()


@respx.mock
def test_stored_message_is_the_joined_answer_without_markers(db_path: Path) -> None:
    """非串流回給呼叫端的那則訊息就是要存的全文：沒有標記，階段在 meta。"""
    calls = {"n": 0}

    def csp(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(
                200,
                json=_reply(
                    "STAGE: 分析需求範圍\n先分析需求。\nROUND: CONTINUE 計算總價",
                    meta={"citations": [{"id": "kb:a", "title": "甲", "snippet": "第一條"}]},
                ),
            )
        payload = json.loads(request.content.decode())
        users = [m.get("content") or "" for m in payload["messages"] if m.get("role") == "user"]
        assistants = [
            m.get("content") or "" for m in payload["messages"] if m.get("role") == "assistant"
        ]
        assert any("不要重複" in text and "計算總價" in text for text in users)
        assert any("先分析需求。" in text and "ROUND:" not in text for text in assistants)
        return httpx.Response(
            200,
            json=_reply(
                "STAGE: 算出總價\n總價是 10。",
                meta={
                    "citations": [
                        {"id": "kb:b", "title": "乙", "snippet": "第二條"},
                        {"id": "kb:a", "title": "甲", "snippet": "重複"},
                    ]
                },
            ),
        )

    respx.get(CSP_AGENTS_URL).mock(return_value=httpx.Response(200, json={"data": []}))
    respx.post(CSP_URL).mock(side_effect=csp)
    client = TestClient(create_router_app(session_db_path=str(db_path)))
    response = client.post(
        "/v1/chat/completions",
        json={
            "messages": [{"role": "user", "content": "請分步做完"}],
            "stream": False,
            "session_id": "s-rounds",
        },
        headers={"Authorization": "Bearer sk-test"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    content = body["choices"][0]["message"]["content"]
    assert content == "先分析需求。\n總價是 10。"
    assert "ROUND:" not in content
    assert "STAGE:" not in content
    titles = [row["title"] for row in body["anila_meta"]["thinking_stages"]]
    assert titles == ["分析需求範圍", "第 2 輪：計算總價", "算出總價"]
    assert all(row["status"] == "done" for row in body["anila_meta"]["thinking_stages"])
    assert body["choices"][0]["finish_reason"] == "stop"
    assert calls["n"] == 2
    cite_ids = [row["id"] for row in body["anila_meta"]["citations"]]
    assert cite_ids == ["kb:a", "kb:b"]


def _sse(content: str) -> bytes:
    chunk = {
        "choices": [{"index": 0, "delta": {"content": content}, "finish_reason": None}]
    }
    stop = {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
    return (
        "data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n"
        + "data: " + json.dumps(stop) + "\n\n"
        + "data: [DONE]\n\n"
    ).encode("utf-8")


@respx.mock
def test_resume_keeps_ask_and_recall_before_another_round(db_path: Path, monkeypatch) -> None:
    """續答的第一輪若是 ASK 或 RECALL，結尾的 ROUND 不能先開下一輪。"""
    recalls: list[str] = []

    async def remember_recall(_key, query, _conv):
        recalls.append(query)
        return [{"summary": "上次結論是用條列。"}]

    monkeypatch.setattr(rs, "_fetch_recall_hits", remember_recall)

    def csp_ask(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode())
        if payload.get("stream") is not True:
            return httpx.Response(200, json=_reply("ASK:要查哪一年？|2024|2025"))
        blob = json.dumps(payload["messages"], ensure_ascii=False)
        if "請從剛才停下的地方" in blob:
            return httpx.Response(
                200,
                content=_sse("不該開第二輪。"),
                headers={"Content-Type": "text/event-stream"},
            )
        return httpx.Response(
            200,
            content=_sse("ASK:要繼續哪一章？|甲|乙\nROUND: CONTINUE 不該開第二輪\n"),
            headers={"Content-Type": "text/event-stream"},
        )

    respx.get(CSP_AGENTS_URL).mock(return_value=httpx.Response(200, json={"data": []}))
    route = respx.post(CSP_URL).mock(side_effect=csp_ask)
    client = TestClient(create_router_app(session_db_path=str(db_path)))
    first = client.post(
        "/v1/chat/completions",
        json={
            "messages": [{"role": "user", "content": "查規章"}],
            "stream": False,
            "session_id": "s-resume-ask",
        },
        headers={"Authorization": "Bearer sk-test"},
    )
    assert first.status_code == 200, first.text
    state = client.get(
        "/v1/sessions/s-resume-ask/state",
        headers={"Authorization": "Bearer sk-test"},
    ).json()
    interrupt_id = state["pending_interrupts"][0]["id"]
    resumed = client.post(
        f"/v1/sessions/s-resume-ask/answer",
        json={"interrupt_id": interrupt_id, "answer": {"selected": ["2025"], "other_text": ""}},
        headers={"Authorization": "Bearer sk-test"},
    )
    assert resumed.status_code == 200, resumed.text
    visible, _meta = _visible(resumed.text)
    assert "不該開第二輪" not in visible
    assert "ROUND:" not in visible
    asked = None
    for name, data in _events(resumed.text):
        if name == "anila.interrupt_requested":
            asked = json.loads(data)
    assert asked is not None
    assert asked["payload"]["question"] == "要繼續哪一章？"
    # 最初那則非串流，加上續答的第一輪。沒有第三輪。
    assert route.call_count == 2

    def csp_recall(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode())
        blob = json.dumps(payload["messages"], ensure_ascii=False)
        # 搜尋後的那一輪是非串流。摘要在訊息裡才算這一次，否則會被當成最初的 ASK。
        if "上次結論是用條列" in blob:
            if payload.get("stream") is True:
                return httpx.Response(
                    200,
                    content=_sse("搜尋後的答案。"),
                    headers={"Content-Type": "text/event-stream"},
                )
            return httpx.Response(200, json=_reply("搜尋後的答案。"))
        if payload.get("stream") is not True:
            return httpx.Response(200, json=_reply("ASK:要查哪一份？|報告|備忘"))
        return httpx.Response(
            200,
            content=_sse("RECALL:上次的報告\nROUND: CONTINUE 不該先開第二輪\n"),
            headers={"Content-Type": "text/event-stream"},
        )

    route.side_effect = csp_recall
    recalls.clear()
    before = route.call_count
    first = client.post(
        "/v1/chat/completions",
        json={
            "messages": [{"role": "user", "content": "延續上次"}],
            "stream": False,
            "session_id": "s-resume-recall",
        },
        headers={"Authorization": "Bearer sk-test"},
    )
    assert first.status_code == 200, first.text
    state = client.get(
        "/v1/sessions/s-resume-recall/state",
        headers={"Authorization": "Bearer sk-test"},
    ).json()
    interrupt_id = state["pending_interrupts"][0]["id"]
    resumed = client.post(
        "/v1/sessions/s-resume-recall/answer",
        json={"interrupt_id": interrupt_id, "answer": {"selected": ["報告"], "other_text": ""}},
        headers={"Authorization": "Bearer sk-test"},
    )
    assert resumed.status_code == 200, resumed.text
    visible, _meta = _visible(resumed.text)
    assert recalls == ["上次的報告"]
    assert "搜尋後的答案。" in visible
    assert "ROUND:" not in visible
    assert "不該先開第二輪" not in visible
    # 最初的 ASK、續答的 RECALL 輪、搜尋後的那一輪。沒有標記觸發的額外輪。
    assert route.call_count - before == 3
