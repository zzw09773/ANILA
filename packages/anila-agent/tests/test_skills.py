"""skills loader：frontmatter 恆載入、body 按需、信任邊界。"""

from __future__ import annotations

import pytest

from anila_agent.skills.loader import load_skills, skills_index

pytestmark = pytest.mark.unit


def _make_skill(root, name, *, trusted=True, body="# body\n做某事"):
    d = root / name
    d.mkdir()
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {name} 描述\nwhen_to_use: 當需要{name}時\n"
        f"trusted: {str(trusted).lower()}\n---\n{body}",
        encoding="utf-8",
    )


def test_load_skills_reads_frontmatter(tmp_path):
    _make_skill(tmp_path, "alpha")
    skills = load_skills(tmp_path)
    assert len(skills) == 1
    s = skills[0]
    assert s.name == "alpha" and s.description == "alpha 描述"
    assert s.when_to_use == "當需要alpha時" and s.trusted is True


def test_body_loaded_on_demand(tmp_path):
    _make_skill(tmp_path, "beta", body="# 標題\n步驟內容")
    s = load_skills(tmp_path)[0]
    assert "步驟內容" in s.load_body()


def test_untrusted_flag_preserved(tmp_path):
    _make_skill(tmp_path, "gamma", trusted=False)
    assert load_skills(tmp_path)[0].trusted is False


def test_skills_index(tmp_path):
    _make_skill(tmp_path, "alpha")
    idx = skills_index(load_skills(tmp_path))
    assert "alpha" in idx and "何時用" in idx


def test_missing_dir_returns_empty(tmp_path):
    assert load_skills(tmp_path / "nope") == []
    assert skills_index([]) == ""
