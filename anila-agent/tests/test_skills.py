"""P2-11 Skill loader / SkillRegistry 的 unit + integration test。

涵蓋:

* :class:`SkillLoader` 解析 frontmatter(欄位齊全 / 缺欄位 / 型別錯)。
* ``policy`` 段透過 P1-16 ``policy_rule_from_yaml_item`` 正確展開。
* ``parameters`` 段保留任意 yaml 值。
* :meth:`SkillLoader.load_from_dir` 全 example skill 都能載入。
* :class:`SkillRegistry` 註冊 / 重複 / get / list。
* :meth:`SkillRegistry.apply_skill` 同時掛 tool_registry + policy_engine。
* P1-10 integration:``to_system_context`` 把 skill body + tool 灌進
  :class:`SystemContextBuilder`,build 出 byte-identical 結果。
* edge:不存在的 tool / 不存在的 skill / 缺 frontmatter / yaml syntax error。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from anila_agent.core.permission_grammar import PermissionRuleSyntaxError
from anila_agent.core.policy import PolicyEffect, PolicyEngine
from anila_agent.prompts import SystemContextBuilder
from anila_agent.skills import (
    AppliedSkill,
    Skill,
    SkillFrontmatterError,
    SkillLoader,
    SkillRegistry,
)
from anila_agent.tools.base import anila_tool
from anila_agent.tools.registry import ToolRegistry

# ---------------------------------------------------------------------------
# fixtures — tools / loader
# ---------------------------------------------------------------------------


@pytest.fixture
def example_skills_dir() -> Path:
    """指向 repo 內建的 example skills 目錄。"""
    return Path(__file__).resolve().parent.parent / "anila_agent" / "skills" / "examples"


@pytest.fixture
def loader() -> SkillLoader:
    return SkillLoader()


@pytest.fixture
def tool_registry_with_examples() -> ToolRegistry:
    """構造包含所有 example skill 會用到的 tool 的 registry。"""

    @anila_tool(is_read_only=True, category="filesystem")
    def read_file(path: str) -> str:
        """讀取單一檔案內容。"""
        return f"<content of {path}>"

    @anila_tool(is_read_only=True, category="utility")
    def count_tokens(text: str) -> int:
        """估算 token 數。"""
        return len(text.split())

    @anila_tool(is_read_only=True, category="filesystem")
    def list_files(path: str) -> list[str]:
        """列目錄。"""
        return []

    @anila_tool(is_read_only=True, category="retrieval")
    def search_code(query: str) -> list[str]:
        """搜尋程式碼片段。"""
        return []

    @anila_tool(category="filesystem")
    def write_file(path: str, content: str) -> str:
        """寫檔。"""
        return "ok"

    @anila_tool(is_read_only=True, category="visualization")
    def draw_graph(nodes: list[str]) -> str:
        """繪製 DAG。"""
        return "graph"

    registry = ToolRegistry()
    registry.add_many(
        [
            read_file,
            count_tokens,
            list_files,
            search_code,
            write_file,
            draw_graph,
        ]
    )
    return registry


# ---------------------------------------------------------------------------
# 基本 parse 行為
# ---------------------------------------------------------------------------


def test_load_minimal_frontmatter(loader: SkillLoader) -> None:
    """只給最低欄位(name / description)也要能載入。"""
    text = (
        "---\n"
        "name: minimal\n"
        "description: a minimal skill\n"
        "---\n"
        "body text\n"
    )
    skill = loader.load_from_text(text)
    assert skill.name == "minimal"
    assert skill.description == "a minimal skill"
    assert skill.tools == ()
    assert skill.policy_rules == ()
    assert skill.parameters == {}
    assert "body text" in skill.prompt_template
    assert skill.source_path is None


def test_load_full_frontmatter(loader: SkillLoader) -> None:
    """完整 frontmatter — tools / policy / parameters 全到。"""
    text = (
        "---\n"
        "name: full_skill\n"
        "description: full feature skill\n"
        "tools: [read_file, count_tokens]\n"
        "policy:\n"
        '  - permission: "Read(**)"\n'
        "    effect: allow\n"
        "    priority: 600\n"
        "parameters:\n"
        "  max_chars: 500\n"
        "  flag: true\n"
        "---\n"
        "# Full Skill\nprompt content\n"
    )
    skill = loader.load_from_text(text)
    assert skill.name == "full_skill"
    assert skill.tools == ("read_file", "count_tokens")
    assert len(skill.policy_rules) == 1
    rule = skill.policy_rules[0]
    assert rule.effect is PolicyEffect.ALLOW
    assert rule.priority == 600
    assert skill.parameters == {"max_chars": 500, "flag": True}
    assert skill.prompt_template.startswith("# Full Skill")


def test_load_from_file_records_source_path(
    loader: SkillLoader, tmp_path: Path
) -> None:
    """``load_from_file`` 一定要設 ``source_path``。"""
    f = tmp_path / "demo.md"
    f.write_text(
        "---\nname: demo\ndescription: from file\n---\nbody\n", encoding="utf-8"
    )
    skill = loader.load_from_file(f)
    assert skill.source_path == f
    assert skill.name == "demo"


def test_load_dir_sorted_and_recurses(
    loader: SkillLoader, tmp_path: Path
) -> None:
    """``load_from_dir`` 依檔名 sort,並能遞迴。"""
    (tmp_path / "b.md").write_text(
        "---\nname: b_skill\ndescription: b\n---\nb\n", encoding="utf-8"
    )
    (tmp_path / "a.md").write_text(
        "---\nname: a_skill\ndescription: a\n---\na\n", encoding="utf-8"
    )
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "c.md").write_text(
        "---\nname: c_skill\ndescription: c\n---\nc\n", encoding="utf-8"
    )

    flat = loader.load_from_dir(tmp_path)
    assert [s.name for s in flat] == ["a_skill", "b_skill"]

    recursive = loader.load_from_dir(tmp_path, recursive=True)
    assert [s.name for s in recursive] == ["a_skill", "b_skill", "c_skill"]


def test_tools_dedup_preserve_order(loader: SkillLoader) -> None:
    """``tools`` 內若重複,保序去重而非 raise。"""
    text = (
        "---\nname: dup_tools\n"
        "description: with duplicates\n"
        "tools: [a, b, a, c, b]\n"
        "---\n"
    )
    skill = loader.load_from_text(text)
    assert skill.tools == ("a", "b", "c")


# ---------------------------------------------------------------------------
# 錯誤路徑
# ---------------------------------------------------------------------------


def test_missing_frontmatter_raises(loader: SkillLoader) -> None:
    with pytest.raises(SkillFrontmatterError, match="missing YAML frontmatter"):
        loader.load_from_text("no frontmatter here\n")


def test_unclosed_frontmatter_raises(loader: SkillLoader) -> None:
    with pytest.raises(SkillFrontmatterError, match="missing closing '---'"):
        loader.load_from_text("---\nname: x\ndescription: y\nno close\n")


def test_missing_name_raises(loader: SkillLoader) -> None:
    text = "---\ndescription: desc only\n---\nbody\n"
    with pytest.raises(SkillFrontmatterError, match="'name'"):
        loader.load_from_text(text)


def test_missing_description_raises(loader: SkillLoader) -> None:
    text = "---\nname: only_name\n---\nbody\n"
    with pytest.raises(SkillFrontmatterError, match="'description'"):
        loader.load_from_text(text)


def test_invalid_name_raises(loader: SkillLoader) -> None:
    text = "---\nname: \"123 bad\"\ndescription: d\n---\nbody\n"
    with pytest.raises(SkillFrontmatterError, match="invalid 'name'"):
        loader.load_from_text(text)


def test_tools_must_be_list(loader: SkillLoader) -> None:
    text = (
        "---\nname: bad_tools\n"
        "description: d\ntools: not_a_list\n"
        "---\n"
    )
    with pytest.raises(SkillFrontmatterError, match="'tools' must be a list"):
        loader.load_from_text(text)


def test_tools_item_must_be_string(loader: SkillLoader) -> None:
    text = "---\nname: bad_tool_item\ndescription: d\ntools: [123]\n---\n"
    with pytest.raises(SkillFrontmatterError, match="tools\\[0\\]"):
        loader.load_from_text(text)


def test_policy_must_be_list(loader: SkillLoader) -> None:
    text = (
        "---\nname: bad_policy\n"
        "description: d\npolicy: not_a_list\n"
        "---\n"
    )
    with pytest.raises(SkillFrontmatterError, match="'policy' must be a list"):
        loader.load_from_text(text)


def test_policy_bad_permission_syntax(loader: SkillLoader) -> None:
    """``policy:`` 內 permission 字串語法錯 — 透傳 PermissionRuleSyntaxError。"""
    text = (
        "---\nname: bad_perm\n"
        "description: d\n"
        "policy:\n"
        '  - permission: "Read(unclosed"\n'
        "---\n"
    )
    with pytest.raises(PermissionRuleSyntaxError):
        loader.load_from_text(text)


def test_parameters_must_be_mapping(loader: SkillLoader) -> None:
    text = (
        "---\nname: bad_params\n"
        "description: d\nparameters: [not, a, mapping]\n"
        "---\n"
    )
    with pytest.raises(SkillFrontmatterError, match="'parameters' must be a mapping"):
        loader.load_from_text(text)


def test_yaml_parse_error(loader: SkillLoader) -> None:
    text = "---\nname: bad: : yaml\n---\nbody\n"
    with pytest.raises(SkillFrontmatterError, match="yaml parse error"):
        loader.load_from_text(text)


def test_strict_unknown_keys() -> None:
    strict_loader = SkillLoader(strict_unknown_keys=True)
    text = (
        "---\nname: x\ndescription: d\nfunky_key: val\n---\n"
    )
    with pytest.raises(SkillFrontmatterError, match="unknown frontmatter keys"):
        strict_loader.load_from_text(text)


def test_load_from_file_not_found(loader: SkillLoader, tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        loader.load_from_file(tmp_path / "missing.md")


def test_load_from_dir_not_found(loader: SkillLoader, tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        loader.load_from_dir(tmp_path / "nope")


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_registry_register_and_get(loader: SkillLoader) -> None:
    reg = SkillRegistry()
    skill = loader.load_from_text(
        "---\nname: r1\ndescription: d\n---\nbody\n"
    )
    reg.register(skill)
    assert "r1" in reg
    assert len(reg) == 1
    assert reg.get("r1") is skill


def test_registry_duplicate_raises(loader: SkillLoader) -> None:
    reg = SkillRegistry()
    s1 = loader.load_from_text("---\nname: dup\ndescription: a\n---\n")
    s2 = loader.load_from_text("---\nname: dup\ndescription: b\n---\n")
    reg.register(s1)
    with pytest.raises(ValueError, match="duplicate skill name"):
        reg.register(s2)


def test_registry_get_missing_raises() -> None:
    reg = SkillRegistry()
    with pytest.raises(KeyError):
        reg.get("nope")


def test_registry_list_sorted(loader: SkillLoader) -> None:
    reg = SkillRegistry()
    reg.register_many(
        [
            loader.load_from_text("---\nname: b\ndescription: skill b\n---\n"),
            loader.load_from_text("---\nname: a\ndescription: skill a\n---\n"),
            loader.load_from_text("---\nname: c\ndescription: skill c\n---\n"),
        ]
    )
    assert [s.name for s in reg.list()] == ["a", "b", "c"]


# ---------------------------------------------------------------------------
# apply_skill
# ---------------------------------------------------------------------------


def test_apply_skill_attaches_tools_and_rules(
    loader: SkillLoader, tool_registry_with_examples: ToolRegistry
) -> None:
    text = (
        "---\nname: applyer\n"
        "description: d\n"
        "tools: [read_file, count_tokens]\n"
        "policy:\n"
        '  - permission: "Read(**)"\n'
        "    effect: allow\n"
        '  - permission: "Write(**)"\n'
        "    effect: deny\n"
        "---\n"
    )
    skill = loader.load_from_text(text)
    reg = SkillRegistry()
    reg.register(skill)

    engine = PolicyEngine()
    result = reg.apply_skill(
        "applyer",
        tool_registry=tool_registry_with_examples,
        policy_engine=engine,
    )
    assert isinstance(result, AppliedSkill)
    assert len(result.resolved_tools) == 2
    assert {t.name for t in result.resolved_tools} == {"read_file", "count_tokens"}
    assert len(result.applied_rules) == 2
    # engine 應該真的有兩條 rule
    assert len(engine.rules) == 2


def test_apply_skill_unknown_tool_raises(
    loader: SkillLoader, tool_registry_with_examples: ToolRegistry
) -> None:
    skill = loader.load_from_text(
        "---\nname: bad_ref\ndescription: d\ntools: [does_not_exist]\n---\n"
    )
    reg = SkillRegistry()
    reg.register(skill)
    with pytest.raises(KeyError, match="unknown tool"):
        reg.apply_skill(
            "bad_ref", tool_registry=tool_registry_with_examples
        )


def test_apply_skill_without_engines(loader: SkillLoader) -> None:
    """tool_registry / policy_engine 都不給時 — 不該炸,回 empty resolved。"""
    skill = loader.load_from_text(
        "---\nname: standalone\n"
        "description: d\n"
        "tools: [x]\n"
        "policy:\n"
        '  - permission: "Read(**)"\n'
        "    effect: allow\n"
        "---\n"
    )
    reg = SkillRegistry()
    reg.register(skill)
    result = reg.apply_skill("standalone")
    assert result.resolved_tools == ()
    assert result.applied_rules == ()


def test_apply_skill_accepts_skill_instance(loader: SkillLoader) -> None:
    skill = loader.load_from_text(
        "---\nname: by_instance\ndescription: d\n---\n"
    )
    reg = SkillRegistry()  # 故意不註冊也行
    result = reg.apply_skill(skill)
    assert result.skill is skill


# ---------------------------------------------------------------------------
# example skills(repo 內建)— 整段端對端驗證
# ---------------------------------------------------------------------------


def test_examples_dir_loads_all(
    loader: SkillLoader, example_skills_dir: Path
) -> None:
    """``anila_agent/skills/examples/`` 全部 3 個 skill 都要能載入。"""
    skills = loader.load_from_dir(example_skills_dir)
    names = {s.name for s in skills}
    assert names == {"file_summarizer", "code_reviewer", "task_planner"}
    for s in skills:
        assert s.description
        assert s.tools  # 三個 example 都有列 tool
        assert s.policy_rules  # 三個 example 都有 policy
        assert s.prompt_template.strip()


def test_examples_registry_apply_end_to_end(
    loader: SkillLoader,
    example_skills_dir: Path,
    tool_registry_with_examples: ToolRegistry,
) -> None:
    """三個 example 都能 register + apply 進真 engine,policy 確實生效。"""
    skills = loader.load_from_dir(example_skills_dir)
    reg = SkillRegistry()
    reg.register_many(skills)
    engine = PolicyEngine()
    for s in skills:
        reg.apply_skill(
            s.name,
            tool_registry=tool_registry_with_examples,
            policy_engine=engine,
        )
    # 至少 3 條 rule 進 engine(三個 skill 各有多條)
    assert len(engine.rules) >= 6

    # file_summarizer 應 deny write_file
    decision = engine.evaluate(None, "write_file", {"path": "/tmp/x"})
    assert decision.effect is PolicyEffect.DENY


# ---------------------------------------------------------------------------
# P1-10 integration — to_system_context
# ---------------------------------------------------------------------------


def test_to_system_context_includes_body_and_tools(
    loader: SkillLoader,
    tool_registry_with_examples: ToolRegistry,
) -> None:
    """``to_system_context`` 應把 skill body 灌成 role + 把 tool 拉進來。"""
    skill = loader.load_from_text(
        "---\nname: ctx_test\n"
        "description: d\n"
        "tools: [read_file]\n"
        "---\n"
        "ROLE_MARKER you are a ctx tester\n"
    )
    reg = SkillRegistry()
    reg.register(skill)

    builder = SystemContextBuilder()
    reg.to_system_context(
        builder,
        active_skills=["ctx_test"],
        tool_registry=tool_registry_with_examples,
    )
    output = builder.build()
    assert "ROLE_MARKER" in output
    assert "read_file" in output
    # tool 描述應出現於 Available tools 段
    assert "## Available tools" in output


def test_to_system_context_deterministic(
    loader: SkillLoader,
    tool_registry_with_examples: ToolRegistry,
) -> None:
    """同 registry / 同 active_skills 兩次 build 必須 byte-identical。"""
    skills = [
        loader.load_from_text(
            "---\nname: alpha\ndescription: a\ntools: [read_file]\n---\nalpha body\n"
        ),
        loader.load_from_text(
            "---\nname: bravo\ndescription: b\ntools: [count_tokens]\n---\nbravo body\n"
        ),
    ]
    reg = SkillRegistry()
    reg.register_many(skills)

    def render() -> str:
        b = SystemContextBuilder()
        reg.to_system_context(
            b,
            active_skills=["alpha", "bravo"],
            tool_registry=tool_registry_with_examples,
        )
        return b.build()

    assert render() == render()


def test_to_system_context_default_all_skills(loader: SkillLoader) -> None:
    """``active_skills=None`` → 套用全部已註冊 skill,依 name 排序。"""
    reg = SkillRegistry()
    reg.register(
        loader.load_from_text(
            "---\nname: z_one\ndescription: d\n---\nZ_BODY\n"
        )
    )
    reg.register(
        loader.load_from_text(
            "---\nname: a_one\ndescription: d\n---\nA_BODY\n"
        )
    )
    builder = SystemContextBuilder()
    reg.to_system_context(builder)
    out = builder.build()
    # 兩個 body 都應出現,而且 a_ 在 z_ 前(builder.roles 依插入序)
    assert "A_BODY" in out
    assert "Z_BODY" in out
    assert out.index("A_BODY") < out.index("Z_BODY")


def test_to_system_context_unknown_skill_raises(loader: SkillLoader) -> None:
    reg = SkillRegistry()
    builder = SystemContextBuilder()
    with pytest.raises(KeyError):
        reg.to_system_context(builder, active_skills=["nope"])


def test_to_system_context_skips_unknown_tools(loader: SkillLoader) -> None:
    """若 skill.tools 引用的 tool 不在 registry,``to_system_context`` 應靜默略過。

    (``apply_skill`` 才會 strict raise;``to_system_context`` 採 silent dedupe
    風格,避免 prompt 階段被 tool dependency 卡死。)
    """
    skill = loader.load_from_text(
        "---\nname: leniant\n"
        "description: d\ntools: [ghost_tool]\n"
        "---\nbody\n"
    )
    reg = SkillRegistry()
    reg.register(skill)
    tool_reg = ToolRegistry()
    builder = SystemContextBuilder()
    # 不該 raise
    reg.to_system_context(
        builder,
        active_skills=["leniant"],
        tool_registry=tool_reg,
    )
    out = builder.build()
    assert "body" in out
    # tool section 因為沒任何 tool 解析成功,不會出現
    assert "## Available tools" not in out


# ---------------------------------------------------------------------------
# Skill dataclass — 不可變保證
# ---------------------------------------------------------------------------


def test_skill_is_frozen(loader: SkillLoader) -> None:
    skill = loader.load_from_text(
        "---\nname: frozen_test\ndescription: d\n---\nbody\n"
    )
    import dataclasses

    assert isinstance(skill, Skill)
    with pytest.raises(dataclasses.FrozenInstanceError):
        skill.name = "mutated"  # type: ignore[misc]
