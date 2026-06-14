"""slash 指令：frontmatter + body 的 markdown macro → agent input。

從 ``configs/commands/<name>.md`` 載入；body 內 ``{{args}}`` 以使用者輸入替換。
操作者不必改 Python 就能擴充行為（data-over-code）。內建 REPL 指令
（/help、/memory、/style、/clear）由 REPL 直接處理，不在此。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

_FM = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.DOTALL)


@dataclass(frozen=True)
class SlashCommand:
    name: str
    description: str
    body: str

    def expand(self, args: str) -> str:
        """把 body 的 {{args}} 換成使用者輸入。"""
        return self.body.replace("{{args}}", args).strip()


def parse_slash(line: str) -> tuple[str, str] | None:
    """``/name rest`` → (name, rest)；非 slash 回 None。"""
    text = line.strip()
    if not text.startswith("/") or len(text) < 2:
        return None
    rest = text[1:]
    parts = rest.split(maxsplit=1)
    return parts[0], (parts[1] if len(parts) > 1 else "")


def load_commands(config_dir: str | os.PathLike[str] | None = None) -> dict[str, SlashCommand]:
    """讀 configs/commands/*.md。"""
    base = Path(config_dir) if config_dir is not None else Path("configs")
    cmd_dir = base / "commands"
    commands: dict[str, SlashCommand] = {}
    if not cmd_dir.is_dir():
        return commands
    for path in sorted(cmd_dir.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        m = _FM.match(text)
        if m:
            import yaml

            fm = yaml.safe_load(m.group(1)) or {}
            body = m.group(2).strip()
            description = str(fm.get("description", ""))
        else:
            body = text.strip()
            description = ""
        commands[path.stem] = SlashCommand(name=path.stem, description=description, body=body)
    return commands
