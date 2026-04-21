"""Tests for Coordinator mode — worker spawn, task notification, scratchpad."""

from __future__ import annotations

import pytest

from anila_core.context.agent_context import AgentContext, create_subagent_context
from anila_core.coordinator.coordinator import (
    Coordinator,
    build_task_notification,
    parse_task_notification,
)
from anila_core.models.agent import AgentDefinition
from anila_core.models.message import UserMessage
from anila_core.providers.mock import MockProvider, ScriptedResponse
from anila_core.router.tool_router import ToolRegistry


# ---------------------------------------------------------------------------
# Task notification format
# ---------------------------------------------------------------------------

class TestTaskNotificationFormat:
    def test_build_task_notification(self) -> None:
        xml = build_task_notification(
            task_id="t1",
            status="completed",
            summary="Files reviewed",
            result="Found 3 issues",
        )
        assert 'task-id="t1"' in xml
        assert 'status="completed"' in xml
        assert "Found 3 issues" in xml

    def test_parse_task_notification(self) -> None:
        xml = build_task_notification(
            task_id="abc-123",
            status="completed",
            summary="Done",
            result="Success",
        )
        notif = parse_task_notification(xml)
        assert notif is not None
        assert notif.task_id == "abc-123"
        assert notif.status == "completed"
        assert notif.result == "Success"

    def test_parse_no_notification_returns_none(self) -> None:
        assert parse_task_notification("just plain text") is None

    def test_parse_notification_in_longer_text(self) -> None:
        xml = (
            "Some preamble. "
            + build_task_notification("t2", "completed", "summary", "result")
            + " Some postamble."
        )
        notif = parse_task_notification(xml)
        assert notif is not None
        assert notif.task_id == "t2"


# ---------------------------------------------------------------------------
# Context isolation
# ---------------------------------------------------------------------------

class TestContextIsolation:
    def test_create_subagent_context_isolates_messages(self) -> None:
        parent = AgentContext(
            session_id="s1",
            model="gpt-4o",
            messages=[UserMessage(content="parent message")],
        )
        child = create_subagent_context(parent)
        # Different lists
        assert child.messages is not parent.messages
        # Mutations don't cross
        child.messages.append(UserMessage(content="child message"))
        assert len(parent.messages) == 1

    def test_create_subagent_context_uses_agent_def_model(self) -> None:
        parent = AgentContext(session_id="s1", model="gpt-4o")
        agent_def = AgentDefinition(agent_type="fast", model="gpt-4o-mini")
        child = create_subagent_context(parent, agent_def=agent_def)
        assert child.model == "gpt-4o-mini"

    def test_create_subagent_inherits_parent_model_when_no_override(self) -> None:
        parent = AgentContext(session_id="s1", model="gpt-4o")
        child = create_subagent_context(parent)
        assert child.model == "gpt-4o"

    def test_subagent_has_independent_abort_signal(self) -> None:
        parent = AgentContext(session_id="s1", model="gpt-4o")
        child = create_subagent_context(parent)
        parent.abort()
        assert parent.is_aborted()
        assert not child.is_aborted()

    def test_subagent_tool_restriction(self) -> None:
        parent = AgentContext(
            session_id="s1",
            model="gpt-4o",
            allowed_tools={"bash", "file_read", "file_write"},
        )
        restricted = create_subagent_context(
            parent,
            allowed_tools={"file_read", "grep"},
        )
        assert restricted.allowed_tools == {"file_read", "grep"}

    def test_subagent_marked_as_forked(self) -> None:
        parent = AgentContext(session_id="s1", model="gpt-4o")
        child = create_subagent_context(parent)
        assert child.is_forked
        assert child.parent_context_id == parent.context_id


# ---------------------------------------------------------------------------
# Coordinator worker spawn
# ---------------------------------------------------------------------------

class TestCoordinatorWorkerSpawn:
    def _make_coordinator(self, script: list[ScriptedResponse]) -> Coordinator:
        provider = MockProvider(script)
        registry = ToolRegistry()
        parent = AgentContext(session_id="s1", model="gpt-4o")
        return Coordinator(provider, registry, parent)

    @pytest.mark.asyncio
    async def test_spawn_single_worker(self) -> None:
        script = [ScriptedResponse(text="Worker result here")]
        coord = self._make_coordinator(script)
        agent_def = AgentDefinition(agent_type="researcher", max_turns=3)
        task = await coord.spawn_worker(agent_def, "Research topic X")
        assert task.state.status == "completed"
        assert task.result is not None

    @pytest.mark.asyncio
    async def test_spawn_worker_failure(self) -> None:
        script = [ScriptedResponse(raise_error=RuntimeError("provider down"))]
        coord = self._make_coordinator(script)
        agent_def = AgentDefinition(agent_type="researcher", max_turns=1)
        task = await coord.spawn_worker(agent_def, "Research topic Y")
        assert task.state.status == "failed"
        assert "provider down" in (task.state.error or "")

    @pytest.mark.asyncio
    async def test_spawn_workers_parallel(self) -> None:
        # Each worker gets its own response
        script = [
            ScriptedResponse(text="Result A"),
            ScriptedResponse(text="Result B"),
            ScriptedResponse(text="Result C"),
        ]
        provider = MockProvider(script)
        registry = ToolRegistry()
        parent = AgentContext(session_id="s1", model="gpt-4o")
        coord = Coordinator(provider, registry, parent)
        agent_def = AgentDefinition(agent_type="researcher", max_turns=1)
        tasks = await coord.spawn_workers_parallel(agent_def, ["A", "B", "C"])
        assert len(tasks) == 3
        assert all(t.state.status == "completed" for t in tasks)

    @pytest.mark.asyncio
    async def test_spawn_workers_sequential(self) -> None:
        script = [
            ScriptedResponse(text="Step 1"),
            ScriptedResponse(text="Step 2"),
        ]
        provider = MockProvider(script)
        registry = ToolRegistry()
        parent = AgentContext(session_id="s1", model="gpt-4o")
        coord = Coordinator(provider, registry, parent)
        agent_def = AgentDefinition(agent_type="writer", max_turns=1)
        tasks = await coord.spawn_workers_sequential(agent_def, ["Step A", "Step B"])
        assert len(tasks) == 2
        # Sequential: all done
        assert all(t.state.status == "completed" for t in tasks)

    @pytest.mark.asyncio
    async def test_get_task_by_id(self) -> None:
        script = [ScriptedResponse(text="Done")]
        coord = self._make_coordinator(script)
        agent_def = AgentDefinition(agent_type="worker", max_turns=1)
        task = await coord.spawn_worker(agent_def, "Do X")
        retrieved = coord.get_task(task.task_id)
        assert retrieved is task

    @pytest.mark.asyncio
    async def test_all_tasks(self) -> None:
        script = [ScriptedResponse(text="A"), ScriptedResponse(text="B")]
        coord = self._make_coordinator(script)
        agent_def = AgentDefinition(agent_type="w", max_turns=1)
        await coord.spawn_worker(agent_def, "p1")
        await coord.spawn_worker(agent_def, "p2")
        assert len(coord.all_tasks()) == 2

    @pytest.mark.asyncio
    async def test_build_results_summary(self) -> None:
        script = [ScriptedResponse(text="Worker completed task.")]
        coord = self._make_coordinator(script)
        agent_def = AgentDefinition(agent_type="w", max_turns=1)
        await coord.spawn_worker(agent_def, "do work")
        summary = coord.build_results_summary()
        assert "task-notification" in summary or summary == ""
