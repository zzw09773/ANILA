"""memdir 長期記憶分類法（移植自 Claude Code memdir）。

四型：
  - ``user``      — 使用者是誰（角色、專長、偏好）
  - ``feedback``  — 使用者給的工作方式指引（修正/確認），body 需附 Why + How to apply
  - ``project``   — 進行中的工作、目標、限制；相對日期換成絕對日期
  - ``reference`` — 外部資源指標（URL、dashboard、ticket）

不該存的（repo 已記錄者）：程式結構、過往修復、git history、CLAUDE.md，或只與
單次對話相關的內容。
"""

from __future__ import annotations

from enum import Enum


class MemoryType(str, Enum):
    USER = "user"
    FEEDBACK = "feedback"
    PROJECT = "project"
    REFERENCE = "reference"


VALID_TYPES: frozenset[str] = frozenset(t.value for t in MemoryType)

# 這些型別的 body 應包含 Why 與 How to apply。
NEEDS_WHY_HOW: frozenset[MemoryType] = frozenset({MemoryType.FEEDBACK, MemoryType.PROJECT})


def coerce_type(value: str) -> MemoryType:
    """把字串轉成 MemoryType；不合法則 raise。"""
    try:
        return MemoryType(value.strip().lower())
    except ValueError as e:
        raise ValueError(f"記憶 type {value!r} 不合法，須為 {sorted(VALID_TYPES)}") from e
