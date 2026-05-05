"""Wire a custom retriever into the RAG tools and run a query.

This demonstrates the clone-and-fill workflow:
  1. Build a `Retriever` over your data.
  2. Install it via `set_retriever()` before constructing the agent.
  3. The built-in `search_documents` and `read_document` tools pick it up automatically.
"""

from __future__ import annotations

import asyncio

from anila_agent.core.agent import build_agent
from anila_agent.core.runner import AnilaRunner
from anila_agent.models.schemas import Document
from anila_agent.retrieval.dummy import DummyRetriever
from anila_agent.tools.rag_tools import set_retriever
from anila_agent.utils.config import load_config
from anila_agent.utils.logging import configure


def _seed_corpus() -> DummyRetriever:
    return DummyRetriever(
        [
            Document(
                id="anila-overview",
                text=(
                    "Anila is an Agentic RAG starter built on the openai-agents SDK. "
                    "It ports the harness patterns from Claude Code (long-term memdir, "
                    "PreToolUse/PostToolUse/Stop hooks, slash commands) into Python."
                ),
                metadata={"source": "internal/overview"},
            ),
            Document(
                id="anila-memory",
                text=(
                    "Long-term memory is file-backed: <memory_dir>/MEMORY.md plus topic "
                    "files with YAML frontmatter (name, description, type). Recall scans "
                    "the directory and asks a small LLM call to pick relevant files."
                ),
                metadata={"source": "internal/memory"},
            ),
            Document(
                id="anila-hooks",
                text=(
                    "Hooks fire around tool calls. Each hook returns a HookOutput; the "
                    "runner aggregates results, allowing block / approve / context-injection."
                ),
                metadata={"source": "internal/hooks"},
            ),
        ]
    )


async def main() -> None:
    configure()
    set_retriever(_seed_corpus())
    config = load_config()
    assembled = build_agent(config, session_id="example-rag")
    runner = AnilaRunner(assembled, session_id="example-rag")

    summary = await runner.send(
        "Use the search tool to explain how Anila handles long-term memory."
    )
    print(summary.final_output)


if __name__ == "__main__":
    asyncio.run(main())
