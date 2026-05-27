"""P2-3 ``prompt_with_handoff_instructions`` + :class:`HandoffInstructionsBuilder` 單元測試。

涵蓋:

* :func:`prompt_with_handoff_instructions` 基本 format(header / intro / bullet / outro)。
* 多 handoff 按 ``name`` 排序 → deterministic;插入順序不影響 output bytes。
* i18n 雙語 (``zh-TW`` / ``en``) — header 字串正確、語系切換不污染狀態。
* 與 P1-10 :class:`SystemContextBuilder` 整合 — :meth:`HandoffInstructionsBuilder.to_system_context`
  把 handoff section 以 ``add_role`` 形式塞進 builder。
* 與 P2-1 :func:`as_tool` 整合 — 把 sub-agent.as_tool() 結果丟進 handoffs。
* 與 P1-1 :func:`compute_prefix_hash` 整合 — 同 handoff list → 同 instructions → 同 hash。
* edge cases:空 handoffs / 同名去重 / 非 AgentTool 型別 raise TypeError。
"""

from __future__ import annotations

from typing import Any

import pytest
from agents import Agent

from anila_agent.core import (
    AgentTool,
    as_tool,
    compute_prefix_hash,
    make_agent_tool,
)
from anila_agent.extensions import (
    DEFAULT_HANDOFF_INSTRUCTIONS_LANGUAGE,
    HANDOFF_SECTION_HEADER_EN,
    HANDOFF_SECTION_HEADER_ZH,
    HandoffInstructionsBuilder,
    prompt_with_handoff_instructions,
)
from anila_agent.prompts import SystemContextBuilder

# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


def _make_agent(name: str, instructions: str = "Do something useful.") -> Agent[Any]:
    """建一個最小 sub-agent;只當 dispatch target,不真的跑 LLM。"""
    return Agent(name=name, instructions=instructions)


@pytest.fixture
def retriever_tool() -> AgentTool:
    """retriever sub-agent 包成 AgentTool。"""
    return make_agent_tool(
        _make_agent("retriever", "Search the RAG index and return top chunks."),
        name="retriever",
        description="Search the RAG index for relevant chunks.",
    )


@pytest.fixture
def writer_tool() -> AgentTool:
    """writer sub-agent 包成 AgentTool。"""
    return make_agent_tool(
        _make_agent("writer", "Write a polished Traditional Chinese summary."),
        name="writer",
        description="Turn retrieved chunks into a Traditional Chinese summary.",
    )


@pytest.fixture
def summarizer_tool() -> AgentTool:
    """summarizer sub-agent 包成 AgentTool — 用來測同名去重 / 三個排序。"""
    return make_agent_tool(
        _make_agent("summarizer", "Compress text into bullets."),
        name="summarizer",
        description="Compress long text into bullet summaries.",
    )


@pytest.fixture
def three_handoffs(
    retriever_tool: AgentTool,
    writer_tool: AgentTool,
    summarizer_tool: AgentTool,
) -> list[AgentTool]:
    """三個 handoff 的 fixture(刻意亂序,測 sort)。"""
    return [writer_tool, retriever_tool, summarizer_tool]


# ---------------------------------------------------------------------------
# 1. 基本 format
# ---------------------------------------------------------------------------


class TestPromptWithHandoffInstructionsBasic:
    """:func:`prompt_with_handoff_instructions` 基本 format / 預設語系。"""

    def test_default_language_is_zh_tw(self) -> None:
        """預設語言應為 ``zh-TW``。"""
        assert DEFAULT_HANDOFF_INSTRUCTIONS_LANGUAGE == "zh-TW"

    def test_appends_handoff_section_after_base_prompt(
        self, retriever_tool: AgentTool
    ) -> None:
        """handoff section 應該被 append 到 base_prompt 之後。"""
        base = "You are an orchestrator."
        result = prompt_with_handoff_instructions(base, [retriever_tool])

        assert result.startswith(base)
        assert HANDOFF_SECTION_HEADER_ZH in result
        # bullet 必然出現 name 與 description
        assert "**retriever**" in result
        assert "Search the RAG index" in result

    def test_empty_handoffs_returns_base_prompt_unchanged(self) -> None:
        """handoffs 為空 → 原樣回 base_prompt,不加任何分隔符。"""
        base = "You are an orchestrator."
        result = prompt_with_handoff_instructions(base, [])
        assert result == base

    def test_uses_separator_between_base_and_section(
        self, retriever_tool: AgentTool
    ) -> None:
        """base 與 section 之間應有雙換行,讓 LLM 認得段落邊界。"""
        base = "You are an orchestrator."
        result = prompt_with_handoff_instructions(base, [retriever_tool])
        # 至少有一處 \n\n 連接 base + section
        assert "\n\n" in result
        # base 後第一個非空行應該是 section header
        suffix = result[len(base) :]
        assert suffix.startswith("\n\n")
        assert HANDOFF_SECTION_HEADER_ZH in suffix


# ---------------------------------------------------------------------------
# 2. deterministic ordering — 按 name 排序
# ---------------------------------------------------------------------------


class TestDeterministicOrdering:
    """handoff 列表 deterministic 順序。"""

    def test_handoff_bullets_sorted_by_name(
        self, three_handoffs: list[AgentTool]
    ) -> None:
        """三個 handoff 應依 name 排序:retriever / summarizer / writer。"""
        result = prompt_with_handoff_instructions(
            "Base prompt.", three_handoffs, language="en"
        )
        idx_retriever = result.index("**retriever**")
        idx_summarizer = result.index("**summarizer**")
        idx_writer = result.index("**writer**")
        assert idx_retriever < idx_summarizer < idx_writer

    def test_same_handoffs_different_insertion_order_byte_identical(
        self,
        retriever_tool: AgentTool,
        writer_tool: AgentTool,
        summarizer_tool: AgentTool,
    ) -> None:
        """同樣三個 handoff,插入順序不同 → output bytes 應完全一致。"""
        order_a = [retriever_tool, writer_tool, summarizer_tool]
        order_b = [writer_tool, summarizer_tool, retriever_tool]
        order_c = [summarizer_tool, retriever_tool, writer_tool]

        out_a = prompt_with_handoff_instructions("base", order_a)
        out_b = prompt_with_handoff_instructions("base", order_b)
        out_c = prompt_with_handoff_instructions("base", order_c)

        assert out_a == out_b == out_c

    def test_duplicate_name_deduped_with_last_wins(self) -> None:
        """同 name 加多次 → 只出現一條 bullet(以最後一個 description 為準)。"""
        agent = _make_agent("retriever", "first.")
        first = make_agent_tool(agent, name="retriever", description="First description.")
        second = make_agent_tool(agent, name="retriever", description="Second description.")
        result = prompt_with_handoff_instructions("base", [first, second], language="en")

        # 只應該有一行 bullet
        assert result.count("- **retriever**") == 1
        # 以最後一個 description 為準
        assert "Second description." in result
        assert "First description." not in result


# ---------------------------------------------------------------------------
# 3. i18n — zh-TW / en 雙版
# ---------------------------------------------------------------------------


class TestI18nSupport:
    """i18n header / intro / outro 字串切換。"""

    def test_zh_tw_uses_zh_header(self, retriever_tool: AgentTool) -> None:
        """zh-TW 應使用 :data:`HANDOFF_SECTION_HEADER_ZH`。"""
        result = prompt_with_handoff_instructions(
            "base", [retriever_tool], language="zh-TW"
        )
        assert HANDOFF_SECTION_HEADER_ZH in result
        assert HANDOFF_SECTION_HEADER_EN not in result
        # 預期出現的台灣用語關鍵字
        assert "委派" in result

    def test_en_uses_en_header(self, retriever_tool: AgentTool) -> None:
        """en 應使用 :data:`HANDOFF_SECTION_HEADER_EN`。"""
        result = prompt_with_handoff_instructions(
            "base", [retriever_tool], language="en"
        )
        assert HANDOFF_SECTION_HEADER_EN in result
        assert HANDOFF_SECTION_HEADER_ZH not in result
        assert "delegate" in result

    def test_language_switch_yields_different_bytes(
        self, retriever_tool: AgentTool
    ) -> None:
        """同 handoffs,不同 language → 字串不同(且不污染下一次呼叫)。"""
        zh = prompt_with_handoff_instructions("base", [retriever_tool], language="zh-TW")
        en = prompt_with_handoff_instructions("base", [retriever_tool], language="en")
        zh_again = prompt_with_handoff_instructions(
            "base", [retriever_tool], language="zh-TW"
        )

        assert zh != en
        assert zh == zh_again  # 無 hidden state 污染


# ---------------------------------------------------------------------------
# 4. HandoffInstructionsBuilder — builder pattern
# ---------------------------------------------------------------------------


class TestHandoffInstructionsBuilder:
    """:class:`HandoffInstructionsBuilder` 行為。"""

    def test_add_handoff_and_build(self, retriever_tool: AgentTool) -> None:
        """單一 add_handoff → build → section 含該 tool。"""
        builder = HandoffInstructionsBuilder()
        builder.add_handoff(retriever_tool)
        section = builder.build()

        assert HANDOFF_SECTION_HEADER_ZH in section
        assert "**retriever**" in section

    def test_add_handoffs_iterable(self, three_handoffs: list[AgentTool]) -> None:
        """add_handoffs 接 iterable,所有 tool 都應出現在 section 內。"""
        builder = HandoffInstructionsBuilder()
        builder.add_handoffs(three_handoffs)
        section = builder.build()

        assert "**retriever**" in section
        assert "**writer**" in section
        assert "**summarizer**" in section

    def test_empty_builder_build_returns_empty_string(self) -> None:
        """沒 add 任何 handoff,build 應回空字串。"""
        builder = HandoffInstructionsBuilder()
        assert builder.build() == ""

    def test_build_is_deterministic(self, three_handoffs: list[AgentTool]) -> None:
        """同 builder state 多次 build → byte-identical。"""
        builder = HandoffInstructionsBuilder()
        builder.add_handoffs(three_handoffs)

        first = builder.build()
        second = builder.build()
        third = builder.build()

        assert first == second == third

    def test_chainable_methods(self, retriever_tool: AgentTool) -> None:
        """add_handoff / add_handoffs 都應回 self,支援鏈式呼叫。"""
        builder = HandoffInstructionsBuilder()
        result = builder.add_handoff(retriever_tool).add_handoffs([])
        assert result is builder

    def test_non_agent_tool_raises_type_error(self) -> None:
        """非 :class:`AgentTool` 應 raise TypeError。"""
        builder = HandoffInstructionsBuilder()
        with pytest.raises(TypeError, match="AgentTool"):
            builder.add_handoff("not an AgentTool")  # type: ignore[arg-type]

    def test_language_parameter_on_builder(self, retriever_tool: AgentTool) -> None:
        """builder.language 設 en 後 build 應出英文 header。"""
        builder = HandoffInstructionsBuilder(language="en")
        builder.add_handoff(retriever_tool)
        section = builder.build()
        assert HANDOFF_SECTION_HEADER_EN in section
        assert HANDOFF_SECTION_HEADER_ZH not in section


# ---------------------------------------------------------------------------
# 5. 與 P1-10 SystemContextBuilder 整合
# ---------------------------------------------------------------------------


class TestSystemContextIntegration:
    """:meth:`HandoffInstructionsBuilder.to_system_context` 與 P1-10 整合。"""

    def test_to_system_context_injects_section_via_add_role(
        self, retriever_tool: AgentTool, writer_tool: AgentTool
    ) -> None:
        """to_system_context 應透過 add_role 把 section 塞進 builder。"""
        sys_builder = SystemContextBuilder()
        sys_builder.add_role("You are an orchestrator.")

        handoff_builder = HandoffInstructionsBuilder()
        handoff_builder.add_handoffs([retriever_tool, writer_tool])
        handoff_builder.to_system_context(sys_builder)

        prompt = sys_builder.build()
        assert "You are an orchestrator." in prompt
        assert HANDOFF_SECTION_HEADER_ZH in prompt
        assert "**retriever**" in prompt
        assert "**writer**" in prompt

    def test_to_system_context_returns_builder_for_chaining(
        self, retriever_tool: AgentTool
    ) -> None:
        """to_system_context 應回 builder 本身,支援鏈式。"""
        sys_builder = SystemContextBuilder()
        sys_builder.add_role("Base.")

        result = HandoffInstructionsBuilder().add_handoff(
            retriever_tool
        ).to_system_context(sys_builder)
        assert result is sys_builder

    def test_to_system_context_with_empty_handoffs_is_noop(self) -> None:
        """沒 handoff 時 to_system_context 不應動 builder。"""
        sys_builder = SystemContextBuilder()
        sys_builder.add_role("Base only.")
        before = sys_builder.build()

        HandoffInstructionsBuilder().to_system_context(sys_builder)
        after = sys_builder.build()
        assert before == after

    def test_system_context_with_handoff_is_deterministic(
        self, three_handoffs: list[AgentTool]
    ) -> None:
        """同樣 SystemContextBuilder + 同 handoff list → byte-identical prompt。"""

        def _build_prompt() -> str:
            sb = SystemContextBuilder()
            sb.add_role("Orchestrator role.")
            HandoffInstructionsBuilder().add_handoffs(three_handoffs).to_system_context(sb)
            return sb.build()

        assert _build_prompt() == _build_prompt() == _build_prompt()


# ---------------------------------------------------------------------------
# 6. 與 P2-1 as_tool 整合
# ---------------------------------------------------------------------------


class TestAsToolIntegration:
    """:func:`as_tool` (P2-1) 產的 :class:`AgentTool` 可直接丟進 handoffs。"""

    def test_as_tool_result_works_as_handoff(self) -> None:
        """as_tool 包出來的 spec 餵進 prompt_with_handoff_instructions 應正常 render。"""
        retriever_agent = _make_agent("retriever", "Search index.")
        writer_agent = _make_agent("writer", "Write summary.")

        retriever = as_tool(retriever_agent, "retriever", "Search the index for chunks.")
        writer = as_tool(writer_agent, "writer", "Write a polished summary.")

        result = prompt_with_handoff_instructions(
            "Base prompt.", [retriever, writer], language="en"
        )

        assert "**retriever**" in result
        assert "**writer**" in result
        assert "Search the index for chunks." in result
        assert "Write a polished summary." in result

    def test_mixed_make_agent_tool_and_as_tool(self) -> None:
        """混用 make_agent_tool 與 as_tool 包出的 spec → 仍 deterministic 排序。"""
        agent_a = _make_agent("alpha", "first.")
        agent_b = _make_agent("bravo", "second.")
        alpha = make_agent_tool(agent_a, name="alpha", description="Alpha tool.")
        bravo = as_tool(agent_b, "bravo", "Bravo tool.")

        builder = HandoffInstructionsBuilder()
        builder.add_handoffs([bravo, alpha])  # 故意亂序
        section = builder.build()

        idx_alpha = section.index("**alpha**")
        idx_bravo = section.index("**bravo**")
        assert idx_alpha < idx_bravo


# ---------------------------------------------------------------------------
# 7. 與 P1-1 prefix hash 整合
# ---------------------------------------------------------------------------


class TestPrefixHashIntegration:
    """同 handoff list + 同 base prompt → 同 instructions → 同 :func:`compute_prefix_hash`。"""

    def test_same_handoffs_yield_same_prefix_hash(
        self, three_handoffs: list[AgentTool]
    ) -> None:
        """同 handoff list 跑兩次 → 同 instructions content → 同 hash。"""
        prompt_one = prompt_with_handoff_instructions("Base.", three_handoffs)
        prompt_two = prompt_with_handoff_instructions("Base.", list(three_handoffs))

        hash_one = compute_prefix_hash([{"role": "system", "content": prompt_one}])
        hash_two = compute_prefix_hash([{"role": "system", "content": prompt_two}])
        assert hash_one == hash_two

    def test_different_insertion_order_same_prefix_hash(
        self,
        retriever_tool: AgentTool,
        writer_tool: AgentTool,
        summarizer_tool: AgentTool,
    ) -> None:
        """插入順序不同但內容相同 → 同 hash(deterministic prefix cache 友善)。"""
        order_a = [retriever_tool, writer_tool, summarizer_tool]
        order_b = [summarizer_tool, retriever_tool, writer_tool]

        prompt_a = prompt_with_handoff_instructions("Base.", order_a)
        prompt_b = prompt_with_handoff_instructions("Base.", order_b)

        hash_a = compute_prefix_hash([{"role": "system", "content": prompt_a}])
        hash_b = compute_prefix_hash([{"role": "system", "content": prompt_b}])
        assert hash_a == hash_b

    def test_added_handoff_changes_prefix_hash(
        self, retriever_tool: AgentTool, writer_tool: AgentTool
    ) -> None:
        """新增 handoff → instructions 變 → hash 必然變(sanity check)。"""
        smaller = prompt_with_handoff_instructions("Base.", [retriever_tool])
        larger = prompt_with_handoff_instructions("Base.", [retriever_tool, writer_tool])

        hash_small = compute_prefix_hash([{"role": "system", "content": smaller}])
        hash_large = compute_prefix_hash([{"role": "system", "content": larger}])
        assert hash_small != hash_large

    def test_builder_to_system_context_hash_matches_pure_function(
        self, three_handoffs: list[AgentTool]
    ) -> None:
        """Builder 流程與 pure function 流程在 prefix hash 上應一致。

        前提:兩條路徑 base prompt 結構相同 — SystemContextBuilder 用單一 role
        承載完整 base + handoff section,而 pure function 直接 append handoff
        到 base 後;只要 base + handoff bytes 完全一致,hash 必然一致。
        """
        base_role = "Orchestrator role."

        # Pure function 路徑
        pure_prompt = prompt_with_handoff_instructions(base_role, three_handoffs)
        pure_hash = compute_prefix_hash([{"role": "system", "content": pure_prompt}])

        # Builder 路徑 — base role 後直接 add handoff section 也以 add_role 形式塞進去,
        # 兩段 role 之間用 ``_SECTION_SEPARATOR``("\n\n")串接,與 pure function 完全一致。
        sys_builder = SystemContextBuilder()
        sys_builder.add_role(base_role)
        HandoffInstructionsBuilder().add_handoffs(three_handoffs).to_system_context(
            sys_builder
        )
        builder_prompt = sys_builder.build()
        builder_hash = compute_prefix_hash(
            [{"role": "system", "content": builder_prompt}]
        )

        assert pure_prompt == builder_prompt
        assert pure_hash == builder_hash
