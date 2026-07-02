"""Tests for the ``todo_write`` tool."""

from __future__ import annotations

import pytest

from anila_core.context.agent_context import (
    AgentContext,
    set_current_context,
)
from anila_core.models.agent import Todo
from anila_core.tools.todo_write import (
    INPUT_SCHEMA,
    TOOL_NAME,
    TodoValidationError,
    _todo_write_impl,
    _validate,
    todo_write_tool,
)


# ---------------------------------------------------------------------------
# Tool definition
# ---------------------------------------------------------------------------


def test_tool_definition_basics() -> None:
    tool = todo_write_tool()
    assert tool.name == TOOL_NAME == "todo_write"
    assert tool.input_schema["required"] == ["todos"]
    item_schema = INPUT_SCHEMA["properties"]["todos"]["items"]
    assert "content" in item_schema["required"]
    assert "active_form" in item_schema["required"]


# ---------------------------------------------------------------------------
# _validate
# ---------------------------------------------------------------------------


def test_validate_round_trips_well_formed_todos() -> None:
    todos = _validate(
        [
            {"content": "a", "active_form": "doing a", "status": "pending"},
            {
                "content": "b",
                "active_form": "doing b",
                "status": "in_progress",
            },
            {"content": "c", "active_form": "doing c", "status": "completed"},
        ]
    )
    assert [t.content for t in todos] == ["a", "b", "c"]
    assert [t.status for t in todos] == [
        "pending",
        "in_progress",
        "completed",
    ]
    assert all(isinstance(t, Todo) for t in todos)


def test_validate_defaults_status_to_pending() -> None:
    [todo] = _validate([{"content": "x", "active_form": "doing x"}])
    assert todo.status == "pending"


def test_validate_rejects_more_than_one_in_progress() -> None:
    with pytest.raises(TodoValidationError, match="At most one"):
        _validate(
            [
                {"content": "a", "active_form": "..", "status": "in_progress"},
                {"content": "b", "active_form": "..", "status": "in_progress"},
            ]
        )


def test_validate_rejects_blank_content() -> None:
    with pytest.raises(TodoValidationError, match="content is required"):
        _validate([{"content": "  ", "active_form": "doing"}])


def test_validate_rejects_blank_active_form() -> None:
    with pytest.raises(TodoValidationError, match="active_form is required"):
        _validate([{"content": "x", "active_form": ""}])


def test_validate_rejects_unknown_status() -> None:
    with pytest.raises(TodoValidationError, match="status must be one of"):
        _validate(
            [{"content": "x", "active_form": "x", "status": "blocked"}]
        )


def test_validate_rejects_non_dict_item() -> None:
    with pytest.raises(TodoValidationError, match="must be an object"):
        _validate(["not a dict"])  # type: ignore[list-item]


# ---------------------------------------------------------------------------
# _todo_write_impl — happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_writes_to_agent_context_and_returns_summary() -> None:
    ctx = AgentContext()
    set_current_context(ctx)

    msg = await _todo_write_impl(
        {
            "todos": [
                {"content": "fix bug", "active_form": "fixing bug",
                 "status": "in_progress"},
                {"content": "add tests", "active_form": "adding tests"},
            ]
        }
    )
    assert "2 item(s)" in msg
    assert ctx.todos[0].content == "fix bug"
    assert ctx.todos[0].status == "in_progress"
    assert ctx.todos[1].status == "pending"


@pytest.mark.asyncio
async def test_emits_event_when_emitter_is_installed() -> None:
    captured: list[tuple[str, dict]] = []

    async def emitter(name: str, payload: dict) -> None:
        captured.append((name, payload))

    ctx = AgentContext(event_emitter=emitter)
    set_current_context(ctx)

    await _todo_write_impl(
        {
            "todos": [
                {"content": "x", "active_form": "doing x"},
            ]
        }
    )
    assert len(captured) == 1
    name, payload = captured[0]
    assert name == "todos_updated"
    assert payload["todos"][0]["content"] == "x"


@pytest.mark.asyncio
async def test_no_context_no_crash() -> None:
    """Calling the impl without an AgentContext bound must not crash."""
    import contextvars

    isolated = contextvars.copy_context()

    async def runner():
        from anila_core.context.agent_context import _current_context

        _current_context.set(None)  # type: ignore[arg-type]
        return await _todo_write_impl(
            {"todos": [{"content": "x", "active_form": "doing x"}]}
        )

    msg = await isolated.run(runner)
    assert "1 item(s)" in msg


# ---------------------------------------------------------------------------
# _todo_write_impl — error reporting
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_returns_error_string_when_invalid() -> None:
    msg = await _todo_write_impl(
        {
            "todos": [
                {
                    "content": "a",
                    "active_form": "..",
                    "status": "in_progress",
                },
                {
                    "content": "b",
                    "active_form": "..",
                    "status": "in_progress",
                },
            ]
        }
    )
    assert msg.startswith("todo_write error:")
    assert "At most one" in msg


@pytest.mark.asyncio
async def test_returns_error_when_todos_not_a_list() -> None:
    msg = await _todo_write_impl({"todos": "oops"})
    assert msg.startswith("todo_write error: 'todos' must be an array")
