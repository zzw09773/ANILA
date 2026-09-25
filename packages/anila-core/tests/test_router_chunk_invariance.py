"""Chunk boundaries must not change a streaming routing decision.

The detecting loop used to commit to a direct answer at 12 characters unless
the buffer, after ``lstrip``, already began with ``DISPATCH:``. A delta that
ended inside a directive — leading blank lines, or a ``**`` / ``>`` / backtick
wrapper, then ``DISPATC`` — became an answer, while the same text in smaller
deltas dispatched. ASK already tolerated those wrappers and DISPATCH did not.

For each upstream reply below, the whole text, every two-way cut, a
one-character split, and several seeded random splits must produce the same
decision (direct answer, the same agent and query, or the same ASK question,
options, and multi flag) and the same user-visible text. ``ASK*:`` is the
multi-select form and has to survive the same splits, including a cut
between ``ASK`` and ``*``.
"""

from __future__ import annotations

import asyncio
import json
import random

import pytest

from anila_core.api import router_server as rs
from anila_core.memory import MemorySession
from anila_core.registry.remote_agent_manifest import RemoteAgentManifest


_AGENT_REPLY = "AGENT-OK"
_CJK_QUERY = "請查閱差旅規定並說明申請程序與核銷期限" * 3


def _splits(text: str) -> list[tuple[str, list[str]]]:
    """Whole text, every two-way cut, one-character deltas, seeded random cuts."""
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
            pieces = []
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


def _visible(body: str) -> tuple[str, str, dict | None, dict | None]:
    """Visible answer text, joined ``anila.reasoning`` deltas, meta, interrupt."""
    text: list[str] = []
    reasoning: list[str] = []
    meta = None
    interrupt = None
    for name, data in _events(body):
        if name == "anila.reasoning":
            delta = json.loads(data).get("delta")
            if delta:
                reasoning.append(delta)
        elif name == "anila.meta":
            meta = json.loads(data)
        elif name == "anila.interrupt_requested":
            interrupt = json.loads(data)
        elif name == "message" and data != "[DONE]":
            payload = json.loads(data)
            for choice in payload.get("choices") or []:
                piece = (choice.get("delta") or {}).get("content")
                if piece:
                    text.append(piece)
    return "".join(text), "".join(reasoning), meta, interrupt


class _AnyAgent:
    def get(self, _api_key: str, agent_id: str):
        return RemoteAgentManifest(
            agent_id=agent_id,
            name=agent_id,
            description_for_router="demo",
            endpoint_url="http://agents.invalid",
        )


@pytest.fixture
def stream_box(monkeypatch):
    """Install the upstream fakes once; each drive fills ``box``."""
    box: dict = {"deltas": [], "calls": [], "fed": 0, "first": None}
    original_chunk = rs._make_chunk

    def _chunk(content, model, finish=None):
        if content and box["first"] is None:
            box["first"] = box["fed"]
        return original_chunk(content, model, finish=finish)

    async def fake_stream(*_args, **_kwargs):
        for piece in box["deltas"]:
            box["fed"] += 1
            yield {"type": "delta", "content": piece}
        yield {"type": "done", "finish_reason": "stop"}

    async def fake_agent(agent_id, query, *_args, **_kwargs):
        box["calls"].append((agent_id, query))
        yield {"type": "content", "content": _AGENT_REPLY}
        yield {"type": "done"}

    async def fake_recompose(content, _api_key, *, forwarded_headers=None):
        return content, "skipped"

    monkeypatch.setattr(rs, "_make_chunk", _chunk)
    monkeypatch.setattr(rs, "_stream_llm_sse", fake_stream)
    monkeypatch.setattr(rs, "_stream_agent_sse", fake_agent)
    monkeypatch.setattr(rs, "_recompose_reply", fake_recompose)
    return box


def _drive(
    box: dict, deltas: list[str], *, route: str
) -> tuple[tuple, str, str, str | None, int | None]:
    box["deltas"] = deltas
    box["calls"] = []
    box["fed"] = 0
    box["first"] = None

    async def run() -> str:
        parts: list[str] = []
        async for line in rs._router_streaming(
            "sk",
            [{"role": "user", "content": "問題"}],
            [{"role": "user", "content": "問題"}],
            registry=_AnyAgent(),
            base_trace=[],
            started_at=0.0,
            session=MemorySession("chunk"),
            route_signal=route,
        ):
            parts.append(line)
        return "".join(parts)

    visible, reasoning, meta, interrupt = _visible(asyncio.run(run()))
    if box["calls"]:
        agent_id, query = box["calls"][0]
        decision: tuple = ("dispatch", agent_id, query, len(box["calls"]))
    elif interrupt is not None:
        payload = interrupt["payload"]
        options = tuple(item["value"] for item in payload.get("options") or [])
        decision = (
            "ask",
            payload.get("question"),
            options,
            payload.get("multi") is True,
        )
    else:
        decision = ("direct",)
    meta_reasoning = None if meta is None else meta.get("reasoning")
    return decision, visible, reasoning, meta_reasoning, box["first"]


# Representative upstream replies. ``early`` is how many one-character deltas
# may arrive before a normal answer's first visible chunk (the head is no
# longer a directive or thought prefix). Dispatch and ASK stay buffered until
# the line is finished, so they have no ``early`` bound.
_CASES = [
    {
        "id": "answer-d",
        "text": "Definitely a complete sentence about the topic.",
        "route": rs._ROUTE_DIRECT,
        "decision": ("direct",),
        "visible": "Definitely a complete sentence about the topic.",
        "early": 2,
    },
    {
        "id": "answer-a",
        "text": "A normal reply that happens to begin with A.",
        "route": rs._ROUTE_DIRECT,
        "decision": ("direct",),
        "visible": "A normal reply that happens to begin with A.",
        "early": 2,
    },
    {
        "id": "answer-as",
        "text": "ASAP is not a question directive, just prose here.",
        "route": rs._ROUTE_DIRECT,
        "decision": ("direct",),
        "visible": "ASAP is not a question directive, just prose here.",
        "early": 3,
    },
    {
        "id": "answer-dis",
        "text": "DIS is not a dispatch directive at all, just prose.",
        "route": rs._ROUTE_DIRECT,
        "decision": ("direct",),
        "visible": "DIS is not a dispatch directive at all, just prose.",
        "early": 4,
    },
    {
        "id": "answer-zh",
        "text": "這是一段中文回答，不需要分派給任何人。",
        "route": rs._ROUTE_DIRECT,
        "decision": ("direct",),
        "visible": "這是一段中文回答，不需要分派給任何人。",
        "early": 1,
    },
    {
        "id": "quote-dispatch-later",
        "text": "這是普通回答，後面那行只是在引用語法。\nDISPATCH:weather:今天天氣\n",
        "route": rs._ROUTE_DIRECT,
        "decision": ("direct",),
        "visible": "這是普通回答，後面那行只是在引用語法。\nDISPATCH:weather:今天天氣\n",
        "early": 1,
    },
    {
        "id": "dispatch-plain",
        "text": "DISPATCH:weather:今天天氣\n",
        "route": rs._ROUTE_DIRECT,
        "decision": ("dispatch", "weather", "今天天氣", 1),
        "visible": _AGENT_REPLY,
    },
    {
        "id": "dispatch-bold",
        "text": "**DISPATCH:weather:今天天氣**\n",
        "route": rs._ROUTE_DIRECT,
        # The closing stars sit inside the query capture; every split must
        # agree with the offline parser, not invent a cleaned query.
        "decision": ("dispatch", "weather", "今天天氣**", 1),
        "visible": _AGENT_REPLY,
    },
    {
        "id": "dispatch-blanks",
        "text": "\n\nDISPATCH:weather:今天天氣\n",
        "route": rs._ROUTE_DIRECT,
        "decision": ("dispatch", "weather", "今天天氣", 1),
        "visible": _AGENT_REPLY,
    },
    {
        "id": "ask-blanks",
        "text": "\n\nASK:要哪一種？|晴天|雨天\n",
        "route": rs._ROUTE_DIRECT,
        "decision": ("ask", "要哪一種？", ("晴天", "雨天"), False),
        "visible": "",
    },
    {
        "id": "ask-blockquote",
        "text": "> ASK:要哪一種？|晴天|雨天\n",
        "route": rs._ROUTE_DIRECT,
        "decision": ("ask", "要哪一種？", ("晴天", "雨天"), False),
        "visible": "",
    },
    {
        # One-character cuts include the boundary between ``ASK`` and ``*``.
        # That prefix has to stay held, or the char stream answers while one
        # chunk pauses.
        "id": "ask-multi",
        "text": "ASK*:要挑哪幾個來拆成三種版本？|一 環境感測器|六 電網天線|全都要\n",
        "route": rs._ROUTE_DIRECT,
        "decision": (
            "ask",
            "要挑哪幾個來拆成三種版本？",
            ("一 環境感測器", "六 電網天線", "全都要"),
            True,
        ),
        "visible": "",
    },
    {
        "id": "ask-multi-wrapped",
        "text": "\n\n> ASK*:要挑哪幾個？|甲|乙\n",
        "route": rs._ROUTE_DIRECT,
        "decision": ("ask", "要挑哪幾個？", ("甲", "乙"), True),
        "visible": "",
    },
    {
        "id": "thought-dispatch",
        "text": "thought\nThe user wants the weather.\nDISPATCH:weather:今天天氣\n",
        "route": rs._ROUTE_DIRECT,
        "decision": ("dispatch", "weather", "今天天氣", 1),
        "visible": _AGENT_REPLY,
        "check_reasoning": True,
    },
    {
        "id": "thought-dispatch-cjk-query",
        "text": f"thought\nNeed the regulations agent.\nDISPATCH:regs:{_CJK_QUERY}\n",
        "route": rs._ROUTE_DIRECT,
        "decision": ("dispatch", "regs", _CJK_QUERY, 1),
        "visible": _AGENT_REPLY,
        "check_reasoning": True,
    },
    {
        # 30 CJK then a DISPATCH query of 80 CJK. The density window starts
        # in the preamble and reaches into the still-open directive, so a
        # one-character stream used to answer while one chunk dispatched.
        "id": "thought-window-overlaps-dispatch",
        "text": "thought\nxx" + "政" * 30 + "\nDISPATCH:regs:" + "規" * 80 + "\n",
        "route": rs._ROUTE_DIRECT,
        "decision": ("dispatch", "regs", "規" * 80, 1),
        "visible": _AGENT_REPLY,
        "check_reasoning": True,
    },
    {
        # Same window overlap onto a query-less header. End-of-stream salvage
        # must win on every split; the user message is the substituted query.
        "id": "thought-window-overlaps-queryless",
        "text": "thought\nxx" + "政" * 30 + "\nDISPATCH:regs:\n" + "政" * 50,
        "route": rs._ROUTE_DIRECT,
        "decision": ("dispatch", "regs", "問題", 1),
        "visible": _AGENT_REPLY,
        "check_reasoning": True,
    },
    {
        "id": "forced-directive",
        "text": "**DISPATCH:weather:今天天氣**\n",
        "route": rs._ROUTE_FORCED,
        "decision": ("direct",),
        "visible": rs._FORCED_EMPTY_FALLBACK,
    },
    {
        "id": "forced-answer-then-directive",
        "text": "依規定可以直接回答這件事，不需要再查。\nDISPATCH:weather:今天天氣\n",
        "route": rs._ROUTE_FORCED,
        "decision": ("direct",),
        "visible_contains": "依規定可以直接回答這件事，不需要再查。",
        "visible_excludes": "DISPATCH",
        "early": 1,
    },
    {
        # One chunk used to leave one trailing newline and character deltas
        # two, because the directive's surrounding separators were excised
        # out of a later slice instead of the whole answer region.
        "id": "forced-thought-then-directive",
        "text": "thought\nxx" + "政" * 100 + "\nDISPATCH:weather:晴\n",
        "route": rs._ROUTE_FORCED,
        "decision": ("direct",),
        "visible": "政" * 100,
        "check_reasoning": True,
    },
    {
        "id": "prose-dispatch-label",
        "text": (
            "DISPATCH: is a syntax label, not an instruction.\n"
            "Here is the answer."
        ),
        "route": rs._ROUTE_DIRECT,
        "decision": ("direct",),
        "visible": (
            "DISPATCH: is a syntax label, not an instruction.\n"
            "Here is the answer."
        ),
        # Stream at the end of the rejected line, not at EOF.
        "early": len("DISPATCH: is a syntax label, not an instruction.\n"),
    },
]


@pytest.mark.parametrize("case", _CASES, ids=[case["id"] for case in _CASES])
def test_routing_decision_is_invariant_across_chunk_splits(stream_box, case) -> None:
    reference = None
    for label, deltas in _splits(case["text"]):
        decision, visible, reasoning, meta_reasoning, first = _drive(
            stream_box, deltas, route=case["route"]
        )
        observed = (decision, visible, reasoning, meta_reasoning)
        if reference is None:
            reference = observed
            assert decision == case["decision"], (case["id"], label, decision, visible)
            if "visible" in case:
                assert visible == case["visible"], (case["id"], label, visible)
            if "visible_contains" in case:
                assert case["visible_contains"] in visible, (case["id"], visible)
            if "visible_excludes" in case:
                assert case["visible_excludes"] not in visible, (case["id"], visible)
            if case.get("check_reasoning"):
                assert reasoning, (case["id"], "reasoning deltas empty")
                assert meta_reasoning, (case["id"], "meta.reasoning empty")
        else:
            assert observed == reference, (
                f"{case['id']} via {label} diverged\n"
                f"  deltas={deltas!r}\n"
                f"  got={observed!r}\n"
                f"  ref={reference!r}"
            )
        if label == "chars" and case.get("early") is not None:
            assert first == case["early"], (case["id"], first, case["early"])


def test_introducer_hold_accepts_wrappers_and_rejects_prose() -> None:
    """The streaming hold and the offline introducer agree on the head."""
    hold = {
        "",
        "   ",
        "\n\n",
        "**",
        "> ",
        "`",
        "D",
        "DIS",
        "DISPATCH",
        "A",
        "AS",
        "ASK",
        "ASK*",
        "\n\nASK*",
        "> ASK*",
        "`ASK*",
        "\n\n**DISPATC",
        "> AS",
    }
    opened = {
        "DISPATCH:",
        "ASK:",
        "ASK*:",
        "**DISPATCH:",
        "> ASK:",
        "> ASK*:",
        "`ASK:",
        "`ASK*:",
        "\n\nDISPATCH:",
        "\n\nASK*:",
        "  **ASK:",
        "  **ASK*:",
    }
    prose = {
        "Definitely",
        "ASAP",
        "DIS is",
        "這是回答",
        "****nope",
        "Hello\nDISPATCH:",
        "ASK* is not a directive",
        "回答\nASK*:要挑？|甲",
    }
    for text in hold:
        assert rs._directive_intro_status(text) == "hold", text
    for text in opened:
        assert rs._directive_intro_status(text) == "open", text
    for text in prose:
        assert rs._directive_intro_status(text) == "no", text
    assert rs._stream_head_kind("thought\nDISPATCH:weather:今天天氣\n") == "thought"
    assert rs._stream_head_kind("the cat sat on the mat") == "answer"
    label = "DISPATCH: is a syntax label, not an instruction.\nHere is the answer."
    assert rs._stream_head_kind(label) == "answer"
    assert rs._stream_head_kind("DISPATCH: is a syntax label, not an instruction.") == "directive"
    # The star has arrived and the colon has not: still a prefix, not an answer.
    assert rs._stream_head_kind("ASK*") == "hold"
    assert rs._stream_head_kind("ASK*:要挑哪幾個？|甲|乙") == "directive"


def test_forced_partial_answer_survives_stream_error(monkeypatch) -> None:
    """A forced answer already shown must not gain the outage sentence.

    ``_forced_suffix`` records the emission in ``forced_sent`` and leaves
    ``answer_emitted_up_to`` at 0. The error path used to treat that as
    silence and append the outage fallback after ``A partial forced answer``.
    """

    async def fake_stream(*_args, **_kwargs):
        yield {"type": "delta", "content": "A partial forced answer"}
        yield {
            "type": "error",
            "error": "LLM connection: ReadTimeout",
            "detail": "timed out",
        }

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
            session=MemorySession("forced-error"),
            route_signal=rs._ROUTE_FORCED,
        ):
            parts.append(line)
        return "".join(parts)

    body = asyncio.run(run())
    visible, _reasoning, _meta, _interrupt = _visible(body)
    assert visible == "A partial forced answer"
    assert "暫時無法回應" not in body
    assert '"finish_reason": "length"' in body


def test_parse_ask_leading_blank_lines() -> None:
    assert rs._parse_ask("\n\nASK:要哪一種？|晴天|雨天") == {
        "question": "要哪一種？",
        "options": [
            {"label": "晴天", "value": "晴天", "description": ""},
            {"label": "雨天", "value": "雨天", "description": ""},
        ],
        "multi": False,
    }


def test_parse_ask_star_is_multi_and_a_later_line_is_prose() -> None:
    assert rs._parse_ask(
        "ASK*:要挑哪幾個來拆成三種版本？|一 環境感測器|六 電網天線|全都要"
    ) == {
        "question": "要挑哪幾個來拆成三種版本？",
        "options": [
            {"label": "一 環境感測器", "value": "一 環境感測器", "description": ""},
            {"label": "六 電網天線", "value": "六 電網天線", "description": ""},
            {"label": "全都要", "value": "全都要", "description": ""},
        ],
        "multi": True,
    }
    wrapped = rs._parse_ask("> ASK*:要挑哪幾個？|甲|乙")
    assert wrapped is not None and wrapped["multi"] is True
    assert wrapped["question"] == "要挑哪幾個？"
    assert rs._parse_ask("**ASK*:要挑哪幾個？|甲**")["question"] == "要挑哪幾個？"
    prose = "這是回答。\nASK*:要挑哪幾個？|甲|乙\n"
    assert rs._parse_ask(prose) is None
    assert rs._strip_ask_syntax(prose) == prose
    assert rs._strip_ask_syntax("ASK*:要挑哪幾個？|甲|乙\n後面是說明") == "後面是說明"
