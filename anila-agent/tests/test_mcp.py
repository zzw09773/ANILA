"""MCP 接線：無 config 時回 []（不污染預設流程）。"""

from __future__ import annotations

import pytest

from anila_agent.runtime.mcp import build_mcp_servers

pytestmark = pytest.mark.unit


def test_no_config_returns_empty(tmp_path):
    assert build_mcp_servers(tmp_path) == []


def test_unknown_server_type_raises(tmp_path):
    (tmp_path / "mcp.yaml").write_text(
        "servers:\n  - { type: carrier_pigeon, url: 'x' }\n", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="未知 server type"):
        build_mcp_servers(tmp_path)
