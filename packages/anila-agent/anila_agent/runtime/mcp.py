"""原生 MCP client 接線（optional，config-gated）。

從 ``configs/mcp.yaml`` 建 MCP server（stdio / sse / streamable_http），可附
``allowed_tools`` 做 static tool filter（least-privilege，對應 csk- 綁定 collection
的最小權限設計）。未設 mcp.yaml → 回 []。

注意：MCP server 有連線生命週期（需 async context / connect-disconnect）。本函式只
**建構** server 物件；呼叫端負責管理生命週期後再傳給 ``Agent(mcp_servers=...)``。
預設 CLI 流程不啟用 MCP。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any


def build_mcp_servers(config_dir: str | os.PathLike[str] | None = None) -> list[Any]:
    """讀 configs/mcp.yaml 建 MCP server 清單；無設定回 []。"""
    cfg_dir = Path(config_dir) if config_dir is not None else Path("configs")
    path = cfg_dir / "mcp.yaml"
    if not path.is_file():
        return []

    import yaml
    from agents.mcp import (
        MCPServerSse,
        MCPServerStdio,
        MCPServerStreamableHttp,
        create_static_tool_filter,
    )

    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    servers: list[Any] = []
    for spec in data.get("servers", []) or []:
        if not isinstance(spec, dict):
            continue
        kind = str(spec.get("type", "")).strip().lower()
        allowed = spec.get("allowed_tools")
        tool_filter = create_static_tool_filter(allowed_tool_names=allowed) if allowed else None

        if kind == "stdio":
            servers.append(
                MCPServerStdio(
                    params={"command": spec["command"], "args": spec.get("args", [])},
                    tool_filter=tool_filter,
                )
            )
        elif kind == "sse":
            servers.append(
                MCPServerSse(params={"url": spec["url"]}, tool_filter=tool_filter)
            )
        elif kind in ("streamable_http", "http"):
            servers.append(
                MCPServerStreamableHttp(params={"url": spec["url"]}, tool_filter=tool_filter)
            )
        else:
            raise ValueError(f"configs/mcp.yaml: 未知 server type {kind!r}")
    return servers
