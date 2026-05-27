"""P2-3 — ``prompt_with_handoff_instructions``:把 handoff agent 列表自動塞進 system prompt。

對應 openai-agents 上游 :mod:`agents.extensions.handoff_prompt`(原始 19 行版本)的 ANILA
等價物。上游版本只是靜態 ``RECOMMENDED_PROMPT_PREFIX`` 接在 prompt 前面、不列實際 handoff
名稱;本模組進一步從 :class:`anila_agent.core.agent_tool.AgentTool` 清單抽出 ``name`` /
``description``,動態 render 成 bullet list 接到 base prompt 後面,讓 LLM 真正看得到「現在
有哪幾個 sub-agent 可以呼叫」。

# 為什麼要做這層

parent agent 的 ``instructions`` 通常只寫角色與任務,**不會列子 agent 名單**。LLM 不知道
身邊有 ``retriever`` / ``writer`` 這幾個 tool 名,自然不會主動派工。``prompt_with_handoff_instructions``
把這層 visibility 自動補上 — 同樣 handoff 列表 → 同樣 instruction bytes → 同樣
:func:`anila_agent.core.prompt_cache.compute_prefix_hash`,確保 vLLM prefix cache 仍然命中。

# 兩種 API style

* **pure function** :func:`prompt_with_handoff_instructions` — 給快速 inline 用,接
  ``(base_prompt, handoffs, language=...)`` 三參數,回新 prompt 字串。對齊上游 SDK 介面。
* **builder** :class:`HandoffInstructionsBuilder` — 給 P1-10 :class:`SystemContextBuilder`
  整合用,可累積 :class:`AgentTool` 後一次 :meth:`build`,或 :meth:`to_system_context`
  把結果以 ``add_role`` 形式塞回 :class:`SystemContextBuilder`,不破壞既有 builder 介面。

# Deterministic 保證

* handoff 清單一律按 ``agent_tool.name`` 排序後 render,**插入順序不影響 output bytes**。
* 同 builder state 多次 build → byte-identical。
* 與 :func:`compute_prefix_hash` 對齊:同 handoff list → 同 instructions → 同 hash。

# i18n

提供 ``language="zh-TW"`` (預設) 與 ``language="en"`` 兩語版。zh-TW 採台灣用語、繁體中文;
en 對齊上游 SDK 風格但具體列名單。Constant ``HANDOFF_SECTION_HEADER_*`` 為各語系 section
header,可在外部測試 assert。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Literal, TYPE_CHECKING

from anila_agent.core.agent_tool import AgentTool

if TYPE_CHECKING:
    from anila_agent.prompts.prompt_builder import SystemContextBuilder

# ---------------------------------------------------------------------------
# 型別 alias
# ---------------------------------------------------------------------------

# 支援語言代碼。zh-TW = 繁體中文(台灣用語);en = 英文。
HandoffLanguage = Literal["en", "zh-TW"]

# 預設語言。ANILA 主要部署環境為台灣,預設 zh-TW。
DEFAULT_HANDOFF_INSTRUCTIONS_LANGUAGE: HandoffLanguage = "zh-TW"


# ---------------------------------------------------------------------------
# 常數 — 各語系 section header / intro / outro
# ---------------------------------------------------------------------------

# section header — markdown H2,供 LLM 認出區塊邊界。bytes 固定 → deterministic。
HANDOFF_SECTION_HEADER_EN: str = "## Available agents (handoff)"
HANDOFF_SECTION_HEADER_ZH: str = "## 可派工的 sub-agent 清單"

# section intro 一句 — 告訴 LLM 這段在講什麼。bytes 固定 → deterministic。
_HANDOFF_INTRO_EN: str = (
    "You have access to the following agents you can delegate to. "
    "When a task fits an agent's specialty, call it by name with a focused prompt."
)
_HANDOFF_INTRO_ZH: str = (
    "你可以委派任務給下列 agents。當某段任務契合某 agent 的專長時,"
    "請以該 agent 名稱呼叫,並附上聚焦的 prompt。"
)

# section outro — 提示 LLM 不需向使用者揭露派工過程。
_HANDOFF_OUTRO_EN: str = (
    "Transfers between agents are handled in the background; "
    "do not mention or draw attention to them in your reply."
)
_HANDOFF_OUTRO_ZH: str = (
    "派工流程在背景處理,不需在回覆中向使用者特別提及或強調這些切換。"
)

# section 內 bullet 之間的分隔 — 固定 "\n",與 markdown 慣例對齊。
_LINE_SEPARATOR: str = "\n"

# 整段 section 與 base prompt 之間的分隔 — 雙換行,對齊 prompt_builder 慣例。
_SECTION_SEPARATOR: str = "\n\n"


# ---------------------------------------------------------------------------
# 內部 helper — 把 AgentTool 清單 render 成 deterministic bullet 字串
# ---------------------------------------------------------------------------


def _render_handoff_section(
    handoffs: Iterable[AgentTool],
    *,
    language: HandoffLanguage,
) -> str:
    """把 AgentTool 清單 render 成單一段 markdown section 字串。

    流程:
    1. 依 ``agent_tool.name`` 排序 → deterministic ordering。
    2. 同名去重(以最後一個出現的為準),避免重複出現。
    3. 組 header + intro + bullet list + outro,以 ``"\\n"`` 接合。

    Args:
        handoffs: AgentTool 清單(順序不限,內部會 sort)。
        language: ``"zh-TW"`` 或 ``"en"``,決定 header / intro / outro 用字。

    Returns:
        deterministic 渲染出的 markdown section 字串(無外圍 padding,單純 section 內容)。
        若 ``handoffs`` 為空,回空字串(caller 負責決定要不要 append)。
    """
    # 1. de-dup by name(同名以最後一個為準),再 sort by name。
    deduped: dict[str, AgentTool] = {}
    for spec in handoffs:
        deduped[spec.name] = spec

    if not deduped:
        return ""

    if language == "zh-TW":
        header = HANDOFF_SECTION_HEADER_ZH
        intro = _HANDOFF_INTRO_ZH
        outro = _HANDOFF_OUTRO_ZH
    else:
        header = HANDOFF_SECTION_HEADER_EN
        intro = _HANDOFF_INTRO_EN
        outro = _HANDOFF_OUTRO_EN

    lines: list[str] = [header, intro]
    for name in sorted(deduped.keys()):
        spec = deduped[name]
        # 為避免 description 內含 newline 破壞 markdown 結構,replace 為單一空格。
        description = spec.description.replace("\n", " ").strip()
        lines.append(f"- **{name}**: {description}")
    lines.append(outro)
    return _LINE_SEPARATOR.join(lines)


# ---------------------------------------------------------------------------
# Pure function — 對齊上游 SDK ``prompt_with_handoff_instructions``
# ---------------------------------------------------------------------------


def prompt_with_handoff_instructions(
    base_prompt: str,
    handoffs: Iterable[AgentTool],
    *,
    language: HandoffLanguage = DEFAULT_HANDOFF_INSTRUCTIONS_LANGUAGE,
) -> str:
    """把 handoff agent 清單 append 到 base prompt 之後。

    上游 SDK :func:`agents.extensions.handoff_prompt.prompt_with_handoff_instructions`
    只把 ``RECOMMENDED_PROMPT_PREFIX`` 接在前面、**不列實際 handoff 名單**。本實作改為:

    1. 列出每個 :class:`AgentTool` 的 ``name`` / ``description``(bullet list)。
    2. handoffs 按 ``name`` 排序,確保 byte-deterministic。
    3. 預設語言為 ``"zh-TW"``,可切換為 ``"en"``。

    Args:
        base_prompt: 原本的 system prompt 字串(通常是 agent.instructions)。
        handoffs: :class:`AgentTool` 清單(可空)。空 → 直接回 ``base_prompt``。
        language: instructions 語言;預設 ``"zh-TW"``。

    Returns:
        新 prompt 字串:``"{base_prompt}\\n\\n{handoff_section}"``;若 handoffs 為空則
        原樣回 ``base_prompt`` 不加分隔符。
    """
    section = _render_handoff_section(handoffs, language=language)
    if not section:
        return base_prompt
    return f"{base_prompt}{_SECTION_SEPARATOR}{section}"


# ---------------------------------------------------------------------------
# Builder — 對齊 P1-10 SystemContextBuilder pattern
# ---------------------------------------------------------------------------


@dataclass
class HandoffInstructionsBuilder:
    """累積 handoff :class:`AgentTool` 後 build 成 instruction section 字串。

    使用方式:

        builder = HandoffInstructionsBuilder()
        builder.add_handoff(retriever_tool)
        builder.add_handoffs([writer_tool, summarizer_tool])
        section = builder.build()
        # section 已 deterministic 排序,可 append 到任何 prompt 後面

    或直接與 :class:`SystemContextBuilder` (P1-10) 整合:

        sys_builder = SystemContextBuilder().add_role("You are an orchestrator.")
        HandoffInstructionsBuilder().add_handoffs([retriever, writer]).to_system_context(sys_builder)
        prompt = sys_builder.build()

    deterministic 保證:

    * 同 builder state 多次 build → byte-identical 字串。
    * handoff 按 ``name`` 排序、同名去重(以最後一個 add 的為準)。
    * 與 P1-1 :func:`compute_prefix_hash` 對齊:同 handoff list + 同 base prompt
      → 同 system content → 同 hash。

    Attributes:
        handoffs: 透過 :meth:`add_handoff` / :meth:`add_handoffs` 累積的 AgentTool 清單。
        language: instructions 語言;預設 :data:`DEFAULT_HANDOFF_INSTRUCTIONS_LANGUAGE`。
    """

    handoffs: list[AgentTool] = field(default_factory=list)
    language: HandoffLanguage = DEFAULT_HANDOFF_INSTRUCTIONS_LANGUAGE

    # ------------------------------------------------------------------
    # mutating accumulators
    # ------------------------------------------------------------------

    def add_handoff(self, agent_tool: AgentTool) -> HandoffInstructionsBuilder:
        """加入單一 :class:`AgentTool`。可重複呼叫;同名以最後一次為準。

        Args:
            agent_tool: P0-8 :class:`AgentTool` spec(通常由 :func:`make_agent_tool`
                或 P2-1 :func:`as_tool` 產出)。

        Returns:
            ``self``,支援鏈式呼叫。

        Raises:
            TypeError: 若 ``agent_tool`` 非 :class:`AgentTool` 實例。
        """
        if not isinstance(agent_tool, AgentTool):
            raise TypeError(
                f"agent_tool must be AgentTool, got {type(agent_tool).__name__}"
            )
        self.handoffs.append(agent_tool)
        return self

    def add_handoffs(
        self, agent_tools: Iterable[AgentTool]
    ) -> HandoffInstructionsBuilder:
        """加入多個 :class:`AgentTool`。順序不重要,build 時會 sort by name。"""
        for spec in agent_tools:
            self.add_handoff(spec)
        return self

    # ------------------------------------------------------------------
    # build — deterministic 序列化
    # ------------------------------------------------------------------

    def build(self) -> str:
        """組成 handoff instruction section 字串。

        Returns:
            單一段 markdown section(header + intro + bullets + outro)。
            若沒加任何 handoff,回空字串。
        """
        return _render_handoff_section(self.handoffs, language=self.language)

    # ------------------------------------------------------------------
    # SystemContextBuilder 整合 — 把 section 以 add_role 形式塞回 builder
    # ------------------------------------------------------------------

    def to_system_context(
        self, builder: "SystemContextBuilder"
    ) -> "SystemContextBuilder":
        """把 handoff section 透過 :meth:`SystemContextBuilder.add_role` 塞進 P1-10 builder。

        以 ``add_role`` 而非另開 section,有兩個好處:

        1. **不破壞 P1-10 既有介面** — :class:`SystemContextBuilder` 完全不必改動。
        2. **deterministic 仍然成立** — ``add_role`` 依加入順序串接,handoff 內部
           bullets 已 sort by name,所以最終 bytes 仍然 deterministic。

        Args:
            builder: P1-10 :class:`SystemContextBuilder` 實例。

        Returns:
            傳入的 ``builder``(支援鏈式呼叫)。若沒有 handoff 加入過,builder
            不會被動到,直接 noop 回 builder。
        """
        section = self.build()
        if section:
            builder.add_role(section)
        return builder


# ---------------------------------------------------------------------------
# 公開 API
# ---------------------------------------------------------------------------

__all__ = [
    "DEFAULT_HANDOFF_INSTRUCTIONS_LANGUAGE",
    "HANDOFF_SECTION_HEADER_EN",
    "HANDOFF_SECTION_HEADER_ZH",
    "HandoffInstructionsBuilder",
    "HandoffLanguage",
    "prompt_with_handoff_instructions",
]
