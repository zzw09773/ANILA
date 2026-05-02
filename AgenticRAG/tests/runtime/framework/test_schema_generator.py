"""Tests for @tool decorator (A6)."""

from __future__ import annotations

from typing import Annotated, Optional

import pytest

from agentic_rag.runtime.framework import (
    Action,
    ActionContext,
    ActionKind,
    ActionResult,
    SideEffectClass,
    tool,
)
from agentic_rag.runtime.framework.exceptions import UserError
from agentic_rag.runtime.framework.schema_generator import (
    _json_type_for,
    _parse_docstring,
)


def _ctx(params=None):
    return ActionContext(
        run_id="r", agent_name="a", params=params or {}, history=()
    )


# ── Type → JSON schema mapping ────────────────────────────────────────


def test_json_type_for_primitives() -> None:
    assert _json_type_for(str) == {"type": "string"}
    assert _json_type_for(int) == {"type": "integer"}
    assert _json_type_for(float) == {"type": "number"}
    assert _json_type_for(bool) == {"type": "boolean"}


def test_json_type_for_optional() -> None:
    schema = _json_type_for(Optional[str])
    assert schema["type"] == ["string", "null"]


def test_json_type_for_pep604_union() -> None:
    schema = _json_type_for(int | None)
    assert schema["type"] == ["integer", "null"]


def test_json_type_for_list_of_strings() -> None:
    schema = _json_type_for(list[str])
    assert schema == {"type": "array", "items": {"type": "string"}}


def test_json_type_for_annotated_carries_description() -> None:
    schema = _json_type_for(Annotated[int, "Top-K results"])
    assert schema == {"type": "integer", "description": "Top-K results"}


def test_json_type_for_unknown_type_returns_empty() -> None:
    class CustomType:
        pass

    assert _json_type_for(CustomType) == {}


# ── Docstring parsing ─────────────────────────────────────────────────


def test_parse_docstring_extracts_summary_and_args() -> None:
    docstring = """Vector search over user documents.

    Args:
        query: Natural language query.
        top_k: Max number of results.
    """
    summary, arg_map = _parse_docstring(docstring)
    assert summary == "Vector search over user documents."
    assert arg_map == {
        "query": "Natural language query.",
        "top_k": "Max number of results.",
    }


def test_parse_docstring_handles_empty() -> None:
    summary, arg_map = _parse_docstring(None)
    assert summary == ""
    assert arg_map == {}


def test_parse_docstring_no_args_block() -> None:
    summary, arg_map = _parse_docstring("Just a summary.\n\nMore details below.")
    assert summary == "Just a summary."
    assert arg_map == {}


def test_parse_docstring_multi_line_arg_description() -> None:
    docstring = """Tool description.

    Args:
        query: This is a long
            description that spans lines.
        top_k: Short.
    """
    _, arg_map = _parse_docstring(docstring)
    assert "spans lines" in arg_map["query"]
    assert arg_map["top_k"] == "Short."


# ── @tool decorator ───────────────────────────────────────────────────


def test_tool_decorator_bare_form_produces_action() -> None:
    @tool
    async def search(ctx: ActionContext, query: str, top_k: int = 5) -> dict:
        """Vector search."""
        return {"q": query, "k": top_k}

    assert isinstance(search, Action)
    assert search.name == "search"
    assert search.description == "Vector search."
    assert search.kind is ActionKind.SYNC_TOOL
    assert search.input_schema["properties"]["query"] == {"type": "string"}
    assert search.input_schema["properties"]["top_k"]["type"] == "integer"
    assert search.input_schema["properties"]["top_k"]["default"] == 5
    assert search.input_schema["required"] == ["query"]
    assert search.input_schema["additionalProperties"] is False


def test_tool_decorator_parenthesised_form_with_overrides() -> None:
    @tool(name="custom_name", side_effect_class=SideEffectClass.NETWORKED)
    async def search(ctx, query: str) -> dict:
        return {}

    assert search.name == "custom_name"
    assert search.side_effect_class is SideEffectClass.NETWORKED


def test_tool_uses_docstring_args_for_descriptions() -> None:
    @tool
    async def search(ctx, query: str, top_k: int = 5) -> dict:
        """Search.

        Args:
            query: The search string the user typed.
            top_k: How many to return.
        """
        return {}

    assert (
        search.input_schema["properties"]["query"]["description"]
        == "The search string the user typed."
    )
    assert search.input_schema["properties"]["top_k"]["description"] == "How many to return."


def test_tool_annotated_description_used_when_no_docstring_arg() -> None:
    @tool
    async def search(
        ctx, query: Annotated[str, "Annotated description here"]
    ) -> dict:
        """No args block in docstring."""
        return {}

    assert (
        search.input_schema["properties"]["query"]["description"]
        == "Annotated description here"
    )


def test_tool_docstring_arg_overrides_annotated() -> None:
    @tool
    async def search(
        ctx, query: Annotated[str, "Annotated wins?"]
    ) -> dict:
        """Search.

        Args:
            query: Docstring wins.
        """
        return {}

    # Docstring is generally maintained more carefully than annotations.
    assert (
        search.input_schema["properties"]["query"]["description"]
        == "Docstring wins."
    )


def test_tool_rejects_sync_function() -> None:
    with pytest.raises(UserError, match="must be async"):
        @tool
        def search(ctx, query: str):  # noqa: ARG001
            return {}


def test_tool_rejects_function_without_context_param() -> None:
    with pytest.raises(UserError, match="ActionContext"):
        @tool
        async def search(query: str) -> dict:  # noqa: ARG001
            return {}


@pytest.mark.asyncio
async def test_tool_handler_wraps_dict_return_into_action_result() -> None:
    @tool
    async def search(ctx, query: str) -> dict:
        """Doc."""
        return {"hits": [query]}

    result = await search.handler(_ctx({"query": "rag"}))
    assert isinstance(result, ActionResult)
    assert result.output == {"hits": ["rag"]}


@pytest.mark.asyncio
async def test_tool_handler_passes_through_action_result() -> None:
    @tool
    async def maybe_fail(ctx, query: str) -> ActionResult:
        """Doc."""
        if not query:
            return ActionResult(error="empty query")
        return ActionResult(output={"q": query})

    err = await maybe_fail.handler(_ctx({"query": ""}))
    assert err.is_error
    ok = await maybe_fail.handler(_ctx({"query": "x"}))
    assert ok.output == {"q": "x"}


@pytest.mark.asyncio
async def test_tool_handler_catches_exceptions_as_action_errors() -> None:
    @tool
    async def boom(ctx, query: str) -> dict:
        """Doc."""
        raise RuntimeError("disk on fire")

    result = await boom.handler(_ctx({"query": "x"}))
    assert result.is_error
    assert "disk on fire" in result.error


@pytest.mark.asyncio
async def test_tool_handler_applies_defaults_for_missing_kwargs() -> None:
    @tool
    async def search(ctx, query: str, top_k: int = 7) -> dict:
        """Doc."""
        return {"k": top_k}

    result = await search.handler(_ctx({"query": "rag"}))
    assert result.output == {"k": 7}


def test_tool_additional_properties_true_allows_extra_keys() -> None:
    @tool(additional_properties=True)
    async def search(ctx, query: str) -> dict:
        """Doc."""
        return {}

    assert search.input_schema["additionalProperties"] is True


def test_tool_context_param_can_be_named_context() -> None:
    @tool
    async def search(context, query: str) -> dict:
        """Doc."""
        return {"got": query}

    assert search.input_schema["required"] == ["query"]


def test_tool_recognises_context_by_annotation_only() -> None:
    @tool
    async def search(c: ActionContext, query: str) -> dict:
        """Doc."""
        return {"got": query}

    assert search.input_schema["required"] == ["query"]
