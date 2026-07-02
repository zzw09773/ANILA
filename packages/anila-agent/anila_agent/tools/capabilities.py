"""工具能力表：唯一真相來源 name → read_only | write | admin。

SDK 的 ``@function_tool`` **沒有** is_read_only 欄位，所以工具能力在
``configs/tools.yaml`` 宣告，由 policy DSL（P2）與 agent_factory 的 fail-closed
啟動守衛消費。沒有這張表，「read-only-by-default + deny-all」的安全姿態無從實作。

fail-closed：未登錄的工具視為最受限（admin）——deny-all 政策下，未知工具預設被拒，
除非明確 allow。
"""

from __future__ import annotations

import os
from enum import Enum
from pathlib import Path


class Capability(str, Enum):
    READ_ONLY = "read_only"
    WRITE = "write"
    ADMIN = "admin"


# 內建工具的預設能力（皆 read-only）。configs/tools.yaml 可覆寫/擴充。
_BUILTIN: dict[str, Capability] = {
    "search_documents": Capability.READ_ONLY,
    "read_document": Capability.READ_ONLY,
    "search_memory": Capability.READ_ONLY,
}


def load_capabilities(config_dir: str | os.PathLike[str] | None = None) -> dict[str, Capability]:
    """讀 configs/tools.yaml 的 capabilities 區塊，疊在內建預設之上。"""
    caps: dict[str, Capability] = dict(_BUILTIN)
    cfg_dir = Path(config_dir) if config_dir is not None else Path("configs")
    path = cfg_dir / "tools.yaml"
    if not path.is_file():
        return caps

    import yaml

    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    raw = (data.get("capabilities") or {}) if isinstance(data, dict) else {}
    for name, value in raw.items():
        try:
            caps[name] = Capability(str(value).strip().lower())
        except ValueError as e:
            raise ValueError(
                f"configs/tools.yaml: 工具 {name!r} 的能力 {value!r} 不合法"
                f"（須為 read_only/write/admin）"
            ) from e
    return caps


def capability_of(name: str, caps: dict[str, Capability]) -> Capability:
    """查工具能力；未登錄者 fail-closed 視為 admin（最受限）。"""
    return caps.get(name, Capability.ADMIN)
