"""工具能力表載入與 fail-closed 查詢。"""

from __future__ import annotations

import pytest

from anila_agent.tools.capabilities import Capability, capability_of, load_capabilities

pytestmark = pytest.mark.unit


def test_builtin_tools_are_read_only(tmp_path):
    caps = load_capabilities(tmp_path)  # 無 tools.yaml → 內建預設
    assert caps["search_documents"] is Capability.READ_ONLY
    assert caps["read_document"] is Capability.READ_ONLY


def test_yaml_overrides_and_extends(tmp_path):
    (tmp_path / "tools.yaml").write_text(
        "capabilities:\n  ingest_document: write\n  purge_collection: admin\n", encoding="utf-8"
    )
    caps = load_capabilities(tmp_path)
    assert caps["ingest_document"] is Capability.WRITE
    assert caps["purge_collection"] is Capability.ADMIN
    assert caps["search_documents"] is Capability.READ_ONLY  # 內建保留


def test_invalid_capability_raises(tmp_path):
    (tmp_path / "tools.yaml").write_text("capabilities:\n  x: superuser\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_capabilities(tmp_path)


def test_unknown_tool_fails_closed_to_admin():
    caps = {"search_documents": Capability.READ_ONLY}
    # 未登錄工具 → 視為 admin（最受限），配合 deny-all 預設被拒。
    assert capability_of("rm_rf_everything", caps) is Capability.ADMIN
    assert capability_of("search_documents", caps) is Capability.READ_ONLY
