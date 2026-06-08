"""Skills package — P2-11 Skill loader + registry。

Skill 是「markdown 檔含 YAML frontmatter」的可載入單元,frontmatter 描述
metadata / tool list / policy / parameters,markdown body 為 prompt template。

對外 API::

    from anila_agent.skills import (
        Skill,
        SkillLoader,
        SkillRegistry,
        SkillFrontmatterError,
        AppliedSkill,
    )

    loader = SkillLoader()
    skill = loader.load_from_file("skills/file_summarizer.md")
    registry = SkillRegistry()
    registry.register(skill)
    registry.apply_skill(
        skill,
        tool_registry=tool_registry,
        policy_engine=policy_engine,
    )

Examples 目錄(``anila_agent/skills/examples/``)提供三個可載入的示範
skill:``file_summarizer.md`` / ``code_reviewer.md`` / ``task_planner.md``。
"""

from __future__ import annotations

from anila_agent.skills.loader import (
    AppliedSkill,
    Skill,
    SkillFrontmatterError,
    SkillLoader,
    SkillRegistry,
)

__all__ = [
    "AppliedSkill",
    "Skill",
    "SkillFrontmatterError",
    "SkillLoader",
    "SkillRegistry",
]
