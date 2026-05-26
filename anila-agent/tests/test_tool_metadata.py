"""P0-2 Tool metadata 擴充的 unit test。

涵蓋:
- `ToolMetadata` 預設值與向後相容欄位。
- `concurrency_safe` 依 `is_read_only` 自動推導。
- `requires_confirmation` 與 `requires_approval` 互通。
- `@anila_tool` 裝飾器正確掛上 metadata。
- `AnilaTool.from_function` 程式化路徑正確。
- `ToolRegistry.find_by_metadata` 過濾邏輯。
- 既有 tool(filesystem read / list、RAG search / read_document)的 metadata 正確。
"""

from __future__ import annotations

import pytest
from agents import FunctionTool

from anila_agent.tools import filesystem_tools, rag_tools
from anila_agent.tools.base import (
    AnilaTool,
    ToolMetadata,
    anila_tool,
    get_metadata,
)
from anila_agent.tools.registry import ToolRegistry


# ---------------------------------------------------------------------------
# ToolMetadata 結構本身
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_metadata_defaults() -> None:
    """預設 metadata 應為「保守值」:非唯讀、非破壞性、不可平行、不需 approval。"""
    meta = ToolMetadata()
    assert meta.is_read_only is False
    assert meta.is_destructive is False
    assert meta.concurrency_safe is False  # 預設依 is_read_only=False 推導
    assert meta.cost_estimate is None
    assert meta.requires_approval is False
    assert meta.is_open_world is False
    assert meta.category == "general"
    assert meta.requires_confirmation is False


@pytest.mark.unit
def test_concurrency_safe_inferred_from_read_only() -> None:
    """唯讀 tool 預設可平行;寫入 tool 預設不可平行。"""
    read_only = ToolMetadata(is_read_only=True)
    assert read_only.concurrency_safe is True

    writer = ToolMetadata(is_read_only=False)
    assert writer.concurrency_safe is False


@pytest.mark.unit
def test_concurrency_safe_explicit_override() -> None:
    """`concurrency_safe` 顯式宣告時不被自動推導蓋掉。"""
    # idempotent 寫入 — 寫入但仍可平行
    meta = ToolMetadata(is_read_only=False, concurrency_safe=True)
    assert meta.concurrency_safe is True

    # 唯讀但有 race condition,顯式禁止平行
    meta2 = ToolMetadata(is_read_only=True, concurrency_safe=False)
    assert meta2.concurrency_safe is False


@pytest.mark.unit
def test_requires_confirmation_alias_to_requires_approval() -> None:
    """舊欄位 `requires_confirmation=True` 應自動帶起 `requires_approval`。"""
    meta = ToolMetadata(requires_confirmation=True)
    assert meta.requires_confirmation is True
    assert meta.requires_approval is True


@pytest.mark.unit
def test_metadata_immutable() -> None:
    """`ToolMetadata` 為 frozen dataclass,不應可變更。"""
    meta = ToolMetadata(is_read_only=True)
    with pytest.raises(Exception):
        meta.is_read_only = False  # type: ignore[misc]


# ---------------------------------------------------------------------------
# @anila_tool decorator 與 AnilaTool 程式化路徑
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_anila_tool_decorator_attaches_metadata() -> None:
    @anila_tool(
        is_read_only=True,
        cost_estimate="low",
        category="test",
    )
    def sample(x: int) -> int:
        """sample doc."""
        return x

    assert isinstance(sample, FunctionTool)
    meta = get_metadata(sample)
    assert meta.is_read_only is True
    assert meta.cost_estimate == "low"
    assert meta.category == "test"
    assert meta.concurrency_safe is True  # 自動推導


@pytest.mark.unit
def test_anila_tool_destructive_metadata() -> None:
    @anila_tool(
        is_destructive=True,
        requires_approval=True,
        category="filesystem",
        cost_estimate="medium",
    )
    def delete_thing(name: str) -> None:
        """delete doc."""
        return None

    meta = get_metadata(delete_thing)
    assert meta.is_destructive is True
    assert meta.requires_approval is True
    assert meta.is_read_only is False
    assert meta.concurrency_safe is False  # 寫入 tool 預設不可平行


@pytest.mark.unit
def test_anila_tool_open_world_flag() -> None:
    @anila_tool(is_open_world=True, category="network")
    def fetch_url(url: str) -> str:
        """fetch doc."""
        return ""

    meta = get_metadata(fetch_url)
    assert meta.is_open_world is True
    assert meta.category == "network"


@pytest.mark.unit
def test_get_metadata_default_for_untagged() -> None:
    """非 anila tool 物件呼叫 `get_metadata` 應回傳預設值(向後相容)。"""

    class NotATool:
        pass

    meta = get_metadata(NotATool())
    assert meta == ToolMetadata()


@pytest.mark.unit
def test_anila_tool_from_function() -> None:
    """程式化路徑也應正確掛 metadata。"""

    def my_fn(value: str) -> str:
        """fn doc."""
        return value

    tool = AnilaTool.from_function(
        my_fn,
        is_read_only=True,
        cost_estimate="free",
        category="util",
    )
    assert isinstance(tool, FunctionTool)
    meta = get_metadata(tool)
    assert meta.is_read_only is True
    assert meta.cost_estimate == "free"
    assert meta.category == "util"


# ---------------------------------------------------------------------------
# ToolRegistry.find_by_metadata
# ---------------------------------------------------------------------------


def _make_registry() -> ToolRegistry:
    """建立含多樣 metadata 的測試 registry。"""

    @anila_tool(
        is_read_only=True,
        category="retrieval",
        cost_estimate="low",
        name_override="r_search",
    )
    def r_search(q: str) -> str:
        """search doc."""
        return q

    @anila_tool(
        is_read_only=True,
        category="filesystem",
        cost_estimate="free",
        name_override="fs_read",
    )
    def fs_read(p: str) -> str:
        """read doc."""
        return p

    @anila_tool(
        is_destructive=True,
        requires_approval=True,
        category="filesystem",
        cost_estimate="medium",
        name_override="fs_write",
    )
    def fs_write(p: str, content: str) -> None:
        """write doc."""
        return None

    @anila_tool(
        is_open_world=True,
        category="network",
        cost_estimate="high",
        name_override="net_fetch",
    )
    def net_fetch(url: str) -> str:
        """fetch doc."""
        return url

    registry = ToolRegistry()
    registry.add_many([r_search, fs_read, fs_write, net_fetch])
    return registry


@pytest.mark.unit
def test_registry_find_by_read_only() -> None:
    registry = _make_registry()
    found = registry.find_by_metadata(is_read_only=True)
    names = {t.name for t in found}
    assert names == {"r_search", "fs_read"}


@pytest.mark.unit
def test_registry_find_by_destructive() -> None:
    registry = _make_registry()
    found = registry.find_by_metadata(is_destructive=True)
    assert {t.name for t in found} == {"fs_write"}


@pytest.mark.unit
def test_registry_find_by_category() -> None:
    registry = _make_registry()
    found = registry.find_by_metadata(category="filesystem")
    assert {t.name for t in found} == {"fs_read", "fs_write"}


@pytest.mark.unit
def test_registry_find_by_multiple_conditions_and() -> None:
    """多條件以 AND 串接。"""
    registry = _make_registry()
    found = registry.find_by_metadata(category="filesystem", is_read_only=True)
    assert {t.name for t in found} == {"fs_read"}


@pytest.mark.unit
def test_registry_find_by_cost_estimate() -> None:
    registry = _make_registry()
    high = registry.find_by_metadata(cost_estimate="high")
    assert {t.name for t in high} == {"net_fetch"}

    free = registry.find_by_metadata(cost_estimate="free")
    assert {t.name for t in free} == {"fs_read"}


@pytest.mark.unit
def test_registry_find_by_open_world() -> None:
    registry = _make_registry()
    found = registry.find_by_metadata(is_open_world=True)
    assert {t.name for t in found} == {"net_fetch"}


@pytest.mark.unit
def test_registry_no_filter_returns_all() -> None:
    """無條件時應返回 registry 所有 tool。"""
    registry = _make_registry()
    found = registry.find_by_metadata()
    assert len(found) == 4


@pytest.mark.unit
def test_registry_get_metadata_lookup() -> None:
    registry = _make_registry()
    meta = registry.get_metadata("fs_write")
    assert meta.is_destructive is True
    assert meta.requires_approval is True

    with pytest.raises(KeyError):
        registry.get_metadata("does_not_exist")


# ---------------------------------------------------------------------------
# 既有 tool 的 metadata 設對(回歸保護)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_filesystem_read_file_metadata() -> None:
    meta = get_metadata(filesystem_tools.read_file)
    assert meta.is_read_only is True
    assert meta.is_destructive is False
    assert meta.concurrency_safe is True
    assert meta.category == "filesystem"
    assert meta.cost_estimate == "free"


@pytest.mark.unit
def test_filesystem_list_dir_metadata() -> None:
    meta = get_metadata(filesystem_tools.list_dir)
    assert meta.is_read_only is True
    assert meta.is_destructive is False
    assert meta.concurrency_safe is True
    assert meta.category == "filesystem"


@pytest.mark.unit
def test_rag_search_documents_metadata() -> None:
    meta = get_metadata(rag_tools.search_documents)
    assert meta.is_read_only is True
    assert meta.is_destructive is False
    assert meta.concurrency_safe is True
    assert meta.category == "retrieval"
    assert meta.cost_estimate == "low"


@pytest.mark.unit
def test_rag_read_document_metadata() -> None:
    meta = get_metadata(rag_tools.read_document)
    assert meta.is_read_only is True
    assert meta.is_destructive is False
    assert meta.concurrency_safe is True
    assert meta.category == "retrieval"
