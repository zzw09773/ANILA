"""CLI 進入點：載入設定 → 建構 agent → 跑 REPL。"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from dotenv import load_dotenv

from anila_agent.config import load_config
from anila_agent.runtime.agent_factory import build_agent


def main() -> None:
    load_dotenv()
    cfg = load_config()
    logging.basicConfig(
        level=getattr(logging, cfg.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    assembled = build_agent(cfg)

    # 延遲匯入：讓 import anila_agent.main 不必拉 prompt_toolkit/rich（測試友善）。
    from anila_agent.cli.app import repl
    from anila_agent.memory.session import build_session

    chat_session = build_session(cfg, session_id="cli")

    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(repl(assembled, chat_session, cfg))


if __name__ == "__main__":
    main()
