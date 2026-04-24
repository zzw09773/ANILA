"""RAG Pre-processor for the AgenticRAG query engine.

Retrieves semantically relevant document chunks before each LLM call
and injects them into the conversation history as a structured context block.

Injection format:
    [RAG Context - Retrieved Documents]
    --- Source 1 (confidence: 0.92, Title > Chapter 1 (p.3)) ---
    {chunk_content}
    --- Source 2 (confidence: 0.87, Manual > Setup) ---
    {chunk_content}
    [End RAG Context]

This is inserted as a system-role-style prefix in the first UserMessage
so the model receives the context without extra round trips.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

from ..models.message import Message, UserMessage
from ..models.storage import Citation, RetrievalTrace

logger = logging.getLogger(__name__)


class RagPreprocessor:
    """Inject retrieved document context into the conversation history.

    Args:
        embedding_provider: Provides embed(texts, input_type) → vectors.
        retrieval_provider: Provides search(embedding, user_id, project_id, ...).
        user_id:            Used to scope retrieval.
        project_id:         Used to scope retrieval.
        top_k:              Maximum number of chunks to retrieve.
        min_score:          Minimum cosine similarity threshold.
        trace_store:        Optional store for audit logging of retrievals.
    """

    def __init__(
        self,
        embedding_provider: Any,
        retrieval_provider: Any,
        user_id: str,
        project_id: str,
        top_k: int = 5,
        min_score: float = 0.7,
        trace_store: Optional[Any] = None,
    ) -> None:
        self._embedder = embedding_provider
        self._retriever = retrieval_provider
        self._user_id = user_id
        self._project_id = project_id
        self._top_k = top_k
        self._min_score = min_score
        self._trace_store = trace_store

    async def preprocess(
        self,
        history: list[Message],
        session_id: str = "",
    ) -> tuple[list[Message], Optional[str]]:
        """Retrieve relevant chunks and inject them into *history*.

        Args:
            history:    Current conversation history (not mutated).
            session_id: For audit trace logging.

        Returns:
            (augmented_history, rag_context_text | None)
        """
        query = _extract_latest_query(history)
        if not query:
            return history, None

        start_ms = time.monotonic()

        # 1. Embed the query
        try:
            embeddings = await self._embedder.embed([query], input_type="query")
            query_embedding: list[float] = embeddings[0]
        except Exception as exc:
            logger.warning("RAG embedding failed, skipping context injection: %s", exc)
            return history, None

        # 2. Retrieve top-k citations
        try:
            citations: list[Citation] = await self._retriever.search(
                query_embedding=query_embedding,
                user_id=self._user_id,
                project_id=self._project_id,
                top_k=self._top_k,
                min_score=self._min_score,
            )
        except Exception as exc:
            logger.warning("RAG retrieval failed, skipping context injection: %s", exc)
            return history, None

        latency_ms = (time.monotonic() - start_ms) * 1000

        if not citations:
            return history, None

        # 3. Format context block
        context_text = _format_context(citations)

        # 4. Inject into history (prepend to last UserMessage)
        augmented_history = _inject_context(history, context_text)

        # 5. Optionally log retrieval trace
        if self._trace_store and session_id:
            await _log_trace(
                self._trace_store,
                session_id=session_id,
                user_id=self._user_id,
                project_id=self._project_id,
                query=query,
                citations=citations,
                latency_ms=latency_ms,
            )

        logger.debug(
            "RAG: injected %d citations (%.1f ms) for query=%.60r",
            len(citations), latency_ms, query,
        )
        return augmented_history, context_text


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _extract_latest_query(history: list[Message]) -> str:
    """Return the text of the most recent UserMessage."""
    for msg in reversed(history):
        if isinstance(msg, UserMessage):
            content = msg.content
            if isinstance(content, str):
                return content.strip()
            if isinstance(content, list):
                parts = [
                    block.get("text", "")
                    for block in content
                    if isinstance(block, dict) and block.get("type") == "text"
                ]
                return " ".join(parts).strip()
    return ""


def _format_context(citations: list[Citation]) -> str:
    """Format retrieved citations into a structured context block."""
    lines = ["[RAG Context - Retrieved Documents]"]
    for i, c in enumerate(citations, 1):
        trail = c.cite() or c.document_id
        lines.append(
            f"--- Source {i} (confidence: {c.confidence:.2f}, {trail}) ---"
        )
        lines.append(c.content)
        if c.parent_content and c.parent_content.strip() != c.content.strip():
            lines.append(f"(context) {c.parent_content}")
    lines.append("[End RAG Context]")
    return "\n".join(lines)


def _inject_context(history: list[Message], context_text: str) -> list[Message]:
    """Prepend the RAG context block to the last UserMessage in *history*."""
    # Find index of last UserMessage
    last_user_idx: Optional[int] = None
    for i in range(len(history) - 1, -1, -1):
        if isinstance(history[i], UserMessage):
            last_user_idx = i
            break

    if last_user_idx is None:
        # No user message found — prepend a synthetic one
        rag_msg = UserMessage(content=[{"type": "text", "text": context_text}])
        return [rag_msg] + list(history)

    original = history[last_user_idx]
    original_content = original.content

    # Build augmented content
    if isinstance(original_content, str):
        new_content = f"{context_text}\n\n{original_content}"
        augmented = UserMessage(content=new_content)
    else:
        # Block format: prepend a text block with the context
        context_block = {"type": "text", "text": context_text + "\n\n"}
        new_blocks = [context_block] + list(original_content)
        augmented = UserMessage(content=new_blocks)

    return list(history[:last_user_idx]) + [augmented] + list(history[last_user_idx + 1:])


async def _log_trace(
    trace_store: Any,
    session_id: str,
    user_id: str,
    project_id: str,
    query: str,
    citations: list[Citation],
    latency_ms: float,
) -> None:
    try:
        trace = RetrievalTrace(
            session_id=session_id,
            user_id=user_id,
            project_id=project_id,
            query=query,
            retrieved_chunk_ids=[c.chunk_id for c in citations],
            scores=[float(c.confidence) for c in citations],
            latency_ms=latency_ms,
        )
        await trace_store.log(trace)
    except Exception as exc:
        logger.warning("Failed to log retrieval trace: %s", exc)
