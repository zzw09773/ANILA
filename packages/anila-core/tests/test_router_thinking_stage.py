"""生成模型自報的階段標題。

階段行是 ``STAGE:`` 開頭的一整行。切在任何字元邊界，畫面上的答案、
思考摺疊與 ``anila.thinking_stage`` 事件都要相同。沒有這一行時，
不送階段事件。召回與救援走同一條階段清單。
"""

from __future__ import annotations

import asyncio
import json
import random
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
CSP_RECALL_URL = f"{settings.csp_base_url}/api/memory/recall"

_CONTENT = "STAGE: 拆解使用者需求\n先寫範圍。\nSTAGE: 起草第一章節\n第一章草稿。"
_REASONING = "STAGE: 閱讀題目範圍\n先看題目。\nSTAGE: 排列章節順序\n再列章節。\n"
_PLAIN = "這是一段沒有階段標記的普通回答。"


def _splits(text: str) -> list[tuple[str, list[str]]]:
    """整段、每個兩段切點、逐字、以及幾次固定種子的隨機切法。"""
    out: list[tuple[str, list[str]]] = [("whole", [text]), ("chars", list(text))]
    for index in range(1, len(text)):
        out.append((f"cut-{index}", [text[:index], text[index:]]))
    rng = random.Random(0)
    for nth in range(6):
        if len(text) < 2:
            pieces = [text]
        else:
            width = min(8, len(text) - 1)
            points = sorted(rng.sample(range(1, len(text)), k=rng.randint(1, width)))
            pieces: list[str] = []
            previous = 0
            for point in points:
                pieces.append(text[previous:point])
                previous = point
            pieces.append(text[previous:])
        out.append((f"rand-{nth}", pieces))
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


def _visible(body: str) -> tuple[str, str, dict | None]:
    text: list[str] = []
    reasoning: list[str] = []
    meta = None
    for name, data in _events(body):
        if name == "anila.reasoning":
            delta = json.loads(data).get("delta")
            if delta:
                reasoning.append(delta)
        elif name == "anila.meta":
            meta = json.loads(data)
        elif name == "message" and data != "[DONE]":
            payload = json.loads(data)
            for choice in payload.get("choices") or []:
                piece = (choice.get("delta") or {}).get("content")
                if piece:
                    text.append(piece)
    return "".join(text), "".join(reasoning), meta


def _stage_events(body: str) -> list[dict]:
    rows: list[dict] = []
    for name, data in _events(body):
        if name == "anila.thinking_stage":
            rows.append(json.loads(data))
    return rows


class _AnyAgent:
    def get(self, _api_key: str, agent_id: str):
        return RemoteAgentManifest(
            agent_id=agent_id,
            name=agent_id,
            description_for_router="demo",
            endpoint_url="http://agents.invalid",
        )


def _drive(
    monkeypatch,
    *,
    content: list[str],
    reasoning: list[str] | None = None,
    extra: list[dict] | None = None,
    route: str | None = None,
) -> str:
    """把假上游切成指定的 reasoning／content 片段，跑一次串流路由。"""
    script: list[dict] = []
    for piece in reasoning or []:
        if piece:
            script.append({"type": "reasoning", "content": piece})
    for piece in content:
        if piece:
            script.append({"type": "delta", "content": piece})
    if extra:
        script.extend(extra)
    else:
        script.append({"type": "done", "finish_reason": "stop"})

    async def fake_stream(*_args, **_kwargs):
        for item in script:
            yield item

    monkeypatch.setattr(rs, "_stream_llm_sse", fake_stream)

    async def run() -> str:
        parts: list[str] = []
        async for line in rs._router_streaming(
            "sk",
            [{"role": "user", "content": "問題"}],
            [{"role": "user", "content": "問題"}],
            registry=_AnyAgent(),
            base_trace=[],
            started_at=0.0,
            session=MemorySession("stages"),
            route_signal=route or rs._ROUTE_DIRECT,
        ):
            parts.append(line)
        return "".join(parts)

    return asyncio.run(run())


def test_stage_rule_sits_on_the_non_editable_tail(monkeypatch):
    """階段規則跟日期、不得外洩一樣附在組好的提示後面，治理中心改不到。"""
    for text in (
        rp.DEFAULT_ROUTER_SYSTEM,
        rp.DEFAULT_PLAIN_ASSISTANT,
        rp.DEFAULT_FORCED_ANSWER,
    ):
        assert "STAGE:" not in text
        assert "每進入一個新步驟" not in text

    zh = rs._build_system_prompt([])
    forced = rs._forced_answer_prompt()
    for text in (zh, forced):
        assert "每進入一個新步驟" in text
        assert "STAGE:" in text
        assert "10 到 20 字" in text
        assert "寫在思考裡" in text
        assert "該段回答的開頭" in text
        assert "不要寫內部細節" in text
        assert text.index(rp.DISCLOSURE_RULE_ZH) < text.index("STAGE:")
        assert text.index("STAGE:") < text.rindex("今天是")
        assert text.rstrip().endswith("Asia/Taipei。")

    monkeypatch.setattr(
        rs,
        "current_router_prompts",
        lambda: {
            rp.KEY_SYSTEM: "Route the request.\n{agent_list}",
            rp.KEY_PLAIN: "Answer in English.",
            rp.KEY_FORCED: "Answer from the regulations.",
        },
    )
    plain = rs._build_system_prompt([])
    forced_en = rs._forced_answer_prompt()
    for text in (plain, forced_en):
        assert "STAGE:" in text
        assert "whenever you move to a new step" in text
        assert "10 to 20" in text
        assert "in the reasoning" in text
        assert "start of that part of the answer" in text
        assert "no internal details" in text
        assert text.index(rp.DISCLOSURE_RULE_EN) < text.index("STAGE:")
        assert text.index("STAGE:") < text.rindex("Today is")
        assert "每進入一個新步驟" not in text
    stored = rs.current_router_prompts()
    assert "STAGE:" not in stored[rp.KEY_PLAIN]
    assert "STAGE:" not in stored[rp.KEY_FORCED]
    assert "STAGE:" not in stored[rp.KEY_SYSTEM]


def test_no_stage_line_keeps_today_behavior(monkeypatch):
    """模型沒寫 STAGE 時，不送階段事件，答案與現在一樣整段送出。"""
    body = _drive(monkeypatch, content=[_PLAIN])
    visible, reasoning, meta = _visible(body)
    assert visible == _PLAIN
    assert reasoning == ""
    assert _stage_events(body) == []
    assert not (meta or {}).get("thinking_stages")


def test_stage_lines_are_invariant_across_chunk_splits(monkeypatch):
    """正文裡的 STAGE 行不論怎麼切，階段清單與可見答案都一樣，而且答案沒有 STAGE。"""
    expected_titles = ["拆解使用者需求", "起草第一章節"]
    expected_visible = "先寫範圍。\n第一章草稿。"
    first: list[dict] | None = None
    for label, pieces in _splits(_CONTENT):
        body = _drive(monkeypatch, content=pieces)
        visible, reasoning, meta = _visible(body)
        stages = _stage_events(body)
        assert visible == expected_visible, label
        assert "STAGE:" not in visible, label
        assert reasoning == "", label
        assert [row["title"] for row in stages if row["status"] == "running"] == expected_titles
        assert stages[-1]["status"] == "done"
        assert stages[-2]["status"] == "running"
        assert stages[-2]["title"] == expected_titles[-1]
        # 新階段開始時，前一個先變成 done。
        assert stages[0] == {"index": 0, "title": expected_titles[0], "status": "running"}
        assert stages[1] == {"index": 0, "title": expected_titles[0], "status": "done"}
        assert stages[2] == {"index": 1, "title": expected_titles[1], "status": "running"}
        assert stages[3] == {"index": 1, "title": expected_titles[1], "status": "done"}
        saved = (meta or {}).get("thinking_stages")
        assert [row["title"] for row in saved] == expected_titles
        assert all(row["status"] == "done" for row in saved)
        if first is None:
            first = stages
        else:
            assert stages == first, label


def test_reasoning_stage_lines_are_stripped_and_chunk_invariant(monkeypatch):
    """思考通道的 STAGE 行也解析，並且不留在原始思考裡。"""
    expected_titles = ["閱讀題目範圍", "排列章節順序"]
    expected_reasoning = "先看題目。\n再列章節。\n"
    first: list[dict] | None = None
    for label, pieces in _splits(_REASONING):
        body = _drive(monkeypatch, content=["這是回答。"], reasoning=pieces)
        visible, reasoning, meta = _visible(body)
        stages = _stage_events(body)
        assert visible == "這是回答。", label
        assert reasoning == expected_reasoning, label
        assert "STAGE:" not in reasoning, label
        assert "STAGE:" not in visible, label
        assert [row["title"] for row in stages if row["status"] == "running"] == expected_titles
        assert all(row["status"] == "done" for row in (meta or {}).get("thinking_stages") or [])
        assert ((meta or {}).get("reasoning") or "").strip() == expected_reasoning.strip()
        if first is None:
            first = stages
        else:
            assert stages == first, label


def test_reasoning_stages_precede_content_stages(monkeypatch):
    """同一輪的思考階段與正文階段共用一條編號。"""
    body = _drive(
        monkeypatch,
        reasoning=["STAGE: 閱讀題目範圍\n先看題目。\n"],
        content=["STAGE: 起草第一章節\n第一章草稿。"],
    )
    visible, reasoning, meta = _visible(body)
    assert visible == "第一章草稿。"
    assert "STAGE:" not in reasoning
    assert "STAGE:" not in visible
    titles = [row["title"] for row in (meta or {}).get("thinking_stages") or []]
    assert titles == ["閱讀題目範圍", "起草第一章節"]
    assert [row["index"] for row in meta["thinking_stages"]] == [0, 1]


def test_quoted_stage_inside_a_sentence_stays_in_the_answer(monkeypatch):
    """只有整行的階段標記會拿掉。句子裡提到這個寫法要留著。"""
    text = "這句提到 STAGE: 只是說明格式。\nSTAGE: 開始撰寫正文\n正文在這裡。"
    body = _drive(monkeypatch, content=[text])
    visible, _reasoning, meta = _visible(body)
    assert visible == "這句提到 STAGE: 只是說明格式。\n正文在這裡。"
    assert [row["title"] for row in meta["thinking_stages"]] == ["開始撰寫正文"]


def test_stage_line_before_ask_still_pauses(monkeypatch):
    body = _drive(
        monkeypatch,
        content=["STAGE: 確認缺的年份\nASK:要查哪一年？|2024|2025\n"],
    )
    visible, _reasoning, meta = _visible(body)
    assert "STAGE:" not in visible
    assert "ASK:" not in visible
    interrupt = None
    for name, data in _events(body):
        if name == "anila.interrupt_requested":
            interrupt = json.loads(data)
    assert interrupt is not None
    assert interrupt["payload"]["question"] == "要查哪一年？"
    assert [row["title"] for row in meta["thinking_stages"]] == ["確認缺的年份"]
    assert meta["thinking_stages"][0]["status"] == "done"


def test_stage_line_before_dispatch_still_routes(monkeypatch):
    calls: list[tuple[str, str]] = []

    async def fake_agent(agent_id, query, *_args, **_kwargs):
        calls.append((agent_id, query))
        yield {"type": "content", "content": "AGENT-OK"}
        yield {"type": "done"}

    monkeypatch.setattr(rs, "_stream_agent_sse", fake_agent)
    body = _drive(
        monkeypatch,
        content=["STAGE: 交給法規助手\nDISPATCH:demo:請查規定\n"],
    )
    visible, reasoning, meta = _visible(body)
    assert calls == [("demo", "請查規定")]
    assert visible == "AGENT-OK"
    assert "STAGE:" not in visible
    assert "DISPATCH:" not in visible
    assert "STAGE:" not in (reasoning or "")
    assert [row["title"] for row in (meta or {}).get("thinking_stages") or []] == ["交給法規助手"]


def _asked(body: str) -> dict | None:
    for name, data in _events(body):
        if name == "anila.interrupt_requested":
            return json.loads(data)
    return None


def _install_agent(monkeypatch) -> list[tuple[str, str]]:
    calls: list[tuple[str, str]] = []

    async def fake_agent(agent_id, query, *_args, **_kwargs):
        calls.append((agent_id, query))
        yield {"type": "content", "content": "AGENT-OK"}
        yield {"type": "done"}

    monkeypatch.setattr(rs, "_stream_agent_sse", fake_agent)
    return calls


def test_blockquote_stage_line_stays_and_does_not_open_a_directive(monkeypatch):
    """以 > 引用的 STAGE 行是正文。拿掉它會讓下一行 ASK 或 DISPATCH 變成協定。"""
    calls = _install_agent(monkeypatch)
    samples = [
        "> STAGE: 引用的範例\nASK:要查哪一年？|2024|2025\n這是答案。\n",
        "> STAGE: 引用的範例\nDISPATCH:demo:請查規定\n這是答案。\n",
        "   > STAGE: 引用的範例\nSTAGE: 開始撰寫正文\n這是答案。\n",
    ]
    for text in samples:
        calls.clear()
        first: tuple[str, list[dict]] | None = None
        for label, pieces in _splits(text):
            body = _drive(monkeypatch, content=pieces)
            visible, _reasoning, meta = _visible(body)
            stages = _stage_events(body)
            assert visible == text.replace("STAGE: 開始撰寫正文\n", ""), label
            assert "引用的範例" not in "".join(row["title"] for row in stages), label
            assert _asked(body) is None, label
            assert calls == [], label
            saved = [row["title"] for row in (meta or {}).get("thinking_stages") or []]
            if "開始撰寫正文" in text:
                assert saved == ["開始撰寫正文"], label
            else:
                assert saved == [], label
            observed = (visible, stages)
            if first is None:
                first = observed
            else:
                assert observed == first, label


def test_fenced_stage_line_stays_across_chunk_splits(monkeypatch):
    """程式碼區塊裡的 STAGE 行留在答案裡。欄位狀態要跨片段記住。"""
    calls = _install_agent(monkeypatch)
    samples = [
        (
            "說明在這。\n"
            "```python\n"
            "STAGE: 區塊裡的範例\n"
            "ASK:要查哪一年？|2024|2025\n"
            "```\n"
            "STAGE: 開始撰寫正文\n"
            "這是答案。\n",
            ["開始撰寫正文"],
        ),
        (
            "~~~\n"
            "STAGE: 波浪裡的範例\n"
            "DISPATCH:demo:請查規定\n"
            "~~~\n"
            "這是答案。\n",
            [],
        ),
        (
            "```\n"
            "STAGE: 還沒關的範例\n"
            "print('x')\n",
            [],
        ),
        (
            "````\n"
            "STAGE: 四個反引號裡\n"
            "```\n"
            "還在區塊。\n"
            "````\n"
            "STAGE: 開始撰寫正文\n"
            "正文。\n",
            ["開始撰寫正文"],
        ),
    ]
    for text, titles in samples:
        calls.clear()
        expected = text
        for title in titles:
            expected = expected.replace(f"STAGE: {title}\n", "")
        first: tuple[str, list[dict]] | None = None
        for label, pieces in _splits(text):
            body = _drive(monkeypatch, content=pieces)
            visible, _reasoning, meta = _visible(body)
            stages = _stage_events(body)
            assert visible == expected, label
            assert _asked(body) is None, label
            assert calls == [], label
            saved = [row["title"] for row in (meta or {}).get("thinking_stages") or []]
            assert saved == titles, label
            running = [row["title"] for row in stages if row["status"] == "running"]
            assert running == titles, label
            observed = (visible, stages)
            if first is None:
                first = observed
            else:
                assert observed == first, label


def test_info_string_with_a_backtick_does_not_open_a_fence(monkeypatch):
    """反引號資訊字串裡再出現反引號就不是程式碼區塊，後面的階段行仍然算。"""
    text = "```python `不是欄\nSTAGE: 開始撰寫正文\n正文。\n"
    body = _drive(monkeypatch, content=[text])
    visible, _reasoning, meta = _visible(body)
    assert visible == "```python `不是欄\n正文。\n"
    assert [row["title"] for row in meta["thinking_stages"]] == ["開始撰寫正文"]


def test_stage_titles_drop_internal_details_and_keep_the_previous_stage(monkeypatch):
    """標題裡的路徑、檔名、位址、主機與環境變數要濾掉；濾完是空的就不開新階段。"""
    long_title = "章" * 50
    text = (
        "STAGE: 先看題目範圍\n"
        "STAGE: ANILA_TRACE_TOKEN\n"
        "STAGE: 讀 router_server.py 一節\n"
        "STAGE: 略過 10.1.2.3 與 csp.internal\n"
        "STAGE: 規劃 /opt/anila/x.py 結構\n"
        f"STAGE: {long_title}\n"
        "正文。\n"
    )
    expected = [
        ("先看題目範圍", "running"),
        ("先看題目範圍", "done"),
        ("讀 一節", "running"),
        ("讀 一節", "done"),
        ("略過 與", "running"),
        ("略過 與", "done"),
        ("規劃 結構", "running"),
        ("規劃 結構", "done"),
        ("章" * 40, "running"),
        ("章" * 40, "done"),
    ]
    first: list[dict] | None = None
    for label, pieces in (("whole", [text]), ("chars", list(text))):
        body = _drive(monkeypatch, content=pieces)
        visible, _reasoning, meta = _visible(body)
        stages = _stage_events(body)
        blob = json.dumps({"stages": stages, "meta": meta}, ensure_ascii=False)
        assert visible == "正文。\n", label
        assert [(row["title"], row["status"]) for row in stages] == expected, label
        assert [row["title"] for row in meta["thinking_stages"]] == [
            "先看題目範圍",
            "讀 一節",
            "略過 與",
            "規劃 結構",
            "章" * 40,
        ], label
        assert all(len(row["title"]) <= 40 for row in meta["thinking_stages"])
        for secret in (
            "ANILA_TRACE_TOKEN",
            "router_server.py",
            "/opt/anila",
            "10.1.2.3",
            "csp.internal",
        ):
            assert secret not in blob, secret
        if first is None:
            first = stages
        else:
            assert stages == first


def test_stream_error_marks_the_running_stage(monkeypatch):
    body = _drive(
        monkeypatch,
        content=[],
        reasoning=["STAGE: 拆解使用者需求\n先看題目。"],
        extra=[{"type": "error", "error": "LLM connection: ReadTimeout", "detail": "timed out"}],
    )
    stages = _stage_events(body)
    assert stages[0]["status"] == "running"
    assert stages[-1] == {"index": 0, "title": "拆解使用者需求", "status": "error"}
    _visible_text, reasoning, meta = _visible(body)
    assert "STAGE:" not in reasoning
    saved = (meta or {}).get("thinking_stages") or stages
    assert saved[-1]["status"] == "error"


def test_recall_and_rescue_join_the_stage_list(monkeypatch):
    """召回與救援用同一種階段事件，標題就是畫面上那兩個步驟。"""

    async def fake_hits(*_args, **_kwargs):
        return [{"summary": "上次報告的結論是用條列。"}]

    async def fake_second(*_args, **_kwargs):
        return {
            "content": "STAGE: 整理舊結論\n找到了，用條列。",
            "reasoning": "STAGE: 對照摘要\n摘要說用條列。",
            "error": None,
            "anila_meta": None,
        }

    monkeypatch.setattr(rs, "_fetch_recall_hits", fake_hits)
    monkeypatch.setattr(rs, "_call_llm_non_stream", fake_second)
    body = _drive(monkeypatch, content=["RECALL:上次的報告\n"])
    visible, reasoning, meta = _visible(body)
    assert "RECALL:" not in visible
    assert "STAGE:" not in visible
    assert "STAGE:" not in reasoning
    assert "用條列" in visible
    titles = [row["title"] for row in meta["thinking_stages"]]
    assert titles[0] == "搜尋過往對話"
    assert "整理舊結論" in titles
    assert "對照摘要" in titles
    assert all(row["status"] == "done" for row in meta["thinking_stages"])
    # 搜尋那一步有跑起來、也有收束，不是只在最後補一筆。
    recall_rows = [row for row in _stage_events(body) if row["title"] == "搜尋過往對話"]
    assert [row["status"] for row in recall_rows] == ["running", "done"]

    rescue = _drive(
        monkeypatch,
        content=[],
        reasoning=["STAGE: 構思回答大綱\n大綱想好了。"],
        extra=[
            {"type": "rescue", "reason": "reasoning_exhausted"},
            {"type": "delta", "content": "STAGE: 寫出可用答案\n這是答案。"},
            {"type": "done", "finish_reason": "stop"},
        ],
    )
    rescue_visible, rescue_reasoning, rescue_meta = _visible(rescue)
    assert rescue_visible == "這是答案。"
    assert "STAGE:" not in rescue_visible
    assert "STAGE:" not in rescue_reasoning
    rescue_titles = [row["title"] for row in rescue_meta["thinking_stages"]]
    assert rescue_titles == ["構思回答大綱", "整理答案", "寫出可用答案"]
    assert all(row["status"] == "done" for row in rescue_meta["thinking_stages"])


@pytest_asyncio.fixture
async def db_path(tmp_path: Path):
    db = tmp_path / "router-stages.db"
    yield db
    await close_all_connections()


def _sse_completion(content: str, *, reasoning: str = "", finish: str = "stop") -> bytes:
    frames: list[str] = []
    if reasoning:
        chunk = {
            "id": "chatcmpl-s",
            "object": "chat.completion.chunk",
            "model": "router-llm",
            "choices": [
                {
                    "index": 0,
                    "delta": {"reasoning_content": reasoning},
                    "finish_reason": None,
                }
            ],
        }
        frames.append("data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n")
    if content:
        # 刻意從 STAGE 的中間切開，確認真的串流解析也吃得下。
        cut = content.index("STAGE:") + 3 if "STAGE:" in content else 1
        for piece in (content[:cut], content[cut:]):
            if not piece:
                continue
            chunk = {
                "id": "chatcmpl-s",
                "object": "chat.completion.chunk",
                "model": "router-llm",
                "choices": [
                    {"index": 0, "delta": {"content": piece}, "finish_reason": None}
                ],
            }
            frames.append("data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n")
    stop = {
        "id": "chatcmpl-s",
        "object": "chat.completion.chunk",
        "model": "router-llm",
        "choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
    }
    frames.append("data: " + json.dumps(stop) + "\n\n")
    frames.append("data: [DONE]\n\n")
    return "".join(frames).encode("utf-8")


def _completion(content: str, *, reasoning: str = "", finish: str = "stop") -> dict:
    message: dict = {"role": "assistant", "content": content}
    if reasoning:
        message["reasoning_content"] = reasoning
    return {
        "id": "chatcmpl-s",
        "object": "chat.completion",
        "model": "router-llm",
        "choices": [{"index": 0, "message": message, "finish_reason": finish}],
    }


@respx.mock
def test_non_stream_strips_stage_lines_and_stores_them_on_meta(db_path: Path) -> None:
    respx.get(CSP_AGENTS_URL).mock(return_value=httpx.Response(200, json={"data": []}))
    respx.post(CSP_URL).mock(
        return_value=httpx.Response(
            200,
            json=_completion(
                "STAGE: 起草第一章節\n第一章草稿。",
                reasoning="STAGE: 閱讀題目範圍\n先看題目。",
            ),
        )
    )
    client = TestClient(create_router_app(session_db_path=str(db_path)))
    response = client.post(
        "/v1/chat/completions",
        json={
            "messages": [{"role": "user", "content": "寫第一章"}],
            "stream": False,
            "session_id": "s-stage",
        },
        headers={"Authorization": "Bearer sk-test"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["choices"][0]["message"]["content"] == "第一章草稿。"
    assert "STAGE:" not in body["choices"][0]["message"]["content"]
    meta = body["anila_meta"]
    assert "STAGE:" not in (meta.get("reasoning") or "")
    assert [row["title"] for row in meta["thinking_stages"]] == [
        "閱讀題目範圍",
        "起草第一章節",
    ]
    assert all(row["status"] == "done" for row in meta["thinking_stages"])


@respx.mock
def test_resume_strips_stage_lines_and_emits_stages(db_path: Path) -> None:
    """ASK 續答仍是同一則工作階段；續答裡的 STAGE 不進已存內容，也不進答案。"""
    calls = {"n": 0}

    def csp(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(200, json=_completion("ASK:要查哪一年？|2024|2025"))
        payload = json.loads(request.content.decode())
        assert payload.get("stream") is True
        return httpx.Response(
            200,
            content=_sse_completion(
                "STAGE: 依回答寫說明\n2025 年的規章如下。",
                reasoning="STAGE: 接上使用者的選擇\n選了 2025。",
            ),
            headers={"Content-Type": "text/event-stream"},
        )

    respx.get(CSP_AGENTS_URL).mock(return_value=httpx.Response(200, json={"data": []}))
    respx.post(CSP_URL).mock(side_effect=csp)
    client = TestClient(create_router_app(session_db_path=str(db_path)))
    first = client.post(
        "/v1/chat/completions",
        json={
            "messages": [{"role": "user", "content": "查規章"}],
            "stream": False,
            "session_id": "s-resume-stage",
        },
        headers={"Authorization": "Bearer sk-test"},
    )
    assert first.status_code == 200, first.text
    state = client.get(
        "/v1/sessions/s-resume-stage/state",
        headers={"Authorization": "Bearer sk-test"},
    ).json()
    interrupt_id = state["pending_interrupts"][0]["id"]
    resumed = client.post(
        "/v1/sessions/s-resume-stage/answer",
        json={
            "interrupt_id": interrupt_id,
            "answer": {"selected": ["2025"], "other_text": ""},
        },
        headers={"Authorization": "Bearer sk-test"},
    )
    assert resumed.status_code == 200, resumed.text
    visible, reasoning, meta = _visible(resumed.text)
    assert "anila.resumed" in resumed.text
    assert visible == "2025 年的規章如下。"
    assert "STAGE:" not in visible
    assert "STAGE:" not in reasoning
    assert [row["title"] for row in meta["thinking_stages"]] == [
        "接上使用者的選擇",
        "依回答寫說明",
    ]
    assert all(row["status"] == "done" for row in meta["thinking_stages"])
    stored = client.get(
        "/v1/sessions/s-resume-stage/state",
        headers={"Authorization": "Bearer sk-test"},
    ).json()
    blob = json.dumps(stored, ensure_ascii=False)
    assert "STAGE:" not in blob
    assert "依回答寫說明" not in blob or "STAGE:" not in blob
