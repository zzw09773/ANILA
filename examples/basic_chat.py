"""Smoke test: load config, send one prompt, print the answer.

Run with:
    python examples/basic_chat.py
"""

from __future__ import annotations

import asyncio

from anila_agent.core.agent import build_agent
from anila_agent.core.runner import AnilaRunner
from anila_agent.utils.config import load_config
from anila_agent.utils.logging import configure


async def main() -> None:
    configure()
    config = load_config()
    assembled = build_agent(config, session_id="example-basic")
    runner = AnilaRunner(assembled, session_id="example-basic")

    summary = await runner.send("In one sentence: what is Anila?")
    print(summary.final_output)


if __name__ == "__main__":
    asyncio.run(main())
