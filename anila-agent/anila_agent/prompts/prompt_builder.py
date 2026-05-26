"""Prompt builder — P1-10 systemContext / userContext 兩段式 prompt 結構。

本模組對應 enhancement roadmap §4.2 P1-10,主要參考 claude-code-src 的:

* ``src/context.ts``(``getSystemContext`` / ``getUserContext``)— memoized 兩段
  靜態 / 動態 context dict。
* ``src/utils/api.ts``(``appendSystemContext`` / ``prependUserContext``)— 把
  context 接到 system prompt 尾 / 第一個 user message 前。

# 為什麼要拆 systemContext / userContext

ANILA 跑 vLLM gemma4,vLLM ``--enable-prefix-caching`` 只在 prefix bytes
**完全相同**時才命中 KV cache(無 explicit ``cache_control`` API)。若把整段
prompt 當一段 markdown 灌進去,只要 currentDate 或 git status 變動,整段就
invalidate,prompt cache 命中率非常差。

claude-code 的設計:

* **systemContext**:**穩定不變的 instruction**(system prompt、tool description、
  agent role、guideline、safety)— 在 prompt 最前面,bytes 一樣 → vLLM cache
  整段命中。
* **userContext**:**每次 turn 變動的內容**(user input、conversation history、
  recent memory recall、attachments)— 後面,每 turn 變化但前綴不變。

# 與 P1-1 ``prompt_cache.compute_prefix_hash`` 的對齊

:class:`AnilaPromptBuilder.build_messages` 第一條一定是 ``role=system``,
且 :class:`SystemContextBuilder` 內部對所有 fragment(尤其是 tool list)做
**deterministic sort**,確保:

1. 同 builder state → 同 system message content → 同 bytes → 同
   :func:`compute_prefix_hash` 結果。
2. user 對話變動只會影響 ``messages[1:]``,system message bytes 不變 →
   vLLM 前綴 cache 仍命中。

# 模組與既有 ``prompts/system.md`` 的關係

:meth:`SystemContextBuilder.from_markdown` 提供 helper 讀既有 markdown 當
``role`` 內容載入,不破壞既有 prompts 結構。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from anila_agent.core.prompt_cache import Message

# ---------------------------------------------------------------------------
# 常數 — 對齊 claude-code-src ``utils/api.ts`` 的 boilerplate
# ---------------------------------------------------------------------------

# system prompt 內各 section 的分隔符。固定 ``"\n\n"`` 與 markdown 慣例對齊,
# 且 bytes deterministic,不受 platform 影響。
_SECTION_SEPARATOR: str = "\n\n"

# tool 描述區塊的 header 與每個 tool 條目的格式。固定字串保證 bytes 穩定。
_TOOLS_SECTION_HEADER: str = "## Available tools"

# guideline / safety section header,給 LLM 看的 markdown 風格 anchor。
_GUIDELINE_SECTION_HEADER: str = "## Guidelines"
_SAFETY_SECTION_HEADER: str = "## Safety"

# userContext system-reminder 包裝 — 對齊 ``prependUserContext`` 的 boilerplate
# 與 turn-by-turn 註記區隔,並讓 LLM 知道這段非 user 直接輸入。
_USER_CONTEXT_OPEN: str = "<system-reminder>"
_USER_CONTEXT_CLOSE: str = "</system-reminder>"
_USER_CONTEXT_INTRO: str = (
    "As you answer the user's questions, you can use the following context:"
)
_USER_CONTEXT_OUTRO: str = (
    "IMPORTANT: this context may or may not be relevant to your tasks. "
    "You should not respond to this context unless it is highly relevant to your task."
)

# memory recall / attachments / history section header — 給 LLM 結構提示。
_MEMORY_SECTION_HEADER: str = "# Recent memory"
_HISTORY_SECTION_HEADER: str = "# Conversation history"
_ATTACHMENT_SECTION_HEADER: str = "# Attachments"


# ---------------------------------------------------------------------------
# 型別 alias — ToolDescriptor 是 builder 給 caller 的最小 schema
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolDescriptor:
    """tool description 在 prompt 內的最小 schema。

    刻意不直接依賴 ``anila_agent.tools.base.ToolMetadata`` — 避免 prompts 子
    模組往下耦合 openai-agents / FunctionTool。caller 可以從任何 tool registry
    撈出 (name, description) 後造 :class:`ToolDescriptor`。

    Attributes:
        name: tool 名(在 prompt 上會被當 anchor)。
        description: tool 用途說明(LLM 看的自然語言)。
        signature: 可選的 signature 字串(例如 ``"search_documents(query: str, k: int = 5)"``)。
    """

    name: str
    description: str
    signature: str | None = None


# ---------------------------------------------------------------------------
# SystemContextBuilder — 穩定不變的 instruction(role + tools + guideline +
# safety),可被 vLLM 整段 cache。
# ---------------------------------------------------------------------------


@dataclass
class SystemContextBuilder:
    """累積 system 段內容(role / tools / guideline / safety),deterministic build。

    使用方式:

        builder = SystemContextBuilder()
        builder.add_role("You are an ANILA RAG assistant.")
        builder.add_tool_description(tools)
        builder.add_guideline("Always cite document IDs.")
        builder.add_safety("Refuse PII extraction requests.")
        system_prompt = builder.build()

    **deterministic 保證**:

    * 同樣的 fragment(尤其是同 tool list,不管插入順序如何)會產生 byte-identical
      output。tool list 在 build 時按 ``name`` 排序。
    * role / guideline / safety 依**加入順序**呈現(同一段 section 內);
      section 間順序固定:role → tools → guideline → safety。

    Attributes:
        roles: 透過 :meth:`add_role` 加入的角色字串。
        tools: 透過 :meth:`add_tool_description` 加入的 ToolDescriptor 清單。
        guidelines: 透過 :meth:`add_guideline` 加入的操作指引。
        safety_rules: 透過 :meth:`add_safety` 加入的安全守則。
    """

    roles: list[str] = field(default_factory=list)
    tools: list[ToolDescriptor] = field(default_factory=list)
    guidelines: list[str] = field(default_factory=list)
    safety_rules: list[str] = field(default_factory=list)

    # ------------------------------------------------------------------
    # mutating accumulators
    # ------------------------------------------------------------------

    def add_role(self, text: str) -> SystemContextBuilder:
        """加入 agent 身份 / 角色描述。可多次呼叫,以加入順序串接。"""
        if not text or not text.strip():
            raise ValueError("role text must be a non-empty string")
        self.roles.append(text.strip())
        return self

    def add_tool_description(
        self, tools: Iterable[ToolDescriptor | Mapping[str, Any]]
    ) -> SystemContextBuilder:
        """加入一組 tool description。

        接受 :class:`ToolDescriptor` 或 dict(``{name, description, signature?}``)。
        重複加入同名 tool 會在 :meth:`build` 時去重(以最後一次加入的 description
        為準),仍保證最終 prompt 按 name 排序 deterministic。

        Args:
            tools: tool descriptor 的 iterable。

        Returns:
            ``self``,支援鏈式呼叫。
        """
        for tool in tools:
            descriptor = _coerce_tool_descriptor(tool)
            self.tools.append(descriptor)
        return self

    def add_guideline(self, text: str) -> SystemContextBuilder:
        """加入一條操作指引(以 markdown bullet 呈現)。"""
        if not text or not text.strip():
            raise ValueError("guideline text must be a non-empty string")
        self.guidelines.append(text.strip())
        return self

    def add_safety(self, text: str) -> SystemContextBuilder:
        """加入一條 safety / guardrail 規則。"""
        if not text or not text.strip():
            raise ValueError("safety text must be a non-empty string")
        self.safety_rules.append(text.strip())
        return self

    # ------------------------------------------------------------------
    # 從既有 markdown 載入 — 不破壞 prompts/system.md 結構
    # ------------------------------------------------------------------

    @classmethod
    def from_markdown(
        cls, path: str | Path, *, encoding: str = "utf-8"
    ) -> SystemContextBuilder:
        """以既有 markdown 檔(例如 ``prompts/system.md``)當 base role 載入。

        檔案整段內容會被當作一條 role 字串塞進 :attr:`roles`,呼叫端可以再
        ``add_tool_description`` / ``add_guideline`` 等堆疊更多 section。

        Args:
            path: markdown 檔案路徑。
            encoding: 檔案編碼(預設 UTF-8)。

        Returns:
            新的 :class:`SystemContextBuilder` 實例。

        Raises:
            FileNotFoundError: 若檔案不存在。
            ValueError: 若檔案內容為空白。
        """
        text = Path(path).read_text(encoding=encoding)
        if not text.strip():
            raise ValueError(f"markdown file is empty: {path}")
        builder = cls()
        builder.add_role(text)
        return builder

    # ------------------------------------------------------------------
    # build — deterministic 序列化
    # ------------------------------------------------------------------

    def build(self) -> str:
        """組成完整 system prompt 字串。

        section 順序固定:role → tools → guideline → safety;tool 按 name 排序
        確保 deterministic。同一份 builder state 多次 build 必然 byte-identical。

        Returns:
            完整 system prompt 字串(可能為空字串,若 builder 完全沒被填)。
        """
        sections: list[str] = []

        if self.roles:
            sections.append(_SECTION_SEPARATOR.join(self.roles))

        if self.tools:
            sections.append(self._render_tools())

        if self.guidelines:
            sections.append(self._render_bulleted(_GUIDELINE_SECTION_HEADER, self.guidelines))

        if self.safety_rules:
            sections.append(
                self._render_bulleted(_SAFETY_SECTION_HEADER, self.safety_rules)
            )

        return _SECTION_SEPARATOR.join(sections)

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _render_tools(self) -> str:
        """以 deterministic 順序(name asc)渲染 tool description section。

        若同名 tool 被加入多次,以最後一次出現的 description / signature 為準
        (de-dup by name)。
        """
        deduped: dict[str, ToolDescriptor] = {}
        for descriptor in self.tools:
            deduped[descriptor.name] = descriptor

        lines: list[str] = [_TOOLS_SECTION_HEADER]
        for name in sorted(deduped.keys()):
            descriptor = deduped[name]
            signature_part = f" — `{descriptor.signature}`" if descriptor.signature else ""
            lines.append(f"- **{descriptor.name}**{signature_part}: {descriptor.description}")
        return "\n".join(lines)

    @staticmethod
    def _render_bulleted(header: str, items: Sequence[str]) -> str:
        """以 markdown bullet list 渲染一段 section。"""
        lines = [header]
        for item in items:
            lines.append(f"- {item}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# UserContextBuilder — 每 turn 變動的內容(history / memory / input / attachments)
# ---------------------------------------------------------------------------


@dataclass
class UserContextBuilder:
    """累積 user 段內容,build 後展開為 message list。

    使用方式:

        builder = UserContextBuilder()
        builder.add_history(prior_messages)
        builder.add_memory(memdir_recall_docs)
        builder.add_attachments(uploaded_files)
        builder.add_input("How do I configure the retriever?")
        user_messages = builder.build()

    build 出來的是 ``list[Message]``,可直接接到 :meth:`AnilaPromptBuilder.build_messages`
    的 system message 後面。順序固定為:**history → memory + attachments(as
    system-reminder)→ user input**,確保前段(history)bytes 在後續 turn 加 input
    時仍 byte-identical。

    Attributes:
        history: 之前的對話訊息(role/content dict)。
        memory_items: memdir 召回的 item list(原始 dict)。
        attachments: 附件描述 list(file path / description dict)。
        input_text: 當前 user 輸入(必填,否則 build 會 raise)。
    """

    history: list[Message] = field(default_factory=list)
    memory_items: list[Mapping[str, Any]] = field(default_factory=list)
    attachments: list[Mapping[str, Any]] = field(default_factory=list)
    input_text: str | None = None

    # ------------------------------------------------------------------
    # mutating accumulators
    # ------------------------------------------------------------------

    def add_history(self, messages: Iterable[Message]) -> UserContextBuilder:
        """加入過去對話 message。message 形狀對齊 OpenAI / Anthropic Messages API。"""
        for msg in messages:
            if not isinstance(msg, Mapping) or "role" not in msg:
                raise ValueError(
                    "history message must be a Mapping with 'role' key, "
                    f"got {type(msg).__name__}: {msg!r}"
                )
            self.history.append(dict(msg))
        return self

    def add_memory(self, memdir_recall: Iterable[Mapping[str, Any]]) -> UserContextBuilder:
        """加入 memdir 召回的 memory item(每個都該至少有 ``text`` 或 ``content``)。"""
        for item in memdir_recall:
            if not isinstance(item, Mapping):
                raise ValueError(
                    f"memory item must be a Mapping, got {type(item).__name__}"
                )
            self.memory_items.append(dict(item))
        return self

    def add_input(self, user_input: str) -> UserContextBuilder:
        """設定當前 turn 的 user 輸入。重複呼叫會覆蓋。"""
        if not user_input or not user_input.strip():
            raise ValueError("user_input must be a non-empty string")
        self.input_text = user_input
        return self

    def add_attachments(self, files: Iterable[Mapping[str, Any]]) -> UserContextBuilder:
        """加入附件描述(``{path, description?, mime?}``)。"""
        for entry in files:
            if not isinstance(entry, Mapping):
                raise ValueError(
                    f"attachment entry must be a Mapping, got {type(entry).__name__}"
                )
            self.attachments.append(dict(entry))
        return self

    # ------------------------------------------------------------------
    # build — 展開為 message list
    # ------------------------------------------------------------------

    def build(self) -> list[Message]:
        """組成 user-side 的 message list。

        順序:

        1. 既有 ``history`` 原樣展開(role/content 不動)。
        2. 若有 memory / attachments,組一個 system-reminder boilerplate user
           message(以 ``isMeta`` 風格標註),對齊 ``prependUserContext`` 的 TS
           實作。
        3. 最後一條為 ``role=user``,content 為 :attr:`input_text`。

        Returns:
            可直接附加到 ``[system_message] + user_messages`` 後面送 LLM 的
            message list。

        Raises:
            ValueError: 若 :attr:`input_text` 未設定。
        """
        if not self.input_text:
            raise ValueError(
                "UserContextBuilder.build requires add_input() to be called first"
            )

        messages: list[Message] = list(self.history)

        reminder = self._render_context_reminder()
        if reminder:
            messages.append({"role": "user", "content": reminder})

        messages.append({"role": "user", "content": self.input_text})
        return messages

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _render_context_reminder(self) -> str | None:
        """組 memory + attachments 的 system-reminder boilerplate;兩者皆空回 None。"""
        if not self.memory_items and not self.attachments:
            return None

        sections: list[str] = [_USER_CONTEXT_INTRO]

        if self.memory_items:
            sections.append(_MEMORY_SECTION_HEADER)
            for item in self.memory_items:
                sections.append(f"- {_render_memory_item(item)}")

        if self.attachments:
            sections.append(_ATTACHMENT_SECTION_HEADER)
            for entry in self.attachments:
                sections.append(f"- {_render_attachment(entry)}")

        sections.append(_USER_CONTEXT_OUTRO)

        body = "\n".join(sections)
        return f"{_USER_CONTEXT_OPEN}\n{body}\n{_USER_CONTEXT_CLOSE}"


# ---------------------------------------------------------------------------
# AnilaPromptBuilder — 組合 system + user builder,產生最終 message list
# ---------------------------------------------------------------------------


@dataclass
class AnilaPromptBuilder:
    """組合 :class:`SystemContextBuilder` 與 :class:`UserContextBuilder`。

    使用方式:

        prompt = AnilaPromptBuilder(system_builder, user_builder)
        messages = prompt.build_messages()
        # messages[0] 一定是 role=system,且 bytes deterministic

    與 :func:`anila_agent.core.prompt_cache.compute_prefix_hash` 直接相容 —
    把 ``messages[:1]`` 餵進去算 hash,兩次同 system_builder 必然回同 hash。
    """

    system_builder: SystemContextBuilder
    user_builder: UserContextBuilder

    def build_messages(self) -> list[Message]:
        """組成最終送 LLM 的 message list。

        嚴格保證:

        1. ``messages[0]["role"] == "system"``。
        2. ``messages[0]["content"]`` 來自 :meth:`SystemContextBuilder.build`,
           同 state 多次呼叫 bytes 相同(deterministic)。
        3. 後續 messages 為 ``UserContextBuilder.build`` 的輸出,原樣展開。

        Returns:
            ``[system_message, *user_messages]``。
        """
        system_content = self.system_builder.build()
        system_message: Message = {"role": "system", "content": system_content}
        user_messages = self.user_builder.build()
        return [system_message, *user_messages]

    def build_system_messages(self) -> list[Message]:
        """只回傳 system 段(``[system_message]``),方便對接 prompt_cache。

        典型用法:

            from anila_agent.core.prompt_cache import compute_prefix_hash
            hash1 = compute_prefix_hash(prompt.build_system_messages())
            hash2 = compute_prefix_hash(prompt.build_system_messages())
            assert hash1 == hash2

        Returns:
            單元素 list,內含 ``role=system`` message。
        """
        system_content = self.system_builder.build()
        return [{"role": "system", "content": system_content}]


# ---------------------------------------------------------------------------
# 內部 helper
# ---------------------------------------------------------------------------


def _coerce_tool_descriptor(
    tool: ToolDescriptor | Mapping[str, Any],
) -> ToolDescriptor:
    """把 ToolDescriptor / dict 統一成 ToolDescriptor。"""
    if isinstance(tool, ToolDescriptor):
        return tool
    if isinstance(tool, Mapping):
        try:
            name = tool["name"]
            description = tool["description"]
        except KeyError as exc:
            raise ValueError(
                f"tool dict must contain 'name' and 'description', got {dict(tool)!r}"
            ) from exc
        signature = tool.get("signature")
        return ToolDescriptor(
            name=str(name),
            description=str(description),
            signature=None if signature is None else str(signature),
        )
    raise TypeError(
        f"tool must be ToolDescriptor or Mapping, got {type(tool).__name__}"
    )


def _render_memory_item(item: Mapping[str, Any]) -> str:
    """把 memory recall item 渲染成單行(取 ``text`` / ``content`` / 整 dict 序列化)。"""
    if "text" in item:
        return str(item["text"]).strip()
    if "content" in item:
        return str(item["content"]).strip()
    return str(item)


def _render_attachment(entry: Mapping[str, Any]) -> str:
    """把 attachment entry 渲染成單行(``path`` 為主,desc 附加在後)。"""
    path = entry.get("path") or entry.get("name") or "<unknown>"
    description = entry.get("description")
    if description:
        return f"{path} — {description}"
    return str(path)


# ---------------------------------------------------------------------------
# 公開 API
# ---------------------------------------------------------------------------

__all__ = [
    "AnilaPromptBuilder",
    "SystemContextBuilder",
    "ToolDescriptor",
    "UserContextBuilder",
]
