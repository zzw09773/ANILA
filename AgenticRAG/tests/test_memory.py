"""Tests for Memory lifecycle — extraction, relevance selection, consolidation."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from agentic_rag.context.agent_context import AgentContext
from agentic_rag.memory.consolidation import (
    ConsolidationConfig,
    ConsolidationService,
    build_consolidation_prompt,
)
from agentic_rag.memory.extract_memories import MemoryExtractor
from agentic_rag.memory.memdir import (
    MAX_ENTRYPOINT_LINES,
    MemdirManager,
    scan_memory_files,
    truncate_entrypoint_content,
)
from agentic_rag.memory.relevance_selector import (
    ModelBasedRelevanceSelector,
    format_memory_manifest,
)
from agentic_rag.models.memory import MemoryHeader, MemoryType
from agentic_rag.models.message import AssistantMessage, ToolCall, UserMessage
from agentic_rag.providers.mock import MockProvider, ScriptedResponse


# ---------------------------------------------------------------------------
# MemdirManager
# ---------------------------------------------------------------------------

class TestMemdirManager:
    def test_ensure_dir_creates_directory(self, tmp_path: Path) -> None:
        mem_dir = str(tmp_path / "memory")
        mgr = MemdirManager(mem_dir)
        mgr.ensure_dir()
        assert os.path.isdir(mem_dir)

    def test_read_empty_entrypoint(self, tmp_path: Path) -> None:
        mgr = MemdirManager(str(tmp_path))
        assert mgr.read_entrypoint() == ""

    def test_write_and_read_entrypoint(self, tmp_path: Path) -> None:
        mgr = MemdirManager(str(tmp_path))
        mgr.ensure_dir()
        mgr.write_entrypoint("- [Topic](topic.md) - a hook\n")
        content = mgr.read_entrypoint()
        assert "Topic" in content

    def test_build_memory_prompt_empty(self, tmp_path: Path) -> None:
        mgr = MemdirManager(str(tmp_path))
        mgr.ensure_dir()
        prompt = mgr.build_memory_prompt()
        assert "MEMORY.md" in prompt
        assert "currently empty" in prompt

    def test_build_memory_prompt_with_content(self, tmp_path: Path) -> None:
        mgr = MemdirManager(str(tmp_path))
        mgr.ensure_dir()
        mgr.write_entrypoint("- [Test](test.md) - hook\n")
        prompt = mgr.build_memory_prompt()
        assert "Test" in prompt

    def test_add_index_entry(self, tmp_path: Path) -> None:
        mgr = MemdirManager(str(tmp_path))
        mgr.ensure_dir()
        mgr.add_index_entry("My Topic", "topic.md", "a useful hook")
        content = mgr.read_entrypoint()
        assert "My Topic" in content
        assert "topic.md" in content

    def test_add_index_entry_no_duplicate(self, tmp_path: Path) -> None:
        mgr = MemdirManager(str(tmp_path))
        mgr.ensure_dir()
        mgr.add_index_entry("Topic", "topic.md", "hook")
        mgr.add_index_entry("Topic", "topic.md", "hook")
        content = mgr.read_entrypoint()
        assert content.count("topic.md") == 1


class TestTruncateEntrypoint:
    def test_no_truncation_needed(self) -> None:
        content = "- [A](a.md) - hook\n" * 5
        result, was_line, was_byte = truncate_entrypoint_content(content)
        assert not was_line
        assert not was_byte
        assert "A" in result

    def test_line_truncation(self) -> None:
        content = "- [Entry](e.md) - hook\n" * (MAX_ENTRYPOINT_LINES + 50)
        result, was_line, was_byte = truncate_entrypoint_content(content)
        assert was_line
        assert "WARNING" in result
        line_count = result.count("\n")
        assert line_count <= MAX_ENTRYPOINT_LINES + 5  # +5 for warning text


class TestScanMemoryFiles:
    @pytest.mark.asyncio
    async def test_scan_empty_dir(self, tmp_path: Path) -> None:
        headers = await scan_memory_files(str(tmp_path))
        assert headers == []

    @pytest.mark.asyncio
    async def test_scan_finds_md_files(self, tmp_path: Path) -> None:
        # Write a memory file with frontmatter
        mem_file = tmp_path / "pref.md"
        mem_file.write_text(
            "---\ntitle: My Pref\ndescription: test pref\ntype: user_preference\n---\nBody\n"
        )
        headers = await scan_memory_files(str(tmp_path))
        assert len(headers) == 1
        assert headers[0].title == "My Pref"

    @pytest.mark.asyncio
    async def test_scan_excludes_memory_md(self, tmp_path: Path) -> None:
        (tmp_path / "MEMORY.md").write_text("- [X](x.md) - hook\n")
        (tmp_path / "x.md").write_text("---\ntitle: X\n---\nContent\n")
        headers = await scan_memory_files(str(tmp_path))
        assert all(h.filename != "MEMORY.md" for h in headers)
        assert len(headers) == 1


# ---------------------------------------------------------------------------
# MemoryExtractor
# ---------------------------------------------------------------------------

class TestMemoryExtractor:
    def _make_messages_with_final_answer(self, n_turns: int = 3) -> list:
        messages = []
        for i in range(n_turns):
            messages.append(AssistantMessage(
                content=[{"type": "tool_use", "id": f"t{i}", "name": "bash", "input": {}}],
                tool_calls=[ToolCall(id=f"t{i}", name="bash", input={})],
            ))
            messages.append(UserMessage(
                content=[{"type": "tool_result", "tool_use_id": f"t{i}", "content": "output"}]
            ))
        messages.append(AssistantMessage(content="Final answer.", tool_calls=[]))
        return messages

    @pytest.mark.asyncio
    async def test_extraction_calls_run_forked(self, tmp_path: Path) -> None:
        extractor = MemoryExtractor(memory_dir=str(tmp_path))
        messages = self._make_messages_with_final_answer()
        ctx = AgentContext(session_id="s1", model="test")

        called_with: list = []

        async def mock_run_forked(msgs, ctx, prompt):
            called_with.append(prompt)
            return []

        await extractor.run(messages, ctx, mock_run_forked)
        assert len(called_with) == 1
        assert "memory extraction subagent" in called_with[0].lower()

    @pytest.mark.asyncio
    async def test_extraction_skips_if_main_agent_wrote_memory(self, tmp_path: Path) -> None:
        mem_dir = str(tmp_path / "memory")
        extractor = MemoryExtractor(memory_dir=mem_dir)

        # Last assistant message wrote to memory dir
        messages = [
            AssistantMessage(
                content=[{
                    "type": "tool_use",
                    "id": "t1",
                    "name": "file_write",
                    "input": {"file_path": f"{mem_dir}/pref.md"},
                }],
                tool_calls=[],
            ),
            AssistantMessage(content="Done.", tool_calls=[]),
        ]
        ctx = AgentContext(session_id="s1", model="test")
        called = False

        async def mock_run(msgs, ctx, prompt):
            nonlocal called
            called = True
            return []

        await extractor.run(messages, ctx, mock_run)
        assert not called

    @pytest.mark.asyncio
    async def test_extractor_advances_cursor(self, tmp_path: Path) -> None:
        extractor = MemoryExtractor(memory_dir=str(tmp_path))
        messages = self._make_messages_with_final_answer(n_turns=2)
        ctx = AgentContext(session_id="s1", model="test")
        call_count = 0

        async def mock_run(msgs, ctx, prompt):
            nonlocal call_count
            call_count += 1
            return []

        await extractor.run(messages, ctx, mock_run)
        # Add more messages
        messages = messages + [
            AssistantMessage(content="Follow-up.", tool_calls=[]),
        ]
        await extractor.run(messages, ctx, mock_run)
        assert call_count == 2  # both runs happened


# ---------------------------------------------------------------------------
# RelevanceSelector
# ---------------------------------------------------------------------------

class TestModelBasedRelevanceSelector:
    def _make_headers(self) -> list[MemoryHeader]:
        return [
            MemoryHeader(
                filename="pref.md",
                file_path="/memory/pref.md",
                title="User Preference",
                description="How user likes to work",
                memory_type=MemoryType.USER_PREFERENCE,
                mtime_ms=1000.0,
            ),
            MemoryHeader(
                filename="project.md",
                file_path="/memory/project.md",
                title="Project Conventions",
                description="Coding conventions for this project",
                memory_type=MemoryType.PROJECT_CONVENTION,
                mtime_ms=2000.0,
            ),
            MemoryHeader(
                filename="debug.md",
                file_path="/memory/debug.md",
                title="Debug Lesson",
                description="How to debug async issues",
                memory_type=MemoryType.DEBUGGING_LESSON,
                mtime_ms=500.0,
            ),
        ]

    @pytest.mark.asyncio
    async def test_selects_relevant_memories(self) -> None:
        import json
        selected = {"selected_memories": ["pref.md", "project.md"]}
        provider = MockProvider([
            ScriptedResponse(text=json.dumps(selected))
        ])
        selector = ModelBasedRelevanceSelector(provider, model="test-model")
        headers = self._make_headers()
        result = await selector.select(
            query="user preferences",
            memory_headers=headers,
            recent_tools=[],
            already_surfaced=set(),
        )
        filenames = [r.filename for r in result]
        assert "pref.md" in filenames
        assert "project.md" in filenames

    @pytest.mark.asyncio
    async def test_filters_already_surfaced(self) -> None:
        import json
        selected = {"selected_memories": ["pref.md", "project.md"]}
        provider = MockProvider([
            ScriptedResponse(text=json.dumps(selected))
        ])
        selector = ModelBasedRelevanceSelector(provider, model="test-model")
        headers = self._make_headers()
        result = await selector.select(
            query="something",
            memory_headers=headers,
            recent_tools=[],
            already_surfaced={"pref.md"},  # filter this out
        )
        filenames = [r.filename for r in result]
        assert "pref.md" not in filenames

    @pytest.mark.asyncio
    async def test_handles_empty_response(self) -> None:
        import json
        provider = MockProvider([
            ScriptedResponse(text=json.dumps({"selected_memories": []}))
        ])
        selector = ModelBasedRelevanceSelector(provider, model="test-model")
        result = await selector.select(
            query="test",
            memory_headers=self._make_headers(),
            recent_tools=[],
            already_surfaced=set(),
        )
        assert result == []

    @pytest.mark.asyncio
    async def test_handles_provider_error_gracefully(self) -> None:
        provider = MockProvider([
            ScriptedResponse(raise_error=Exception("provider error"))
        ])
        selector = ModelBasedRelevanceSelector(provider, model="test-model")
        result = await selector.select(
            query="test",
            memory_headers=self._make_headers(),
            recent_tools=[],
            already_surfaced=set(),
        )
        assert result == []

    def test_format_memory_manifest(self) -> None:
        headers = self._make_headers()
        manifest = format_memory_manifest(headers)
        assert "pref.md" in manifest
        assert "project.md" in manifest
        assert "user_preference" in manifest


# ---------------------------------------------------------------------------
# ConsolidationService
# ---------------------------------------------------------------------------

class TestConsolidationService:
    @pytest.mark.asyncio
    async def test_time_gate_blocks_recent_consolidation(self, tmp_path: Path) -> None:
        config = ConsolidationConfig(min_hours=24, min_sessions=1)
        # Create a recent lock file
        lock_path = tmp_path / ".consolidate-lock"
        lock_path.write_text(str(os.getpid()))
        # Set mtime to "just now"
        now = time.time()
        os.utime(lock_path, (now, now))

        svc = ConsolidationService(
            memory_dir=str(tmp_path),
            sessions_dir=str(tmp_path),
            config=config,
        )
        should_run, session_ids = await svc.should_consolidate()
        assert not should_run

    @pytest.mark.asyncio
    async def test_session_gate_blocks_insufficient_sessions(self, tmp_path: Path) -> None:
        config = ConsolidationConfig(min_hours=0, min_sessions=10)
        svc = ConsolidationService(
            memory_dir=str(tmp_path),
            sessions_dir=str(tmp_path),
            config=config,
        )
        # Force scan interval to 0
        svc._last_session_scan = 0
        should_run, session_ids = await svc.should_consolidate()
        assert not should_run

    @pytest.mark.asyncio
    async def test_run_calls_forked_fn(self, tmp_path: Path) -> None:
        config = ConsolidationConfig(min_hours=0, min_sessions=1)

        # Create fake session files
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        for i in range(3):
            f = sessions_dir / f"session-{i}.jsonl"
            f.write_text("{}")

        svc = ConsolidationService(
            memory_dir=str(tmp_path),
            sessions_dir=str(sessions_dir),
            config=config,
        )
        svc._last_session_scan = 0

        forked_called = False

        async def mock_forked(mem_dir, prompt):
            nonlocal forked_called
            forked_called = True
            return ["memory/file.md"]

        # Run directly with a known session list to bypass all gates
        result = await svc.run(mock_forked, session_ids=["session-0", "session-1", "session-2"])
        # The forked function should have been called
        assert forked_called
        assert result == ["memory/file.md"]

    def test_build_consolidation_prompt_contains_dirs(self) -> None:
        prompt = build_consolidation_prompt(
            memory_dir="/memory",
            transcript_dir="/transcripts",
            session_ids=["session-abc", "session-xyz"],
        )
        assert "/memory" in prompt
        assert "/transcripts" in prompt
        assert "session-abc" in prompt
        assert "orient" in prompt.lower()
        assert "consolidate" in prompt.lower()
