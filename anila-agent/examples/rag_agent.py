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
    """Tiny placeholder corpus. Replace with documents from your domain."""
    return DummyRetriever(
        [
            Document(
                id="doc-1",
                text=(
                    "Pluto was reclassified from a planet to a dwarf planet by the "
                    "International Astronomical Union in 2006."
                ),
                metadata={"source": "example/astronomy"},
            ),
            Document(
                id="doc-2",
                text=(
                    "The Mariana Trench is the deepest known part of the world's "
                    "oceans, reaching about 11,000 metres at the Challenger Deep."
                ),
                metadata={"source": "example/geography"},
            ),
            Document(
                id="doc-3",
                text=(
                    "The speed of light in vacuum is exactly 299,792,458 metres "
                    "per second by definition."
                ),
                metadata={"source": "example/physics"},
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
        "Use the search tool to answer: how deep is the Mariana Trench?"
    )
    print(summary.final_output)


if __name__ == "__main__":
    asyncio.run(main())
