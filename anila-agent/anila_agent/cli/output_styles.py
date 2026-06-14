"""output style 載入：可切換的回應 persona（.md）。

先找 ``configs/output_styles/<name>.md``（操作者覆寫），再退回套件內建
``prompts/output_styles/<name>.md``。內建：zh-tw-formal、concise-cited。
"""

from __future__ import annotations

import os
from pathlib import Path

_BUILTIN_DIR = Path(__file__).parent.parent / "prompts" / "output_styles"


def _user_dir(config_dir: str | os.PathLike[str] | None) -> Path:
    base = Path(config_dir) if config_dir is not None else Path("configs")
    return base / "output_styles"


def load_output_style(
    name: str, config_dir: str | os.PathLike[str] | None = None
) -> str | None:
    """讀 output style 內文；找不到回 None。"""
    if not name:
        return None
    for directory in (_user_dir(config_dir), _BUILTIN_DIR):
        path = directory / f"{name}.md"
        if path.is_file():
            return path.read_text(encoding="utf-8").strip()
    return None


def list_output_styles(config_dir: str | os.PathLike[str] | None = None) -> list[str]:
    """列出可用 style 名（使用者 + 內建，去重）。"""
    names: set[str] = set()
    for directory in (_BUILTIN_DIR, _user_dir(config_dir)):
        if directory.is_dir():
            names.update(p.stem for p in directory.glob("*.md"))
    return sorted(names)
