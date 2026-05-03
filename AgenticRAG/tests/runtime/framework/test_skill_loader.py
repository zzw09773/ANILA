"""Sprint 7 tests — skill loader + SkillRegistry."""

from __future__ import annotations

import pytest

from agentic_rag.runtime.framework import (
    Action,
    ActionContext,
    ActionKind,
    Skill,
    SkillRegistry,
    load_skills_from_dir,
    parse_skill_file,
)
from agentic_rag.runtime.framework.exceptions import UserError


def _ctx(params=None) -> ActionContext:
    return ActionContext(
        run_id="r", agent_name="a", params=params or {}, history=()
    )


# ── parse_skill_file ─────────────────────────────────────────────────


def test_parse_skill_basic_shape() -> None:
    text = (
        "---\n"
        "name: my_skill\n"
        "description: Does a thing.\n"
        "when_to_use: when the user asks about things\n"
        "---\n"
        "Do the thing.\n"
    )
    skill = parse_skill_file(text)
    assert skill.name == "my_skill"
    assert skill.description == "Does a thing."
    assert skill.when_to_use == "when the user asks about things"
    assert skill.body == "Do the thing."
    assert isinstance(skill.action, Action)
    assert skill.action.name == "my_skill"
    assert skill.action.kind is ActionKind.SYNC_TOOL


def test_parse_skill_with_input_schema() -> None:
    text = (
        "---\n"
        "name: summarise_pr\n"
        "description: Summarise a PR.\n"
        "input_schema:\n"
        "  type: object\n"
        "  properties:\n"
        "    url: {type: string}\n"
        "  required: [url]\n"
        "---\n"
        "Summarise the PR at {{ url }}.\n"
    )
    skill = parse_skill_file(text)
    assert skill.input_schema["properties"]["url"]["type"] == "string"
    assert skill.input_schema["required"] == ["url"]
    assert skill.action.input_schema == skill.input_schema


def test_parse_skill_rejects_missing_frontmatter() -> None:
    with pytest.raises(UserError, match="missing YAML frontmatter"):
        parse_skill_file("just body, no frontmatter")


def test_parse_skill_rejects_invalid_yaml() -> None:
    text = (
        "---\n"
        "name: x\n"
        "description: [unclosed\n"
        "---\n"
        "body\n"
    )
    with pytest.raises(UserError, match="frontmatter YAML invalid"):
        parse_skill_file(text)


def test_parse_skill_rejects_missing_name() -> None:
    text = (
        "---\n"
        "description: no name here\n"
        "---\n"
        "body\n"
    )
    with pytest.raises(UserError, match="frontmatter.name is required"):
        parse_skill_file(text)


def test_parse_skill_rejects_empty_body() -> None:
    text = (
        "---\n"
        "name: empty_skill\n"
        "---\n"
    )
    with pytest.raises(UserError, match="body is empty"):
        parse_skill_file(text)


def test_parse_skill_rejects_non_dict_input_schema() -> None:
    text = (
        "---\n"
        "name: bad_schema\n"
        "input_schema: 'not a dict'\n"
        "---\n"
        "body\n"
    )
    with pytest.raises(UserError, match="input_schema must be a YAML mapping"):
        parse_skill_file(text)


def test_parse_skill_records_source_path() -> None:
    text = "---\nname: x\n---\nbody\n"
    skill = parse_skill_file(text, source_path="/skills/x.md")
    assert skill.source_path == "/skills/x.md"


# ── Skill action handler ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_skill_action_returns_rendered_body() -> None:
    text = (
        "---\n"
        "name: greet\n"
        "input_schema:\n"
        "  type: object\n"
        "  properties: {name: {type: string}}\n"
        "  required: [name]\n"
        "---\n"
        "Hello, {{ name }}! Welcome.\n"
    )
    skill = parse_skill_file(text)
    result = await skill.action.handler(_ctx({"name": "Alice"}))
    assert not result.is_error
    assert result.output["skill_name"] == "greet"
    assert result.output["skill_body"] == "Hello, Alice! Welcome."


@pytest.mark.asyncio
async def test_skill_action_renders_missing_var_as_placeholder() -> None:
    text = (
        "---\nname: t\n---\nValue: {{ missing }} done.\n"
    )
    skill = parse_skill_file(text)
    result = await skill.action.handler(_ctx({}))
    assert result.output["skill_body"] == "Value: [missing] done."


@pytest.mark.asyncio
async def test_skill_action_substitutes_multiple_vars() -> None:
    text = "---\nname: t\n---\n{{ a }} + {{ b }} = {{ c }}\n"
    skill = parse_skill_file(text)
    result = await skill.action.handler(_ctx({"a": 1, "b": 2, "c": 3}))
    assert result.output["skill_body"] == "1 + 2 = 3"


# ── load_skills_from_dir ─────────────────────────────────────────────


def test_load_skills_from_dir_returns_empty_for_missing_dir(tmp_path) -> None:
    nonexistent = tmp_path / "nope"
    assert load_skills_from_dir(nonexistent) == []


def test_load_skills_from_dir_loads_multiple(tmp_path) -> None:
    (tmp_path / "alpha.md").write_text(
        "---\nname: alpha\ndescription: A.\n---\nA body\n", encoding="utf-8"
    )
    (tmp_path / "beta.md").write_text(
        "---\nname: beta\ndescription: B.\n---\nB body\n", encoding="utf-8"
    )
    skills = load_skills_from_dir(tmp_path)
    names = sorted(s.name for s in skills)
    assert names == ["alpha", "beta"]


def test_load_skills_from_dir_recurses_subdirs(tmp_path) -> None:
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "nested.md").write_text(
        "---\nname: nested\n---\nbody\n", encoding="utf-8"
    )
    skills = load_skills_from_dir(tmp_path)
    assert any(s.name == "nested" for s in skills)


def test_load_skills_from_dir_skips_invalid_by_default(tmp_path, caplog) -> None:
    (tmp_path / "good.md").write_text(
        "---\nname: good\n---\nbody\n", encoding="utf-8"
    )
    (tmp_path / "bad.md").write_text("no frontmatter at all", encoding="utf-8")
    skills = load_skills_from_dir(tmp_path)
    assert [s.name for s in skills] == ["good"]


def test_load_skills_from_dir_raises_on_invalid_when_strict(tmp_path) -> None:
    (tmp_path / "bad.md").write_text("no frontmatter", encoding="utf-8")
    with pytest.raises(UserError):
        load_skills_from_dir(tmp_path, skip_invalid=False)


# ── SkillRegistry ────────────────────────────────────────────────────


def _skill(name: str, when_to_use: str = "", description: str = "") -> Skill:
    text = (
        f"---\nname: {name}\ndescription: {description}\nwhen_to_use: {when_to_use}\n"
        f"---\nbody for {name}\n"
    )
    return parse_skill_file(text)


def test_registry_add_and_lookup() -> None:
    reg = SkillRegistry([_skill("a"), _skill("b")])
    assert "a" in reg
    assert "b" in reg
    assert "missing" not in reg
    assert reg.get("a") is not None
    assert reg.get("missing") is None
    assert len(reg) == 2


def test_registry_rejects_duplicate_name() -> None:
    reg = SkillRegistry([_skill("dup")])
    with pytest.raises(ValueError, match="already has a skill named"):
        reg.add(_skill("dup"))


def test_registry_remove_returns_true_on_success() -> None:
    reg = SkillRegistry([_skill("a")])
    assert reg.remove("a") is True
    assert reg.remove("a") is False
    assert len(reg) == 0


def test_registry_all_actions_returns_in_registration_order() -> None:
    reg = SkillRegistry([_skill("first"), _skill("second"), _skill("third")])
    actions = reg.all_actions()
    assert [a.name for a in actions] == ["first", "second", "third"]


# ── Conditional discovery ────────────────────────────────────────────


def test_relevant_to_substring_ranker() -> None:
    reg = SkillRegistry(
        [
            _skill("pr_summary", when_to_use="user asks about pull request"),
            _skill("doc_search", when_to_use="user wants to find documentation"),
            _skill("greet", when_to_use="say hello"),
        ]
    )
    relevant = reg.relevant_to("can you summarise this pull request?")
    names = [s.name for s in relevant]
    assert "pr_summary" in names
    # Doc-search shouldn't match "pull request" closely.
    assert names[0] == "pr_summary"


def test_relevant_to_returns_empty_when_no_match() -> None:
    reg = SkillRegistry(
        [_skill("greet", when_to_use="say hello"), _skill("farewell", when_to_use="say goodbye")]
    )
    assert reg.relevant_to("what is the weather like?") == []


def test_relevant_to_respects_limit() -> None:
    reg = SkillRegistry(
        [
            _skill(f"skill_{i}", when_to_use="match this query")
            for i in range(10)
        ]
    )
    relevant = reg.relevant_to("match query", limit=3)
    assert len(relevant) == 3


def test_relevant_to_sync_rejects_async_ranker() -> None:
    reg = SkillRegistry([_skill("a", when_to_use="x")])

    async def fake_ranker(query, skills):
        return skills

    with pytest.raises(ValueError, match="async ranker"):
        reg.relevant_to("x", ranker=fake_ranker)


@pytest.mark.asyncio
async def test_aelevant_to_uses_custom_ranker() -> None:
    reg = SkillRegistry(
        [_skill("alpha", when_to_use="A"), _skill("beta", when_to_use="B")]
    )
    seen = {}

    async def reverse_ranker(query, skills):
        seen["called"] = True
        return list(reversed(skills))

    result = await reg.aelevant_to("anything", ranker=reverse_ranker)
    assert seen["called"] is True
    assert [s.name for s in result] == ["beta", "alpha"]


@pytest.mark.asyncio
async def test_aelevant_to_falls_back_when_ranker_raises() -> None:
    reg = SkillRegistry(
        [_skill("greet", when_to_use="say hello")]
    )

    async def broken_ranker(query, skills):
        raise RuntimeError("ranker died")

    result = await reg.aelevant_to("say hello world", ranker=broken_ranker)
    # Falls back to substring rank — "say hello" matches.
    assert len(result) == 1
    assert result[0].name == "greet"


def test_actions_for_returns_actions_only() -> None:
    reg = SkillRegistry(
        [_skill("greet", when_to_use="say hello")]
    )
    actions = reg.actions_for("say hello")
    assert len(actions) == 1
    assert isinstance(actions[0], Action)
    assert actions[0].name == "greet"
