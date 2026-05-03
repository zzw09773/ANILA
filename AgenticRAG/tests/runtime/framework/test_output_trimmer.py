"""Tests for C1 — ToolOutputTrimmerMiddleware."""

from __future__ import annotations

import json

import pytest

from agentic_rag.runtime.framework import (
    Action,
    ActionContext,
    ActionKind,
    ActionResult,
    CostEstimate,
)
from agentic_rag.runtime.framework.middleware import (
    ToolOutputTrimmerMiddleware,
    compose_chain,
)


def _ctx() -> ActionContext:
    return ActionContext(run_id="r", agent_name="a", params={}, history=())


def _action(handler, *, name: str = "t") -> Action:
    return Action(
        name=name,
        description="",
        kind=ActionKind.SYNC_TOOL,
        handler=handler,
        cost_estimate=CostEstimate(),
    )


# ── Constructor validation ────────────────────────────────────────────


def test_constructor_rejects_invalid_max_chars() -> None:
    with pytest.raises(ValueError):
        ToolOutputTrimmerMiddleware(max_chars=0)


def test_constructor_rejects_preview_larger_than_max() -> None:
    with pytest.raises(ValueError):
        ToolOutputTrimmerMiddleware(max_chars=100, preview_chars=200)


# ── Pass-through paths ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_short_string_output_passes_through() -> None:
    async def h(ctx):
        return ActionResult(output="short")

    chain = compose_chain(
        _action(h), [ToolOutputTrimmerMiddleware(max_chars=1000, preview_chars=200)]
    )
    result = await chain(_ctx())
    assert result.output == "short"
    assert "_output_trimmed" not in result.metadata


@pytest.mark.asyncio
async def test_error_output_never_trimmed() -> None:
    big_err = "x" * 5_000

    async def h(ctx):
        return ActionResult(error=big_err)

    chain = compose_chain(
        _action(h), [ToolOutputTrimmerMiddleware(max_chars=100, preview_chars=50)]
    )
    result = await chain(_ctx())
    assert result.is_error
    assert result.error == big_err  # not touched


@pytest.mark.asyncio
async def test_none_output_passes_through() -> None:
    async def h(ctx):
        return ActionResult(output=None)

    chain = compose_chain(
        _action(h), [ToolOutputTrimmerMiddleware()]
    )
    result = await chain(_ctx())
    assert result.output is None


# ── Allowlist filtering ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_allowlist_excludes_unlisted_tools() -> None:
    big = "x" * 5_000

    async def h(ctx):
        return ActionResult(output=big)

    # Only "vector_search" is listed; our action is "other"
    chain = compose_chain(
        _action(h, name="other"),
        [
            ToolOutputTrimmerMiddleware(
                max_chars=100, preview_chars=50, trim_tools=["vector_search"]
            )
        ],
    )
    result = await chain(_ctx())
    assert result.output == big  # untouched


@pytest.mark.asyncio
async def test_allowlist_includes_listed_tools() -> None:
    big = "x" * 5_000

    async def h(ctx):
        return ActionResult(output=big)

    chain = compose_chain(
        _action(h, name="vector_search"),
        [
            ToolOutputTrimmerMiddleware(
                max_chars=100, preview_chars=50, trim_tools=["vector_search"]
            )
        ],
    )
    result = await chain(_ctx())
    assert result.output != big
    assert result.metadata["_output_trimmed"] is True


# ── Trimming behaviour ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_long_string_output_gets_trimmed_to_preview() -> None:
    big = "abcdefghij" * 1_000  # 10000 chars

    async def h(ctx):
        return ActionResult(output=big)

    chain = compose_chain(
        _action(h), [ToolOutputTrimmerMiddleware(max_chars=500, preview_chars=200)]
    )
    result = await chain(_ctx())
    assert isinstance(result.output, str)
    # Preview chars + marker
    assert "[output trimmed by middleware" in result.output
    assert len(result.output) < len(big)
    # Original size preserved in metadata for tracing
    assert result.metadata["_original_size_chars"] == len(big)


@pytest.mark.asyncio
async def test_long_dict_output_becomes_structured_preview() -> None:
    big_dict = {"results": [{"chunk": "x" * 200} for _ in range(50)]}
    rendered_size = len(json.dumps(big_dict, ensure_ascii=False))

    async def h(ctx):
        return ActionResult(output=big_dict)

    chain = compose_chain(
        _action(h), [ToolOutputTrimmerMiddleware(max_chars=500, preview_chars=200)]
    )
    result = await chain(_ctx())
    assert isinstance(result.output, dict)
    assert result.output["_trimmed"] is True
    assert "preview" in result.output
    assert result.output["original_size_chars"] == rendered_size
    assert "trim_marker" in result.output


@pytest.mark.asyncio
async def test_dict_just_under_threshold_passes_through() -> None:
    """Boundary: exactly at threshold = no trim; just over = trim."""
    almost = {"x": "y" * 100}  # ~110 chars JSON

    async def h(ctx):
        return ActionResult(output=almost)

    chain = compose_chain(
        _action(h), [ToolOutputTrimmerMiddleware(max_chars=200, preview_chars=50)]
    )
    result = await chain(_ctx())
    assert result.output == almost
    assert "_output_trimmed" not in result.metadata


@pytest.mark.asyncio
async def test_metadata_is_merged_not_replaced() -> None:
    """Trimming preserves whatever metadata earlier middleware attached."""

    async def h(ctx):
        return ActionResult(
            output="x" * 5_000,
            metadata={"earlier_key": "earlier_value"},
        )

    chain = compose_chain(
        _action(h), [ToolOutputTrimmerMiddleware(max_chars=100, preview_chars=50)]
    )
    result = await chain(_ctx())
    assert result.metadata["earlier_key"] == "earlier_value"
    assert result.metadata["_output_trimmed"] is True
