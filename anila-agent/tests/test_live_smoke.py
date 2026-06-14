"""live 端到端 smoke：對真實 OpenAI-compatible 端點跑一輪。

預設 skip；設定 ANILA_BASE_URL（指向可用端點）後以 ``pytest -m live`` 執行。
證明 air-gap provider 路徑（Chat Completions + 本地模型 + DummyRetriever）端到端可跑。
"""

from __future__ import annotations

import os

import pytest

from anila_agent.config import load_config
from anila_agent.runtime.agent_factory import build_agent
from anila_agent.runtime.run import run_once

pytestmark = pytest.mark.live


@pytest.mark.skipif(not os.getenv("ANILA_BASE_URL"), reason="未設 ANILA_BASE_URL")
async def test_live_single_turn():
    assembled = build_agent(load_config())
    result = await run_once(assembled, "ANILA 是什麼平台？用一句話說明。")
    assert isinstance(result.final_output, str) and result.final_output.strip()
