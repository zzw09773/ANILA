"""Quick end-to-end smoke test for AgenticRAG.

Usage:
    $env:OPENAI_API_KEY = "sk-..."
    python e2e_smoke.py

Sends a single-turn query through QueryEngine → OpenAICompatProvider → OpenAI API
and prints each streamed delta plus the final usage summary.
"""

from __future__ import annotations

import asyncio
import os
import sys

from agentic_rag.engine.query_engine import QueryConfig, QueryEngine
from agentic_rag.models.message import StreamDelta, UserMessage
from agentic_rag.models.tool import ToolDefinition, ToolSafety
from agentic_rag.providers.openai_compat import OpenAICompatProvider
from agentic_rag.router.tool_router import ToolRegistry


# ── 1. Provider ──────────────────────────────────────────────────────────────

api_key = os.environ.get("OPENAI_API_KEY", "")
if not api_key:
    print("ERROR: OPENAI_API_KEY environment variable is not set.", file=sys.stderr)
    sys.exit(1)

provider = OpenAICompatProvider(
    base_url="https://api.openai.com/v1",
    api_key=api_key,
    timeout=60.0,
)

# ── 2. Tool registry (no tools for this smoke test) ──────────────────────────

registry = ToolRegistry()

# Optional: register a trivial echo tool to verify tool-call path
echo_def = ToolDefinition(
    name="echo",
    description="Echoes the input text back to the caller.",
    input_schema={
        "type": "object",
        "properties": {"text": {"type": "string", "description": "Text to echo."}},
        "required": ["text"],
    },
    safety=ToolSafety.READ_ONLY,
    handler=lambda tool_call: {"echoed": tool_call.input.get("text", "")},
)
registry.register(echo_def)

# ── 3. Engine config ─────────────────────────────────────────────────────────

config = QueryConfig(
    max_turns=3,
    model="gpt-4o-mini",  # cheap model for testing
    system_prompt=(
        "You are a helpful assistant. "
        "If you want to demonstrate tool use, call the echo tool once, "
        "then answer the user."
    ),
)

engine = QueryEngine(provider, registry, config)


# ── 4. Delta callback ─────────────────────────────────────────────────────────

async def on_delta(delta: StreamDelta) -> None:  # type: ignore[override]
    """Print each streaming delta as it arrives."""
    if delta.type == "text" and delta.text:
        print(delta.text, end="", flush=True)
    elif delta.type == "tool_call" and delta.tool_call:
        print(f"\n[tool_call] → {delta.tool_call.name}  partial={delta.tool_call.input_partial!r}")
    elif delta.type == "tool_result" and delta.tool_result:
        print(f"[tool_result] ← {delta.tool_result.content}")
    elif delta.type == "stop":
        usage = delta.usage
        if usage:
            print(
                f"\n\n── Usage ──────────────────────────────────────────────"
                f"\n  input tokens   : {usage.input_tokens}"
                f"\n  output tokens  : {usage.output_tokens}"
                f"\n  cache_read     : {usage.cache_read_tokens}"
                f"\n───────────────────────────────────────────────────────"
            )


# ── 5. Run ───────────────────────────────────────────────────────────────────

async def main() -> None:
    print("=== AgenticRAG — end-to-end smoke test ===\n")
    print("User: Hello! Please call the echo tool with 'smoke-test-ok', then greet me.\n")
    print("Assistant: ", end="", flush=True)

    messages = [
        UserMessage(content="Hello! Please call the echo tool with 'smoke-test-ok', then greet me.")
    ]

    result = await engine.run(messages, on_stream_delta=on_delta)

    print("\n\n=== TurnResult ===")
    print(f"  stop_reason : {result.stop_reason}")
    print(f"  turns_used  : {result.turn_count}")
    print(f"  finish_reason: {result.finish_reason}")
    print("=== PASS ===")


if __name__ == "__main__":
    asyncio.run(main())
