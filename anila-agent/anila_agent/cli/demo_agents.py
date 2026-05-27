"""P2-14 demo agent stubs — 給 :mod:`anila_agent.cli.run_demo_loop` 用的最小 mock agent。

本模組提供兩個輕量 sample agent,讓 developer 試 streaming REPL 時不必接真 LLM 就
能跑起來:

* :func:`echo_agent_stream` — 把 user input 原樣 echo 回去(逐字 raw_token + final_output)。
* :func:`weather_agent_stream` — 模擬一次 tool_call + tool_completed + final message,
  示範三層 StreamEvent 在 tool 場景下的順序。

兩支 stub 都是 ``Callable[[str], AsyncIterator[StreamChunk]]`` 的形狀,可直接餵給
:func:`anila_agent.cli.run_demo_loop.run_demo_loop_async` 的 ``agent`` 參數。

設計重點
========

* **不接 LLM**:純 mock chunk,跑起來零 dependency(連 vLLM / Triton 都不用)。
* **chunk schema 對齊 P1-7**:用 :data:`anila_agent.core.streaming.StreamChunk` 約定的
  ``{"kind": ..., ...}`` dict,讓 :class:`AnilaStreamRunner` 直接消費。
* **完整三層覆蓋**:weather agent 涵蓋 ``agent_started`` / ``raw_token`` /
  ``tool_started`` / ``tool_completed`` / ``message`` / ``agent_ended`` 六種 chunk。
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Callable
from typing import Any

from anila_agent.core.streaming import StreamChunk

# 一個 demo agent 是「接 user input → 產生 StreamChunk async iterator」的 callable。
# 真實 SDK 整合(P1-11)時,這個 type 會由 adapter 把 ``Runner.run_streamed`` 的
# SDK stream events 轉成同形狀 chunk。
DemoAgent = Callable[[str], AsyncIterator[StreamChunk]]


async def echo_agent_stream(user_input: str) -> AsyncIterator[StreamChunk]:
    """Echo agent — 把 user input 原樣 echo 回去。

    模擬一輪極簡 LLM run:
    ``agent_started`` → 逐字 ``raw_token`` 把 input echo → ``agent_ended``。

    Args:
        user_input: REPL 收到的這一輪 user 輸入。

    Yields:
        StreamChunk: P1-7 chunk schema 的 dict。
    """
    yield {"kind": "agent_started", "agent": "echo"}
    # 逐字 echo,模擬 LLM token-by-token streaming;字距用 0 秒 sleep 讓單元測試快。
    for ch in user_input:
        yield {
            "kind": "raw_token",
            "delta": ch,
            "model": "demo-echo",
        }
    final = f"echo: {user_input}"
    yield {"kind": "agent_ended", "agent": "echo", "output": final}


async def weather_agent_stream(user_input: str) -> AsyncIterator[StreamChunk]:
    """Fake weather agent — 示範 tool_call + tool_completed 流程。

    模擬流程:
    1. agent_started("weather")。
    2. 一段 thinking raw_token(``"Let me check..."``)。
    3. tool_started(``get_weather``,city 從 user_input 推斷)。
    4. tool_completed,output 為 fake forecast。
    5. 完整 message 印出 forecast。
    6. agent_ended,output 為人類可讀字串。

    Args:
        user_input: 當作要查的城市名(預設用整段 input)。

    Yields:
        StreamChunk: P1-7 chunk schema 的 dict。
    """
    city = user_input.strip() or "Taipei"
    call_id = f"call_{uuid.uuid4().hex[:8]}"

    yield {"kind": "agent_started", "agent": "weather"}
    for token in ("Let", " me", " check", " ", city, "..."):
        yield {"kind": "raw_token", "delta": token, "model": "demo-weather"}

    yield {
        "kind": "tool_started",
        "tool": "get_weather",
        "args": {"city": city},
        "call_id": call_id,
    }
    # 模擬 tool 跑 — 給單測一個 deterministic, 短的 sleep。
    await asyncio.sleep(0)
    fake_forecast: dict[str, Any] = {
        "city": city,
        "temperature_c": 27,
        "condition": "sunny",
    }
    yield {
        "kind": "tool_completed",
        "tool": "get_weather",
        "call_id": call_id,
        "output": fake_forecast,
    }
    summary = (
        f"{city}: {fake_forecast['temperature_c']}°C, "
        f"{fake_forecast['condition']}."
    )
    yield {"kind": "message", "text": summary}
    yield {"kind": "agent_ended", "agent": "weather", "output": summary}


def list_demo_agents() -> dict[str, DemoAgent]:
    """回傳「name → demo agent stub」對應表,供 CLI default agent 選擇用。"""
    return {
        "echo": echo_agent_stream,
        "weather": weather_agent_stream,
    }


__all__ = [
    "DemoAgent",
    "echo_agent_stream",
    "list_demo_agents",
    "weather_agent_stream",
]
