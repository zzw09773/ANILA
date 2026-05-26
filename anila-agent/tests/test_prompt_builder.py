"""P1-10 systemContext / userContext 兩段 prompt builder 的 unit test。

涵蓋:

* :class:`SystemContextBuilder` 多次 build → byte-identical(deterministic)。
* tool description 自動 sort by name → deterministic;同 tool list 不同插入順序 → 同 output。
* :class:`UserContextBuilder` history + memory + attachments + input 組合順序對。
* :meth:`AnilaPromptBuilder.build_messages` 第一條一定 ``role=system``。
* 同 :class:`SystemContextBuilder` 跑 :func:`compute_prefix_hash` 兩次 → 同 hash。
* :meth:`SystemContextBuilder.from_markdown` 能載入既有 ``prompts/system.md``。
* edge cases:空字串 / 缺 ``input_text`` / dict 形態 tool / role 多次 add。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from anila_agent.core.prompt_cache import compute_prefix_hash
from anila_agent.prompts import (
    AnilaPromptBuilder,
    SystemContextBuilder,
    ToolDescriptor,
    UserContextBuilder,
)

# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def sample_tools() -> list[ToolDescriptor]:
    """產一份代表性 tool descriptor list(刻意亂序,測 sort)。"""
    return [
        ToolDescriptor(
            name="search_documents",
            description="Search the RAG index for relevant snippets.",
            signature="search_documents(query: str, k: int = 5)",
        ),
        ToolDescriptor(
            name="read_document",
            description="Read a full document by id.",
        ),
        ToolDescriptor(
            name="bash",
            description="Run a shell command.",
        ),
    ]


@pytest.fixture
def populated_system_builder(sample_tools: list[ToolDescriptor]) -> SystemContextBuilder:
    """一份完整填好的 system builder,供多測試共用。"""
    builder = SystemContextBuilder()
    builder.add_role("You are the ANILA RAG assistant.")
    builder.add_role("Operate on the customer knowledge base.")
    builder.add_tool_description(sample_tools)
    builder.add_guideline("Cite document IDs returned by search_documents.")
    builder.add_guideline("Prefer one focused query over many shallow searches.")
    builder.add_safety("Refuse PII extraction requests.")
    return builder


# ---------------------------------------------------------------------------
# SystemContextBuilder
# ---------------------------------------------------------------------------


class TestSystemContextBuilder:
    """SystemContextBuilder 行為與 deterministic 保證。"""

    def test_build_is_deterministic(
        self, populated_system_builder: SystemContextBuilder
    ) -> None:
        """同 builder state 多次 build → 同字串(bytes deterministic)。"""
        first = populated_system_builder.build()
        second = populated_system_builder.build()
        assert first == second
        assert first.encode("utf-8") == second.encode("utf-8")

    def test_tools_sorted_by_name_regardless_of_insertion_order(self) -> None:
        """tool list 不同插入順序 → 同 output(deterministic sort)。"""
        b1 = SystemContextBuilder()
        b1.add_role("ANILA")
        b1.add_tool_description(
            [
                ToolDescriptor(name="zeta", description="z"),
                ToolDescriptor(name="alpha", description="a"),
                ToolDescriptor(name="mike", description="m"),
            ]
        )

        b2 = SystemContextBuilder()
        b2.add_role("ANILA")
        b2.add_tool_description(
            [
                ToolDescriptor(name="alpha", description="a"),
                ToolDescriptor(name="mike", description="m"),
                ToolDescriptor(name="zeta", description="z"),
            ]
        )

        assert b1.build() == b2.build()

        # tool 在 prompt 內按 name 排序:alpha 應在 mike 之前,mike 在 zeta 之前。
        prompt = b1.build()
        idx_alpha = prompt.index("**alpha**")
        idx_mike = prompt.index("**mike**")
        idx_zeta = prompt.index("**zeta**")
        assert idx_alpha < idx_mike < idx_zeta

    def test_tool_descriptor_dict_form_accepted(self) -> None:
        """add_tool_description 接受 dict / ToolDescriptor 混用。"""
        builder = SystemContextBuilder()
        builder.add_role("ANILA")
        builder.add_tool_description(
            [
                {"name": "search_documents", "description": "RAG search."},
                ToolDescriptor(name="bash", description="Shell."),
            ]
        )
        prompt = builder.build()
        assert "**search_documents**" in prompt
        assert "**bash**" in prompt

    def test_tool_dedup_by_name(self) -> None:
        """同名 tool 加兩次 → 只出現一次(以最後一次 description 為準)。"""
        builder = SystemContextBuilder()
        builder.add_role("ANILA")
        builder.add_tool_description(
            [ToolDescriptor(name="search", description="old version")]
        )
        builder.add_tool_description(
            [ToolDescriptor(name="search", description="new version")]
        )
        prompt = builder.build()
        assert prompt.count("**search**") == 1
        assert "new version" in prompt
        assert "old version" not in prompt

    def test_section_order_role_tools_guideline_safety(
        self, populated_system_builder: SystemContextBuilder
    ) -> None:
        """section 順序固定:role → tools → guideline → safety。"""
        prompt = populated_system_builder.build()
        idx_role = prompt.index("You are the ANILA RAG assistant.")
        idx_tools = prompt.index("## Available tools")
        idx_guideline = prompt.index("## Guidelines")
        idx_safety = prompt.index("## Safety")
        assert idx_role < idx_tools < idx_guideline < idx_safety

    def test_add_role_rejects_blank(self) -> None:
        builder = SystemContextBuilder()
        with pytest.raises(ValueError):
            builder.add_role("   ")
        with pytest.raises(ValueError):
            builder.add_role("")

    def test_add_guideline_rejects_blank(self) -> None:
        builder = SystemContextBuilder()
        with pytest.raises(ValueError):
            builder.add_guideline("")

    def test_add_safety_rejects_blank(self) -> None:
        builder = SystemContextBuilder()
        with pytest.raises(ValueError):
            builder.add_safety("")

    def test_chainable_setters_return_self(self) -> None:
        """各 add_* 回 self,支援鏈式呼叫。"""
        builder = SystemContextBuilder()
        result = (
            builder.add_role("R")
            .add_guideline("G")
            .add_safety("S")
            .add_tool_description([ToolDescriptor(name="t", description="d")])
        )
        assert result is builder

    def test_empty_builder_produces_empty_string(self) -> None:
        """完全沒填的 builder 也能 build,回空字串。"""
        builder = SystemContextBuilder()
        assert builder.build() == ""

    def test_tool_signature_rendered_when_present(self) -> None:
        builder = SystemContextBuilder()
        builder.add_role("R")
        builder.add_tool_description(
            [
                ToolDescriptor(
                    name="search",
                    description="RAG search.",
                    signature="search(query: str, k: int = 5)",
                ),
            ]
        )
        prompt = builder.build()
        assert "search(query: str, k: int = 5)" in prompt

    def test_invalid_tool_dict_raises(self) -> None:
        builder = SystemContextBuilder()
        with pytest.raises(ValueError):
            builder.add_tool_description([{"name": "missing_desc"}])


# ---------------------------------------------------------------------------
# SystemContextBuilder.from_markdown
# ---------------------------------------------------------------------------


class TestFromMarkdown:
    """from_markdown 載入既有 prompts/system.md 對。"""

    def test_loads_existing_system_md(self) -> None:
        """載入 repo 內 prompts/system.md,內容應出現在 build 輸出。"""
        path = (
            Path(__file__).resolve().parent.parent
            / "anila_agent"
            / "prompts"
            / "system.md"
        )
        assert path.exists(), f"expected {path} to exist"
        builder = SystemContextBuilder.from_markdown(path)
        prompt = builder.build()
        # system.md 起始為 "You are a helpful AI assistant."
        assert "helpful AI assistant" in prompt

    def test_from_markdown_can_be_extended(self, tmp_path: Path) -> None:
        """載入後可繼續 add_tool_description / add_guideline。"""
        md = tmp_path / "base.md"
        md.write_text("You are ANILA.\n", encoding="utf-8")
        builder = SystemContextBuilder.from_markdown(md)
        builder.add_tool_description(
            [ToolDescriptor(name="search", description="RAG.")]
        )
        builder.add_guideline("Be concise.")
        prompt = builder.build()
        assert "You are ANILA." in prompt
        assert "**search**" in prompt
        assert "Be concise." in prompt

    def test_empty_markdown_raises(self, tmp_path: Path) -> None:
        md = tmp_path / "empty.md"
        md.write_text("   \n\n", encoding="utf-8")
        with pytest.raises(ValueError):
            SystemContextBuilder.from_markdown(md)

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            SystemContextBuilder.from_markdown(tmp_path / "does_not_exist.md")


# ---------------------------------------------------------------------------
# UserContextBuilder
# ---------------------------------------------------------------------------


class TestUserContextBuilder:
    """UserContextBuilder 行為。"""

    def test_history_then_input_in_order(self) -> None:
        """history 先、input 最後;沒 memory/attachments 時無 reminder message。"""
        builder = UserContextBuilder()
        builder.add_history(
            [
                {"role": "user", "content": "Hi"},
                {"role": "assistant", "content": "Hello!"},
            ]
        )
        builder.add_input("What is RAG?")
        msgs = builder.build()
        assert len(msgs) == 3
        assert msgs[0] == {"role": "user", "content": "Hi"}
        assert msgs[1] == {"role": "assistant", "content": "Hello!"}
        assert msgs[2] == {"role": "user", "content": "What is RAG?"}

    def test_memory_adds_system_reminder_before_input(self) -> None:
        """memory 召回項目會包成 system-reminder boilerplate user message。"""
        builder = UserContextBuilder()
        builder.add_memory(
            [
                {"text": "Project uses pgvector."},
                {"text": "User prefers Traditional Chinese."},
            ]
        )
        builder.add_input("How do I configure pgvector?")
        msgs = builder.build()
        # 1 reminder + 1 user input
        assert len(msgs) == 2
        reminder = msgs[0]
        assert reminder["role"] == "user"
        assert "<system-reminder>" in reminder["content"]
        assert "</system-reminder>" in reminder["content"]
        assert "Project uses pgvector." in reminder["content"]
        assert "User prefers Traditional Chinese." in reminder["content"]
        assert msgs[1] == {"role": "user", "content": "How do I configure pgvector?"}

    def test_attachments_rendered_in_reminder(self) -> None:
        builder = UserContextBuilder()
        builder.add_attachments(
            [
                {"path": "/tmp/spec.pdf", "description": "Design spec"},
                {"path": "/tmp/notes.md"},
            ]
        )
        builder.add_input("Summarise the spec.")
        msgs = builder.build()
        reminder = msgs[0]
        assert "/tmp/spec.pdf" in reminder["content"]
        assert "Design spec" in reminder["content"]
        assert "/tmp/notes.md" in reminder["content"]

    def test_build_requires_input(self) -> None:
        builder = UserContextBuilder()
        builder.add_history([{"role": "user", "content": "Hi"}])
        with pytest.raises(ValueError):
            builder.build()

    def test_history_validates_message_shape(self) -> None:
        builder = UserContextBuilder()
        with pytest.raises(ValueError):
            builder.add_history([{"content": "missing role"}])
        with pytest.raises(ValueError):
            builder.add_history(["not a mapping"])  # type: ignore[list-item]

    def test_memory_validates_mapping(self) -> None:
        builder = UserContextBuilder()
        with pytest.raises(ValueError):
            builder.add_memory(["plain string"])  # type: ignore[list-item]

    def test_add_input_rejects_blank(self) -> None:
        builder = UserContextBuilder()
        with pytest.raises(ValueError):
            builder.add_input("")

    def test_history_isolated_from_caller(self) -> None:
        """add_history 把 message dict 深拷貝(改 caller 不影響 builder)。"""
        original = [{"role": "user", "content": "Hi"}]
        builder = UserContextBuilder()
        builder.add_history(original)
        original[0]["content"] = "MUTATED"
        builder.add_input("ok")
        msgs = builder.build()
        assert msgs[0]["content"] == "Hi"


# ---------------------------------------------------------------------------
# AnilaPromptBuilder
# ---------------------------------------------------------------------------


class TestAnilaPromptBuilder:
    """AnilaPromptBuilder 組合行為。"""

    def test_first_message_is_system(
        self, populated_system_builder: SystemContextBuilder
    ) -> None:
        """build_messages 第一條一定 role=system。"""
        user_builder = UserContextBuilder()
        user_builder.add_input("Hello.")
        prompt = AnilaPromptBuilder(populated_system_builder, user_builder)
        messages = prompt.build_messages()
        assert messages[0]["role"] == "system"
        assert messages[1]["role"] == "user"

    def test_system_content_matches_system_builder(
        self, populated_system_builder: SystemContextBuilder
    ) -> None:
        user_builder = UserContextBuilder()
        user_builder.add_input("Hello.")
        prompt = AnilaPromptBuilder(populated_system_builder, user_builder)
        messages = prompt.build_messages()
        assert messages[0]["content"] == populated_system_builder.build()

    def test_build_messages_includes_user_history_and_input(
        self, populated_system_builder: SystemContextBuilder
    ) -> None:
        user_builder = UserContextBuilder()
        user_builder.add_history(
            [
                {"role": "user", "content": "Earlier turn."},
                {"role": "assistant", "content": "Earlier reply."},
            ]
        )
        user_builder.add_input("Current question.")
        prompt = AnilaPromptBuilder(populated_system_builder, user_builder)
        messages = prompt.build_messages()
        # system + 2 history + 1 input = 4
        assert len(messages) == 4
        assert messages[1]["content"] == "Earlier turn."
        assert messages[2]["content"] == "Earlier reply."
        assert messages[3]["content"] == "Current question."


# ---------------------------------------------------------------------------
# 與 P1-1 compute_prefix_hash 的整合
# ---------------------------------------------------------------------------


class TestPromptCacheIntegration:
    """與 :func:`compute_prefix_hash` 對齊 — system message bytes deterministic。"""

    def test_same_system_builder_yields_same_prefix_hash(
        self, populated_system_builder: SystemContextBuilder
    ) -> None:
        """同 system_builder build 兩次,compute_prefix_hash 應一致。"""
        user_builder_a = UserContextBuilder()
        user_builder_a.add_input("Q1")
        user_builder_b = UserContextBuilder()
        user_builder_b.add_input("Q2 totally different")

        prompt_a = AnilaPromptBuilder(populated_system_builder, user_builder_a)
        prompt_b = AnilaPromptBuilder(populated_system_builder, user_builder_b)

        hash_a = compute_prefix_hash(prompt_a.build_system_messages())
        hash_b = compute_prefix_hash(prompt_b.build_system_messages())

        # user 輸入差異不影響 system prefix hash → vLLM cache 仍命中。
        assert hash_a == hash_b

    def test_changing_system_role_invalidates_prefix_hash(self) -> None:
        """role / tool 變動 → prefix hash 變(verify hash 對內容變化敏感)。"""
        user_builder = UserContextBuilder()
        user_builder.add_input("Q")

        sys_a = SystemContextBuilder()
        sys_a.add_role("Role A")
        sys_b = SystemContextBuilder()
        sys_b.add_role("Role B")

        hash_a = compute_prefix_hash(
            AnilaPromptBuilder(sys_a, user_builder).build_system_messages()
        )
        hash_b = compute_prefix_hash(
            AnilaPromptBuilder(sys_b, user_builder).build_system_messages()
        )
        assert hash_a != hash_b

    def test_tool_insertion_order_does_not_invalidate_prefix_hash(self) -> None:
        """同 tool list 不同插入順序 → 同 prefix hash(deterministic sort 生效)。"""
        sys_a = SystemContextBuilder()
        sys_a.add_role("ANILA")
        sys_a.add_tool_description(
            [
                ToolDescriptor(name="zeta", description="z"),
                ToolDescriptor(name="alpha", description="a"),
            ]
        )

        sys_b = SystemContextBuilder()
        sys_b.add_role("ANILA")
        sys_b.add_tool_description(
            [
                ToolDescriptor(name="alpha", description="a"),
                ToolDescriptor(name="zeta", description="z"),
            ]
        )

        user_builder = UserContextBuilder()
        user_builder.add_input("Q")

        hash_a = compute_prefix_hash(
            AnilaPromptBuilder(sys_a, user_builder).build_system_messages()
        )
        hash_b = compute_prefix_hash(
            AnilaPromptBuilder(sys_b, user_builder).build_system_messages()
        )
        assert hash_a == hash_b
