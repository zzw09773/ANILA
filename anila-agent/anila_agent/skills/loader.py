"""skill 載入（移植自 Claude Code 的 SKILL.md 模式）。

progressive disclosure：frontmatter（name / description / when_to_use）**恆載入**，
body **按需**讀取（避免一次塞爆 context）。保留「不信任 skill」的信任邊界——
標為非 trusted 的 skill 若含 inline shell，呼叫端應拒絕自動執行。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

_FM = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.DOTALL)


@dataclass
class Skill:
    name: str
    description: str
    when_to_use: str
    path: Path
    trusted: bool = False

    def load_body(self) -> str:
        """按需讀 skill body（frontmatter 之後的內容）。"""
        text = self.path.read_text(encoding="utf-8")
        m = _FM.match(text)
        return (m.group(2).strip() if m else text.strip())


def _parse_frontmatter(text: str) -> dict:
    m = _FM.match(text)
    if not m:
        return {}
    import yaml

    return yaml.safe_load(m.group(1)) or {}


def load_skills(skills_dir: str | os.PathLike[str], *, trusted: bool = False) -> list[Skill]:
    """掃描目錄下的 ``*/SKILL.md`` 與 ``*.md``，只讀 frontmatter（body 按需）。"""
    root = Path(skills_dir)
    if not root.is_dir():
        return []
    out: list[Skill] = []
    candidates = list(root.glob("*/SKILL.md")) + [
        p for p in root.glob("*.md") if p.name != "SKILL.md"
    ]
    for path in sorted(candidates):
        fm = _parse_frontmatter(path.read_text(encoding="utf-8"))
        name = str(fm.get("name") or path.parent.name if path.name == "SKILL.md" else path.stem)
        if not name:
            continue
        out.append(
            Skill(
                name=name,
                description=str(fm.get("description", "")),
                when_to_use=str(fm.get("when_to_use", "")),
                path=path,
                trusted=bool(fm.get("trusted", trusted)),
            )
        )
    return out


def skills_index(skills: list[Skill]) -> str:
    """組一份「何時用哪個 skill」的索引（給系統提示）。"""
    if not skills:
        return ""
    lines = ["# 可用 skills"]
    for s in skills:
        trigger = f" — 何時用：{s.when_to_use}" if s.when_to_use else ""
        lines.append(f"- **{s.name}**：{s.description}{trigger}")
    return "\n".join(lines)
