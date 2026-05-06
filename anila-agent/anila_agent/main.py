"""CLI entry point. `python -m anila_agent.main` or `anila` after install."""

from __future__ import annotations

import argparse
import sys

from anila_agent.cli.app import main_sync
from anila_agent.core.agent import build_agent
from anila_agent.core.runner import AnilaRunner
from anila_agent.utils.config import load_config
from anila_agent.utils.logging import configure


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="anila", description="Anila Agentic RAG CLI")
    parser.add_argument(
        "--session",
        default="default",
        help="Session ID. Reusing the same ID resumes the conversation.",
    )
    parser.add_argument(
        "--config-dir",
        default="configs",
        help="Path to config directory. Defaults to <project>/configs.",
    )
    parser.add_argument(
        "--prompt",
        help="One-shot mode: send this prompt, print the result, exit.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    configure()
    args = _parse_args(argv or sys.argv[1:])
    config = load_config(args.config_dir)
    assembled = build_agent(config, session_id=args.session)
    runner = AnilaRunner(assembled, session_id=args.session)

    if args.prompt:
        import asyncio

        summary = asyncio.run(runner.send(args.prompt))
        if summary.aborted:
            print(f"aborted: {summary.abort_reason}", file=sys.stderr)
            return 1
        print(summary.final_output if summary.final_output is not None else "")
        return 0

    main_sync(runner, config)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
