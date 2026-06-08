"""Skill loader — 把 markdown + YAML frontmatter 載成可掛進 agent runtime 的 ``Skill``。

本模組對應 enhancement roadmap §4.3 P2-11,目的是讓使用者可以用一份
**markdown 檔**描述一個 agent skill,內容包含:

* YAML frontmatter:metadata(name / description)+ tool 列表 + policy 規則
  + skill-specific parameters。
* markdown body:skill 的 prompt template(LLM 看的指令)。

格式範例(節錄,完整檔請見 ``examples/file_summarizer.md``):

.. code-block:: markdown

    ---
    name: file_summarizer
    description: 摘要檔案內容
    tools: [read_file, count_tokens]
    policy:
      - permission: "read_file(**)"
        effect: allow
    parameters:
      max_summary_chars: 500
    ---

    # File Summarizer
    You are a file summarizer. Given a file path...

設計重點
========

* **解析路徑**:`SkillLoader.load_from_file` → :class:`Skill`;``policy`` 透過
  P1-16 :func:`policy_rule_from_yaml_item` 直接吃 ``permission`` sugar 與
  P0-7 舊格式並存,**不重寫一份 parser**,避免兩條程式碼漂移。
* **註冊**::class:`SkillRegistry` 在 in-memory 維護 ``name → Skill`` map,
  並提供 ``apply_skill(skill, tool_registry, policy_engine)`` 把 skill 的
  tool / policy 一次掛進對應 registry / engine。
* **prompt 整合**:``to_system_context(builder, active_skills)`` 把 active
  skill 的 prompt template + tool description 餵進 P1-10
  :class:`SystemContextBuilder`,跟既有 prompt cache 流程合流。
* **錯誤分類**::class:`SkillFrontmatterError` 是 ``ValueError`` 子型別,
  涵蓋「frontmatter 缺失 / 結構錯」;``policy:`` 的字串語法錯誤透傳
  :class:`PermissionRuleSyntaxError`(也是 ``ValueError`` 子型別)。

本檔只依賴 std lib + `pyyaml`(既有 dep)+ P0-2 / P0-7 / P1-10 / P1-16
模組,不引入新 pyproject 條目。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml  # type: ignore[import-untyped]

from anila_agent.core.permission_grammar import (
    PermissionRuleSyntaxError,
    policy_rule_from_yaml_item,
)
from anila_agent.core.policy import PolicyEngine, PolicyRule

if TYPE_CHECKING:
    from anila_agent.prompts.prompt_builder import SystemContextBuilder
    from anila_agent.tools.registry import ToolRegistry


# ---------------------------------------------------------------------------
# 例外
# ---------------------------------------------------------------------------


class SkillFrontmatterError(ValueError):
    """Skill frontmatter 結構錯誤(缺 ``name`` / type 不對 / yaml 解析失敗)。

    繼承 ``ValueError``,讓上層可以一次 broad-except 接 frontmatter 與
    permission rule 兩類錯誤;也可用 ``isinstance(e, SkillFrontmatterError)``
    區分。

    Attributes:
        source: 觸發錯誤的 skill 檔路徑或來源描述(無 path 時是字串 ``"<inline>"``)。
        reason: 人類可讀錯誤理由。
    """

    def __init__(self, source: str, reason: str) -> None:
        self.source = source
        self.reason = reason
        super().__init__(f"invalid skill frontmatter in {source!r}: {reason}")


# ---------------------------------------------------------------------------
# 常數 — frontmatter 切分
# ---------------------------------------------------------------------------

# YAML frontmatter delimiter — 對齊 Jekyll / Hugo / claude-code-src 慣例,
# 三個 dash 起頭、三個 dash 結尾,中間是 yaml。
_FRONTMATTER_OPEN_RE = re.compile(r"^---\s*\r?\n")
_FRONTMATTER_CLOSE_RE = re.compile(r"\r?\n---\s*(?:\r?\n|$)")


# ---------------------------------------------------------------------------
# Skill dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Skill:
    """一個 skill 載入後的不可變表示。

    Attributes:
        name: skill 唯一名稱(``[A-Za-z_][A-Za-z0-9_-]*``)。registry key。
        description: 一行說明,給 LLM 看的 anchor。
        tools: skill 期望啟用的 tool name list;``apply_skill`` 時透過
            tool_registry 解析。
        policy_rules: 從 frontmatter ``policy:`` 段轉成的 :class:`PolicyRule`
            列表;``apply_skill`` 時逐條 ``add_rule`` 進 :class:`PolicyEngine`。
        parameters: skill-specific 參數(任意 yaml-serializable 值),例如
            ``{"max_summary_chars": 500}``。runtime 由 skill 對應的 prompt
            template / tool 自行取用,registry 層不解讀。
        prompt_template: markdown body(去除 frontmatter 後的純文字),
            ``to_system_context`` 會塞給 :class:`SystemContextBuilder`。
        source_path: 載入來源檔路徑;由 :meth:`SkillLoader.load_from_file`
            填入,``inline`` 載入則為 ``None``。
    """

    name: str
    description: str
    tools: tuple[str, ...] = ()
    policy_rules: tuple[PolicyRule, ...] = ()
    parameters: dict[str, Any] = field(default_factory=dict)
    prompt_template: str = ""
    source_path: Path | None = None


# ---------------------------------------------------------------------------
# SkillLoader — parse markdown + frontmatter
# ---------------------------------------------------------------------------

# valid skill name 字元集 — 對齊 claude-code-src slash command 命名規則。
_SKILL_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")


@dataclass
class SkillLoader:
    """無狀態的 skill 載入器。

    刻意做成 dataclass 而非純函式集合,日後若要支援 cache / hot reload 可
    在這層加 attribute(例如 ``cache: dict[Path, Skill]``),呼叫端 API
    不變。

    Attributes:
        strict_unknown_keys: ``True`` 時 frontmatter 出現未知 top-level key
            會 raise(預設 ``False``,只 silently 忽略不認得的 key,對齊
            claude-code-src 的寬鬆策略)。
    """

    strict_unknown_keys: bool = False

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------

    def load_from_file(self, path: str | Path) -> Skill:
        """從單一 ``.md`` 檔載入 :class:`Skill`。

        Args:
            path: skill markdown 檔路徑。

        Returns:
            載入後的 :class:`Skill`(``source_path`` 設為解析後的絕對路徑)。

        Raises:
            FileNotFoundError: 檔案不存在。
            SkillFrontmatterError: frontmatter 缺失 / 結構錯 / yaml 解析失敗。
            PermissionRuleSyntaxError: ``policy:`` sugar 字串語法錯。
        """
        file_path = Path(path)
        if not file_path.exists():
            raise FileNotFoundError(f"skill file not found: {file_path}")
        raw = file_path.read_text(encoding="utf-8")
        return self._parse_text(raw, source=file_path)

    def load_from_text(self, text: str, *, source: str = "<inline>") -> Skill:
        """從字串直接載入(主要供測試 / inline skill 使用)。

        Args:
            text: 完整 markdown(含 frontmatter)。
            source: 來源描述,只用在錯誤訊息(不影響行為)。

        Returns:
            :class:`Skill`(``source_path`` 為 ``None``)。
        """
        return self._parse_text(text, source=source)

    def load_from_dir(
        self,
        directory: str | Path,
        *,
        glob: str = "*.md",
        recursive: bool = False,
    ) -> list[Skill]:
        """從目錄掃描所有匹配 glob 的 markdown 並載成 :class:`Skill` 列表。

        Args:
            directory: 目錄路徑。
            glob: 檔名 glob,預設 ``"*.md"``。
            recursive: ``True`` 時改用 ``rglob`` 遞迴掃描。

        Returns:
            載入順序為 ``sorted(path)``(deterministic);名稱重複時**不**自動
            去重(load 階段保持中立),由呼叫端決定;若同一目錄含重複名稱會在
            ``SkillRegistry.register`` 階段 raise。

        Raises:
            FileNotFoundError: 目錄不存在 / 不是 dir。
            SkillFrontmatterError: 任一檔解析失敗(訊息含對應檔名)。
        """
        dir_path = Path(directory)
        if not dir_path.exists() or not dir_path.is_dir():
            raise FileNotFoundError(f"skill directory not found: {dir_path}")
        finder = dir_path.rglob if recursive else dir_path.glob
        files = sorted(finder(glob))
        return [self.load_from_file(f) for f in files]

    # ------------------------------------------------------------------
    # 內部 — 解析
    # ------------------------------------------------------------------

    def _parse_text(self, text: str, *, source: str | Path) -> Skill:
        """把整份 markdown text 切成 frontmatter + body,再組裝 :class:`Skill`。"""
        source_label = str(source)
        frontmatter_text, body = _split_frontmatter(text, source=source_label)

        try:
            data = yaml.safe_load(frontmatter_text) or {}
        except yaml.YAMLError as e:
            raise SkillFrontmatterError(
                source_label, f"yaml parse error: {e}"
            ) from e

        if not isinstance(data, dict):
            raise SkillFrontmatterError(
                source_label,
                f"frontmatter must be a mapping, got {type(data).__name__}",
            )

        if self.strict_unknown_keys:
            unknown = set(data.keys()) - _KNOWN_TOP_KEYS
            if unknown:
                raise SkillFrontmatterError(
                    source_label,
                    f"unknown frontmatter keys: {sorted(unknown)}",
                )

        name = _require_string(data, "name", source_label)
        if not _SKILL_NAME_RE.match(name):
            raise SkillFrontmatterError(
                source_label,
                f"invalid 'name' {name!r}; must match {_SKILL_NAME_RE.pattern!r}",
            )

        description = _require_string(data, "description", source_label)

        tools = _parse_tools(data.get("tools"), source=source_label)
        policy_rules = _parse_policy(data.get("policy"), source=source_label)
        parameters = _parse_parameters(data.get("parameters"), source=source_label)

        source_path = source if isinstance(source, Path) else None
        return Skill(
            name=name,
            description=description,
            tools=tools,
            policy_rules=policy_rules,
            parameters=parameters,
            prompt_template=body,
            source_path=source_path,
        )


# top-level frontmatter keys we recognise — 用於 strict mode 檢查。
_KNOWN_TOP_KEYS: frozenset[str] = frozenset(
    {"name", "description", "tools", "policy", "parameters"}
)


# ---------------------------------------------------------------------------
# SkillRegistry — name → Skill + apply 整合
# ---------------------------------------------------------------------------


@dataclass
class SkillRegistry:
    """skill 註冊表 + 套用 helper。

    Attributes:
        skills: name → :class:`Skill` 映射。
    """

    skills: dict[str, Skill] = field(default_factory=dict)

    # ------------------------------------------------------------------
    # 註冊 / 查詢
    # ------------------------------------------------------------------

    def register(self, skill: Skill) -> None:
        """註冊 skill。重複名稱直接 raise(對齊 ToolRegistry 行為)。

        Raises:
            ValueError: 同名 skill 已註冊。
        """
        if skill.name in self.skills:
            raise ValueError(f"duplicate skill name: {skill.name!r}")
        self.skills[skill.name] = skill

    def register_many(self, skills: Iterable[Skill]) -> None:
        """批次註冊;任一失敗整批中止(不做 partial commit)。"""
        for s in skills:
            self.register(s)

    def get(self, name: str) -> Skill:
        """取 skill;不存在 raise ``KeyError``。"""
        if name not in self.skills:
            raise KeyError(f"skill not in registry: {name!r}")
        return self.skills[name]

    def list(self) -> list[Skill]:
        """回傳目前所有 skill,依 name 排序(deterministic)。"""
        return [self.skills[k] for k in sorted(self.skills.keys())]

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and name in self.skills

    def __len__(self) -> int:
        return len(self.skills)

    # ------------------------------------------------------------------
    # apply — 把 skill 的 tool / policy 掛進對應 registry / engine
    # ------------------------------------------------------------------

    def apply_skill(
        self,
        skill: Skill | str,
        *,
        tool_registry: ToolRegistry | None = None,
        policy_engine: PolicyEngine | None = None,
        activate_deferred: bool = False,
        session: Any = None,
    ) -> AppliedSkill:
        """把 skill 套用到 runtime — 解析 tool list、灌 policy rule。

        Args:
            skill: 已註冊的 :class:`Skill` 或其 name 字串。
            tool_registry: 要解析 ``skill.tools`` 用的 :class:`ToolRegistry`;
                ``None`` 代表略過 tool resolution(只回 name list)。
            policy_engine: 要灌入 policy rule 的 :class:`PolicyEngine`;
                ``None`` 代表略過 policy 套用。
            activate_deferred: ``True`` + 提供 ``tool_registry`` + ``session``
                時,對 skill.tools 中標為 deferred 的 tool 自動呼叫
                ``tool_registry.activate(name, session=session)``。
            session: 配合 ``activate_deferred`` 使用的 SessionContext。

        Returns:
            :class:`AppliedSkill`,記錄這次 apply 解析到的 tool / 套用的 rule
            數,方便 caller log / assert。

        Raises:
            KeyError: skill name 不在 registry;或 tool name 不在 tool_registry。
        """
        resolved_skill = skill if isinstance(skill, Skill) else self.get(skill)

        resolved_tools: list[Any] = []
        if tool_registry is not None:
            for tname in resolved_skill.tools:
                if tname not in tool_registry.tools:
                    raise KeyError(
                        f"skill {resolved_skill.name!r} references "
                        f"unknown tool {tname!r}"
                    )
                tool_obj = tool_registry.tools[tname]
                resolved_tools.append(tool_obj)
                if (
                    activate_deferred
                    and session is not None
                    and tool_registry.is_deferred(tname)
                ):
                    tool_registry.activate(tname, session=session)

        applied_rules: list[PolicyRule] = []
        if policy_engine is not None:
            for rule in resolved_skill.policy_rules:
                policy_engine.add_rule(rule)
                applied_rules.append(rule)

        return AppliedSkill(
            skill=resolved_skill,
            resolved_tools=tuple(resolved_tools),
            applied_rules=tuple(applied_rules),
        )

    # ------------------------------------------------------------------
    # prompt integration — P1-10 SystemContextBuilder
    # ------------------------------------------------------------------

    def to_system_context(
        self,
        builder: SystemContextBuilder,
        active_skills: Sequence[str | Skill] | None = None,
        *,
        tool_registry: ToolRegistry | None = None,
    ) -> SystemContextBuilder:
        """把 active skill 的 prompt template + tool description 灌進 builder。

        每個 active skill 會貢獻:

        1. ``builder.add_role(skill.prompt_template)`` —— 把 markdown body
           當作一段 role / instruction 串進 system prompt。空 body 跳過。
        2. 若提供 ``tool_registry``,從 registry 撈出該 skill 列名的 tool,
           轉成 :class:`ToolDescriptor` 加進 builder(由 builder 自行去重 +
           sort,確保 prompt cache 命中)。

        Args:
            builder: 要灌資料進去的 :class:`SystemContextBuilder`。
            active_skills: 要套用的 skill list;``None`` 代表全部已註冊
                skill(依 name 排序)。可混傳 name 字串與 :class:`Skill` 實例。
            tool_registry: 用於把 tool name 解析成 description 的
                :class:`ToolRegistry`;``None`` 代表不附 tool description。

        Returns:
            ``builder``(支援鏈式呼叫)。

        Raises:
            KeyError: ``active_skills`` 內某 name 不在 registry。
        """
        from anila_agent.prompts.prompt_builder import ToolDescriptor
        from anila_agent.tools.base import get_metadata  # 延後 import,避免 cycle

        if active_skills is None:
            target = self.list()
        else:
            target = [s if isinstance(s, Skill) else self.get(s) for s in active_skills]

        for skill in target:
            body = skill.prompt_template.strip()
            if body:
                builder.add_role(body)
            if tool_registry is None:
                continue
            for tname in skill.tools:
                if tname not in tool_registry.tools:
                    # 已被 apply_skill 驗證過的場景就不會走這條;但
                    # to_system_context 也可獨立呼叫,所以保守略過 + 不 raise,
                    # 跟 SystemContextBuilder 的 silent dedupe 風格一致。
                    continue
                tool_obj = tool_registry.tools[tname]
                description = _tool_description(tool_obj)
                builder.add_tool_description(
                    [ToolDescriptor(name=tname, description=description)]
                )
                # 觸發 get_metadata 一次,確保 tool 至少有有效 metadata
                # (不取值,只當 sanity check)。
                get_metadata(tool_obj)

        return builder


@dataclass(frozen=True)
class AppliedSkill:
    """``apply_skill`` 的回傳值,記錄解析結果。

    Attributes:
        skill: 來源 :class:`Skill`。
        resolved_tools: 從 tool_registry 解到的實際 tool 物件(可能為空)。
        applied_rules: 加進 policy_engine 的 :class:`PolicyRule` 列表。
    """

    skill: Skill
    resolved_tools: tuple[Any, ...]
    applied_rules: tuple[PolicyRule, ...]


# ---------------------------------------------------------------------------
# 內部 helper — frontmatter / yaml 解析
# ---------------------------------------------------------------------------


def _split_frontmatter(text: str, *, source: str) -> tuple[str, str]:
    """切出 yaml frontmatter 字串與 markdown body。

    Returns:
        ``(frontmatter_yaml, body_markdown)``,body 已 ``lstrip``。

    Raises:
        SkillFrontmatterError: 頭沒有 ``---`` 或找不到收尾 ``---``。
    """
    open_match = _FRONTMATTER_OPEN_RE.match(text)
    if not open_match:
        raise SkillFrontmatterError(
            source,
            "missing YAML frontmatter; file must start with '---' on first line",
        )
    after_open = text[open_match.end() :]
    close_match = _FRONTMATTER_CLOSE_RE.search(after_open)
    if not close_match:
        raise SkillFrontmatterError(
            source, "missing closing '---' for frontmatter"
        )
    frontmatter = after_open[: close_match.start()]
    body = after_open[close_match.end() :]
    return frontmatter, body.lstrip("\n\r")


def _require_string(data: dict[str, Any], key: str, source: str) -> str:
    """從 frontmatter dict 取出必填 string 欄位。"""
    if key not in data:
        raise SkillFrontmatterError(source, f"missing required key {key!r}")
    value = data[key]
    if not isinstance(value, str) or not value.strip():
        raise SkillFrontmatterError(
            source, f"{key!r} must be a non-empty string"
        )
    return value.strip()


def _parse_tools(raw: Any, *, source: str) -> tuple[str, ...]:
    """解析 ``tools:`` 段 — 接受 list[str] 或省略;其它 type raise。"""
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise SkillFrontmatterError(
            source,
            f"'tools' must be a list of tool names, got {type(raw).__name__}",
        )
    out: list[str] = []
    for idx, item in enumerate(raw):
        if not isinstance(item, str) or not item.strip():
            raise SkillFrontmatterError(
                source,
                f"'tools[{idx}]' must be a non-empty string, got {item!r}",
            )
        out.append(item.strip())
    # 去重但保序(skill 作者可能會無心重複,我們不要因此 raise)。
    seen: set[str] = set()
    deduped: list[str] = []
    for t in out:
        if t in seen:
            continue
        seen.add(t)
        deduped.append(t)
    return tuple(deduped)


def _parse_policy(raw: Any, *, source: str) -> tuple[PolicyRule, ...]:
    """解析 ``policy:`` 段 — 走 P1-16 ``policy_rule_from_yaml_item``。

    支援:

    * list[dict] — 每個 dict 是一條 rule,格式跟 ``settings.yaml`` 內
      ``rules:`` 完全一樣(``permission`` sugar 或 ``tool_pattern`` 舊格式)。
    * 省略 / ``None`` / 空 list → 回空 tuple。

    其它 type 一律 raise。
    """
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise SkillFrontmatterError(
            source,
            f"'policy' must be a list of rule mappings, "
            f"got {type(raw).__name__}",
        )
    out: list[PolicyRule] = []
    for idx, item in enumerate(raw):
        if not isinstance(item, dict):
            raise SkillFrontmatterError(
                source,
                f"'policy[{idx}]' must be a mapping, got {type(item).__name__}",
            )
        try:
            rule = policy_rule_from_yaml_item(item)
        except PermissionRuleSyntaxError:
            # 透傳 — 上層想抓 syntax 錯也可走 broad ValueError。
            raise
        except ValueError as e:
            raise SkillFrontmatterError(
                source, f"policy[{idx}]: {e}"
            ) from e
        out.append(rule)
    return tuple(out)


def _parse_parameters(raw: Any, *, source: str) -> dict[str, Any]:
    """解析 ``parameters:`` 段 — 必須是 mapping,允許任意 yaml-serializable 值。"""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise SkillFrontmatterError(
            source,
            f"'parameters' must be a mapping, got {type(raw).__name__}",
        )
    # 不對 value 做型別檢查 — 由 skill 對應的 tool / prompt 自行解讀。
    return dict(raw)


def _tool_description(tool: Any) -> str:
    """從 FunctionTool 取出 description,fallback 為空字串。

    openai-agents FunctionTool 有 ``description`` attribute;但 P1-10
    ``ToolDescriptor`` 把 description 當必填 str,所以這邊保證不會回 None。
    """
    desc = getattr(tool, "description", None)
    if isinstance(desc, str):
        return desc
    return ""


__all__ = [
    "AppliedSkill",
    "Skill",
    "SkillFrontmatterError",
    "SkillLoader",
    "SkillRegistry",
]
