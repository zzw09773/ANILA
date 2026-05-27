"""P1-18 ToolSearch + deferred tools 的 unit / integration test。

涵蓋:
- ``ToolMetadata.is_deferred`` 預設與顯式設定
- ``ToolRegistry.register(deferred=True)`` / ``add`` 對應 metadata
- ``find_active`` / ``find_deferred`` per-session 行為
- ``activate`` / ``deactivate`` / ``reset_active_deferred``
- ``tool_search`` 各種 query 語法(select / +keyword / fuzzy)
- ``activate_tool`` 移轉並回傳 ok payload
- per-session 隔離(同 registry,不同 session)
- ``find_by_metadata(is_deferred=True)`` 過濾
- MCP-built tool 預設帶 ``is_deferred=True`` + 註冊後自動進 deferred 集合
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from agents import FunctionTool

from anila_agent.core.hook_context import SessionContext
from anila_agent.mcp.manager import _MCP_TOOL_METADATA, build_function_tool_from_mcp
from anila_agent.mcp.server import MCPTool
from anila_agent.tools import (
    ACTIVATE_TOOL_NAME,
    TOOL_SEARCH_TOOL_NAME,
    ToolMetadata,
    ToolRegistry,
    anila_tool,
    build_meta_tools,
    format_search_results,
    get_metadata,
    make_static_session_resolver,
    register_meta_tools,
    reset_active_deferred,
    search_deferred,
)

# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _build_registry_with_deferred() -> ToolRegistry:
    """建一個 registry,含 2 個 active + 3 個 deferred tool。"""

    @anila_tool(is_read_only=True, category="filesystem", name_override="read_file")
    def read_file(path: str) -> str:
        """Read a file from disk."""
        return path

    @anila_tool(is_read_only=True, category="retrieval", name_override="search")
    def search(query: str) -> str:
        """Search the knowledge base."""
        return query

    @anila_tool(
        is_read_only=True,
        category="github",
        is_deferred=True,
        name_override="github_search_issues",
    )
    def github_search_issues(repo: str) -> str:
        """Search GitHub issues in a repository."""
        return repo

    @anila_tool(
        is_read_only=True,
        category="github",
        is_deferred=True,
        name_override="github_get_pr",
    )
    def github_get_pr(repo: str, number: int) -> str:
        """Get a GitHub pull request."""
        return f"{repo}#{number}"

    @anila_tool(
        is_read_only=False,
        category="slack",
        is_deferred=True,
        name_override="slack_send_message",
    )
    def slack_send_message(channel: str, text: str) -> str:
        """Send a message to a Slack channel."""
        return text

    reg = ToolRegistry()
    reg.add_many(
        [read_file, search, github_search_issues, github_get_pr, slack_send_message]
    )
    return reg


def _new_session(session_id: str = "sess-test") -> SessionContext:
    return SessionContext(session_id=session_id)


# ---------------------------------------------------------------------------
# ToolMetadata.is_deferred
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_metadata_is_deferred_default_false() -> None:
    """既有 tool 預設不 deferred,以保持向後相容。"""
    meta = ToolMetadata()
    assert meta.is_deferred is False


@pytest.mark.unit
def test_metadata_is_deferred_explicit() -> None:
    meta = ToolMetadata(is_deferred=True)
    assert meta.is_deferred is True


@pytest.mark.unit
def test_anila_tool_decorator_sets_is_deferred() -> None:
    @anila_tool(is_deferred=True, category="mcp")
    def some_tool(x: int) -> int:
        """doc."""
        return x

    meta = get_metadata(some_tool)
    assert meta.is_deferred is True
    assert meta.category == "mcp"


# ---------------------------------------------------------------------------
# ToolRegistry deferred 機制
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_registry_add_auto_defers_from_metadata() -> None:
    """metadata 帶 ``is_deferred=True`` 時 ``add`` 自動列入 deferred。"""
    reg = _build_registry_with_deferred()
    assert reg.is_deferred("github_search_issues") is True
    assert reg.is_deferred("read_file") is False


@pytest.mark.unit
def test_registry_register_explicit_deferred_overrides_metadata() -> None:
    """``register(deferred=True)`` 即使 metadata 沒標也能列入 deferred(運行期覆寫)。"""

    @anila_tool(is_read_only=True, name_override="util")
    def util(x: int) -> int:
        """doc."""
        return x

    reg = ToolRegistry()
    reg.register(util, deferred=True)
    assert reg.is_deferred("util") is True
    # metadata 仍是 False — registry 層的 set 才是 source of truth
    assert get_metadata(util).is_deferred is False


@pytest.mark.unit
def test_registry_duplicate_name_raises() -> None:
    @anila_tool(name_override="dup")
    def a(x: int) -> int:
        """doc."""
        return x

    @anila_tool(name_override="dup")
    def b(x: int) -> int:
        """doc."""
        return x

    reg = ToolRegistry()
    reg.add(a)
    with pytest.raises(ValueError, match="Duplicate"):
        reg.add(b)


@pytest.mark.unit
def test_find_active_excludes_deferred_when_no_session() -> None:
    reg = _build_registry_with_deferred()
    active = reg.find_active()
    names = {t.name for t in active}
    assert names == {"read_file", "search"}


@pytest.mark.unit
def test_find_active_with_session_no_activation() -> None:
    """有 session 但沒 activate 過任何 deferred → 結果同 no session。"""
    reg = _build_registry_with_deferred()
    sess = _new_session()
    active = reg.find_active(session=sess)
    names = {t.name for t in active}
    assert names == {"read_file", "search"}


@pytest.mark.unit
def test_find_deferred_lists_all_deferred() -> None:
    reg = _build_registry_with_deferred()
    deferred = reg.find_deferred()
    names = {t.name for t in deferred}
    assert names == {"github_search_issues", "github_get_pr", "slack_send_message"}


@pytest.mark.unit
def test_find_deferred_with_session_excludes_activated() -> None:
    reg = _build_registry_with_deferred()
    sess = _new_session()
    reg.activate("github_get_pr", session=sess)
    deferred = reg.find_deferred(session=sess)
    names = {t.name for t in deferred}
    assert names == {"github_search_issues", "slack_send_message"}


@pytest.mark.unit
def test_activate_moves_tool_into_active() -> None:
    reg = _build_registry_with_deferred()
    sess = _new_session()
    reg.activate("github_search_issues", session=sess)
    active = reg.find_active(session=sess)
    names = {t.name for t in active}
    assert names == {"read_file", "search", "github_search_issues"}


@pytest.mark.unit
def test_activate_unknown_raises_key_error() -> None:
    reg = _build_registry_with_deferred()
    sess = _new_session()
    with pytest.raises(KeyError):
        reg.activate("does_not_exist", session=sess)


@pytest.mark.unit
def test_activate_non_deferred_raises_value_error() -> None:
    reg = _build_registry_with_deferred()
    sess = _new_session()
    with pytest.raises(ValueError, match="not deferred"):
        reg.activate("read_file", session=sess)


@pytest.mark.unit
def test_deactivate_removes_from_active() -> None:
    reg = _build_registry_with_deferred()
    sess = _new_session()
    reg.activate("github_search_issues", session=sess)
    reg.deactivate("github_search_issues", session=sess)
    active_names = {t.name for t in reg.find_active(session=sess)}
    assert "github_search_issues" not in active_names


@pytest.mark.unit
def test_deactivate_noop_if_not_active() -> None:
    """deactivate 沒問題的 tool 應為 no-op,不丟 exception。"""
    reg = _build_registry_with_deferred()
    sess = _new_session()
    reg.deactivate("github_search_issues", session=sess)  # 沒 activate 過


@pytest.mark.unit
def test_reset_active_deferred_clears_session_state() -> None:
    reg = _build_registry_with_deferred()
    sess = _new_session()
    reg.activate("github_search_issues", session=sess)
    reg.activate("slack_send_message", session=sess)
    assert len(reg.find_active(session=sess)) == 4

    reset_active_deferred(sess)

    active_names = {t.name for t in reg.find_active(session=sess)}
    assert active_names == {"read_file", "search"}


@pytest.mark.unit
def test_per_session_isolation() -> None:
    """同 registry,兩個 session activate 不同 deferred tool,互不影響。"""
    reg = _build_registry_with_deferred()
    sess_a = _new_session("sess-a")
    sess_b = _new_session("sess-b")

    reg.activate("github_search_issues", session=sess_a)
    reg.activate("slack_send_message", session=sess_b)

    a_active = {t.name for t in reg.find_active(session=sess_a)}
    b_active = {t.name for t in reg.find_active(session=sess_b)}

    assert "github_search_issues" in a_active
    assert "slack_send_message" not in a_active
    assert "slack_send_message" in b_active
    assert "github_search_issues" not in b_active


@pytest.mark.unit
def test_session_end_loses_activation() -> None:
    """SessionContext 是 dataclass,引用消失後新 session 不繼承狀態(用新 session 模擬)。"""
    reg = _build_registry_with_deferred()
    sess_1 = _new_session("first")
    reg.activate("github_search_issues", session=sess_1)
    assert "github_search_issues" in {t.name for t in reg.find_active(session=sess_1)}

    # 模擬「session 結束」— 開一個全新 session,registry 看不到上一 session 的啟用紀錄。
    sess_2 = _new_session("second")
    active_names = {t.name for t in reg.find_active(session=sess_2)}
    assert active_names == {"read_file", "search"}


# ---------------------------------------------------------------------------
# find_by_metadata 新增 is_deferred filter
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_find_by_metadata_is_deferred_true() -> None:
    """metadata-driven 過濾:標 is_deferred=True 的 tool 才會被列。"""
    reg = _build_registry_with_deferred()
    found = reg.find_by_metadata(is_deferred=True)
    names = {t.name for t in found}
    assert names == {"github_search_issues", "github_get_pr", "slack_send_message"}


@pytest.mark.unit
def test_find_by_metadata_is_deferred_false() -> None:
    reg = _build_registry_with_deferred()
    found = reg.find_by_metadata(is_deferred=False)
    names = {t.name for t in found}
    assert names == {"read_file", "search"}


# ---------------------------------------------------------------------------
# search_deferred 演算法
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_search_deferred_select_clause_exact() -> None:
    reg = _build_registry_with_deferred()
    results = search_deferred(reg, "select:github_get_pr")
    assert len(results) == 1
    assert results[0].name == "github_get_pr"
    assert results[0].score == 1.0


@pytest.mark.unit
def test_search_deferred_select_case_insensitive() -> None:
    reg = _build_registry_with_deferred()
    results = search_deferred(reg, "select:GITHUB_GET_PR,SLACK_SEND_MESSAGE")
    names = {r.name for r in results}
    assert names == {"github_get_pr", "slack_send_message"}


@pytest.mark.unit
def test_search_deferred_select_skips_non_deferred() -> None:
    """select: 只在 deferred pool 內找;active tool 不會被命中。"""
    reg = _build_registry_with_deferred()
    results = search_deferred(reg, "select:read_file")
    assert results == []


@pytest.mark.unit
def test_search_deferred_required_keyword() -> None:
    reg = _build_registry_with_deferred()
    results = search_deferred(reg, "+github issues")
    names = {r.name for r in results}
    # 兩個 github_* 都應入選,slack 不該入選
    assert names == {"github_search_issues", "github_get_pr"}


@pytest.mark.unit
def test_search_deferred_required_keyword_rules_out_all() -> None:
    reg = _build_registry_with_deferred()
    results = search_deferred(reg, "+nonexistent")
    assert results == []


@pytest.mark.unit
def test_search_deferred_fuzzy_ranking_top_k() -> None:
    """純 fuzzy query → 取 top_k,score 高的排前面。"""
    reg = _build_registry_with_deferred()
    results = search_deferred(reg, "slack message", top_k=2)
    assert len(results) <= 2
    # slack_send_message 應該排第一
    assert results[0].name == "slack_send_message"


@pytest.mark.unit
def test_search_deferred_top_k_clamps() -> None:
    reg = _build_registry_with_deferred()
    results = search_deferred(reg, "github", top_k=1)
    assert len(results) == 1


@pytest.mark.unit
def test_search_deferred_excludes_activated_when_session_given() -> None:
    reg = _build_registry_with_deferred()
    sess = _new_session()
    reg.activate("github_search_issues", session=sess)
    results = search_deferred(reg, "+github", session=sess)
    names = {r.name for r in results}
    assert "github_search_issues" not in names
    assert "github_get_pr" in names


# ---------------------------------------------------------------------------
# format_search_results
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_format_search_results_empty() -> None:
    payload = json.loads(format_search_results([]))
    assert payload["matches"] == []
    assert "hint" in payload


@pytest.mark.unit
def test_format_search_results_includes_hint() -> None:
    reg = _build_registry_with_deferred()
    results = search_deferred(reg, "select:github_get_pr")
    payload = json.loads(format_search_results(results))
    assert payload["matches"][0]["name"] == "github_get_pr"
    assert ACTIVATE_TOOL_NAME in payload["hint"]


# ---------------------------------------------------------------------------
# Meta-tool FunctionTool: tool_search / activate_tool
# ---------------------------------------------------------------------------


async def _invoke(tool: FunctionTool, args: dict[str, Any]) -> str:
    """Convenience wrapper to invoke a FunctionTool with JSON args."""
    result = await tool.on_invoke_tool(None, json.dumps(args))  # type: ignore[arg-type]
    assert isinstance(result, str)
    return result


@pytest.mark.unit
async def test_build_meta_tools_returns_two() -> None:
    reg = _build_registry_with_deferred()
    sess = _new_session()
    tools = build_meta_tools(reg, make_static_session_resolver(sess))
    names = {t.name for t in tools}
    assert names == {TOOL_SEARCH_TOOL_NAME, ACTIVATE_TOOL_NAME}


@pytest.mark.unit
async def test_tool_search_invocation_returns_matches() -> None:
    reg = _build_registry_with_deferred()
    sess = _new_session()
    tools = build_meta_tools(reg, make_static_session_resolver(sess))
    search_tool = next(t for t in tools if t.name == TOOL_SEARCH_TOOL_NAME)

    result = await _invoke(search_tool, {"query": "select:github_get_pr"})
    payload = json.loads(result)
    assert len(payload["matches"]) == 1
    assert payload["matches"][0]["name"] == "github_get_pr"


@pytest.mark.unit
async def test_tool_search_invocation_top_k_default() -> None:
    reg = _build_registry_with_deferred()
    sess = _new_session()
    tools = build_meta_tools(reg, make_static_session_resolver(sess))
    search_tool = next(t for t in tools if t.name == TOOL_SEARCH_TOOL_NAME)

    result = await _invoke(search_tool, {"query": "+github"})
    payload = json.loads(result)
    assert len(payload["matches"]) == 2


@pytest.mark.unit
async def test_tool_search_invalid_json_returns_empty() -> None:
    reg = _build_registry_with_deferred()
    sess = _new_session()
    tools = build_meta_tools(reg, make_static_session_resolver(sess))
    search_tool = next(t for t in tools if t.name == TOOL_SEARCH_TOOL_NAME)

    raw = await search_tool.on_invoke_tool(None, "{not json}")  # type: ignore[arg-type]
    assert isinstance(raw, str)
    payload = json.loads(raw)
    assert payload["matches"] == []


@pytest.mark.unit
async def test_activate_tool_invocation_moves_into_active() -> None:
    reg = _build_registry_with_deferred()
    sess = _new_session()
    tools = build_meta_tools(reg, make_static_session_resolver(sess))
    activate = next(t for t in tools if t.name == ACTIVATE_TOOL_NAME)

    raw = await _invoke(activate, {"name": "github_get_pr"})
    payload = json.loads(raw)
    assert payload["ok"] is True
    assert payload["name"] == "github_get_pr"

    # 驗證 registry 真的把它移到 active
    active_names = {t.name for t in reg.find_active(session=sess)}
    assert "github_get_pr" in active_names


@pytest.mark.unit
async def test_activate_tool_unknown_returns_error() -> None:
    reg = _build_registry_with_deferred()
    sess = _new_session()
    tools = build_meta_tools(reg, make_static_session_resolver(sess))
    activate = next(t for t in tools if t.name == ACTIVATE_TOOL_NAME)

    raw = await _invoke(activate, {"name": "does_not_exist"})
    payload = json.loads(raw)
    assert payload["ok"] is False
    assert payload["error"] == "not_registered"


@pytest.mark.unit
async def test_activate_tool_non_deferred_returns_error() -> None:
    reg = _build_registry_with_deferred()
    sess = _new_session()
    tools = build_meta_tools(reg, make_static_session_resolver(sess))
    activate = next(t for t in tools if t.name == ACTIVATE_TOOL_NAME)

    raw = await _invoke(activate, {"name": "read_file"})
    payload = json.loads(raw)
    assert payload["ok"] is False
    assert payload["error"] == "not_deferred"


@pytest.mark.unit
async def test_activate_tool_missing_name_returns_error() -> None:
    reg = _build_registry_with_deferred()
    sess = _new_session()
    tools = build_meta_tools(reg, make_static_session_resolver(sess))
    activate = next(t for t in tools if t.name == ACTIVATE_TOOL_NAME)

    raw = await _invoke(activate, {})
    payload = json.loads(raw)
    assert payload["ok"] is False
    assert payload["error"] == "missing_name"


@pytest.mark.unit
async def test_meta_tools_session_resolver_updated_per_call() -> None:
    """resolver 可動態切換 session(模擬 runner 每 turn 換 session)。"""
    reg = _build_registry_with_deferred()
    sess_a = _new_session("a")
    sess_b = _new_session("b")
    current = {"sess": sess_a}

    def resolver() -> SessionContext:
        return current["sess"]

    tools = build_meta_tools(reg, resolver)
    activate = next(t for t in tools if t.name == ACTIVATE_TOOL_NAME)

    await _invoke(activate, {"name": "github_get_pr"})
    # 切到 sess_b
    current["sess"] = sess_b
    await _invoke(activate, {"name": "slack_send_message"})

    a_active = {t.name for t in reg.find_active(session=sess_a)}
    b_active = {t.name for t in reg.find_active(session=sess_b)}
    assert "github_get_pr" in a_active
    assert "github_get_pr" not in b_active
    assert "slack_send_message" in b_active
    assert "slack_send_message" not in a_active


@pytest.mark.unit
def test_register_meta_tools_adds_to_registry() -> None:
    """``register_meta_tools`` 把 meta-tool 註冊進 registry 且非 deferred。"""
    reg = _build_registry_with_deferred()
    sess = _new_session()
    register_meta_tools(reg, make_static_session_resolver(sess))

    assert TOOL_SEARCH_TOOL_NAME in reg.tools
    assert ACTIVATE_TOOL_NAME in reg.tools
    # meta-tool 自己絕對不能 deferred —— 不然 LLM 永遠找不到 search
    assert not reg.is_deferred(TOOL_SEARCH_TOOL_NAME)
    assert not reg.is_deferred(ACTIVATE_TOOL_NAME)
    # 也應該出現在 find_active 結果
    active_names = {t.name for t in reg.find_active(session=sess)}
    assert TOOL_SEARCH_TOOL_NAME in active_names
    assert ACTIVATE_TOOL_NAME in active_names


# ---------------------------------------------------------------------------
# MCP integration:MCP tool 預設 deferred
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_mcp_default_metadata_marks_deferred() -> None:
    """_MCP_TOOL_METADATA 模組常數應標 is_deferred=True(P1-18)。"""
    assert _MCP_TOOL_METADATA.is_deferred is True
    assert _MCP_TOOL_METADATA.category == "mcp"
    assert _MCP_TOOL_METADATA.is_open_world is True


@pytest.mark.unit
def test_build_function_tool_from_mcp_attaches_deferred_metadata() -> None:
    """build_function_tool_from_mcp 產生的 FunctionTool 必須帶 is_deferred=True。"""
    manager = MagicMock()
    manager.call_tool = AsyncMock(return_value="ok")
    mcp_tool = MCPTool(
        server_name="alpha",
        name="do_thing",
        description="Do something via MCP",
        input_schema={"type": "object"},
    )
    fn_tool = build_function_tool_from_mcp(manager, mcp_tool)
    meta = get_metadata(fn_tool)
    assert meta.is_deferred is True
    assert meta.category == "mcp"


@pytest.mark.unit
def test_registry_add_mcp_tool_lands_in_deferred() -> None:
    """MCP tool 透過 registry.add 註冊後,應自動進 deferred 集合 + find_active 不顯示。"""
    manager = MagicMock()
    manager.call_tool = AsyncMock(return_value="ok")
    mcp_tool = MCPTool(
        server_name="alpha",
        name="fetch_data",
        description="Fetch some data",
        input_schema={"type": "object"},
    )
    fn_tool = build_function_tool_from_mcp(manager, mcp_tool)
    reg = ToolRegistry()
    reg.add(fn_tool)

    assert reg.is_deferred(fn_tool.name) is True
    active = reg.find_active()
    assert fn_tool not in active
    deferred = reg.find_deferred()
    assert fn_tool in deferred
