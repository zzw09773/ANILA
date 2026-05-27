"""P2-7 file-index fuzzy file search tests。

涵蓋:

* :class:`FileIndex` build / refresh 正確性
* exclude_patterns(預設與自訂)真的 skip 對應檔案 / 目錄
* :meth:`FileIndex.fuzzy_search` 對 exact / partial / typo 各種 query 的行為
* ``top_k`` limit 與 score 排序
* meta-tool ``find_file`` 包裝(:class:`FunctionTool` schema、metadata、payload 格式)
* 與 :class:`AnilaToolContext` 整合 ── ``safe_path`` 過濾、workspace 為基準
* :func:`register_file_index_tools` 註冊進 :class:`ToolRegistry`
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any

import pytest

from anila_agent.core.context import AnilaToolContext
from anila_agent.tools import (
    DEFAULT_EXCLUDE_PATTERNS,
    FIND_FILE_TOOL_NAME,
    FileIndex,
    FileMatch,
    ToolRegistry,
    build_find_file_tool,
    fuzzy_search,
    get_metadata,
    register_file_index_tools,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_tree(root: Path) -> None:
    """建立一個有代表性的 dummy file tree。

    包含:應 index 的 source file、應 exclude 的 .git / node_modules / __pycache__,
    以及多層 sub-dir 與一個 *.pyc 檔。
    """
    (root / "src").mkdir()
    (root / "src" / "auth").mkdir()
    (root / "tests").mkdir()
    (root / ".git").mkdir()
    (root / "node_modules").mkdir()
    (root / "node_modules" / "lib").mkdir()
    (root / "__pycache__").mkdir()

    (root / "README.md").write_text("readme")
    (root / "auth.py").write_text("a")
    (root / "src" / "main.py").write_text("m")
    (root / "src" / "auth_handler.py").write_text("ah")
    (root / "src" / "auth" / "login.py").write_text("login")
    (root / "tests" / "test_auth.py").write_text("ta")
    (root / "deep_module.py").write_text("d")
    (root / "compiled.pyc").write_text("pyc")

    (root / ".git" / "config").write_text("g")
    (root / "node_modules" / "lib" / "index.js").write_text("j")
    (root / "__pycache__" / "cached.pyc").write_text("c")


def _invoke_tool(tool: Any, payload: dict[str, Any]) -> dict[str, Any]:
    """同步包 ``await tool.on_invoke_tool``,解析回 dict 方便 assert。"""
    raw = asyncio.run(tool.on_invoke_tool(None, json.dumps(payload)))
    parsed: dict[str, Any] = json.loads(raw)
    return parsed


# ---------------------------------------------------------------------------
# FileIndex 基本建構
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_init_rejects_missing_root(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        FileIndex(tmp_path / "no-such-dir")


@pytest.mark.unit
def test_init_rejects_non_directory(tmp_path: Path) -> None:
    file = tmp_path / "f.txt"
    file.write_text("x")
    with pytest.raises(NotADirectoryError):
        FileIndex(file)


@pytest.mark.unit
def test_default_exclude_patterns_constant() -> None:
    """確保 DEFAULT_EXCLUDE_PATTERNS 涵蓋 .git / node_modules / __pycache__ / .venv / *.pyc。"""
    assert ".git/*" in DEFAULT_EXCLUDE_PATTERNS
    assert "node_modules/*" in DEFAULT_EXCLUDE_PATTERNS
    assert "__pycache__/*" in DEFAULT_EXCLUDE_PATTERNS
    assert ".venv/*" in DEFAULT_EXCLUDE_PATTERNS
    assert "*.pyc" in DEFAULT_EXCLUDE_PATTERNS


# ---------------------------------------------------------------------------
# build
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_build_collects_workspace_files(tmp_path: Path) -> None:
    _make_tree(tmp_path)
    idx = FileIndex(tmp_path)
    idx.build()

    names = {p.name for p in idx.files}
    assert "README.md" in names
    assert "auth.py" in names
    assert "auth_handler.py" in names
    assert "login.py" in names
    assert "test_auth.py" in names


@pytest.mark.unit
def test_build_skips_excluded_dirs(tmp_path: Path) -> None:
    """預設 exclude pattern 應 skip .git / node_modules / __pycache__。"""
    _make_tree(tmp_path)
    idx = FileIndex(tmp_path)
    idx.build()
    rels = {p.relative_to(tmp_path).as_posix() for p in idx.files}
    for rel in rels:
        assert not rel.startswith(".git/"), rel
        assert not rel.startswith("node_modules/"), rel
        assert not rel.startswith("__pycache__/"), rel


@pytest.mark.unit
def test_build_skips_pyc_files(tmp_path: Path) -> None:
    """``*.pyc`` glob 對 root 直接的 .pyc 與深層 .pyc 都應 skip。"""
    _make_tree(tmp_path)
    idx = FileIndex(tmp_path)
    idx.build()
    for p in idx.files:
        assert not p.name.endswith(".pyc"), p


@pytest.mark.unit
def test_custom_exclude_patterns(tmp_path: Path) -> None:
    """自訂 exclude patterns 完全覆寫預設。"""
    _make_tree(tmp_path)
    # 不傳預設 → tests/ 不該被 skip,但 src/ 該被排除
    idx = FileIndex(tmp_path, exclude_patterns=["src/*"])
    idx.build()
    rels = {p.relative_to(tmp_path).as_posix() for p in idx.files}
    assert not any(r.startswith("src/") for r in rels)
    # .git / node_modules / *.pyc 因為沒帶,沒被 exclude
    assert any(r.startswith(".git/") for r in rels)
    assert any(r.endswith(".pyc") for r in rels)


@pytest.mark.unit
def test_empty_exclude_patterns_includes_everything(tmp_path: Path) -> None:
    """傳空 tuple → 不排除任何檔案。"""
    _make_tree(tmp_path)
    idx = FileIndex(tmp_path, exclude_patterns=())
    idx.build()
    rels = {p.relative_to(tmp_path).as_posix() for p in idx.files}
    assert any(r.startswith(".git/") for r in rels)
    assert any(r.startswith("node_modules/") for r in rels)


@pytest.mark.unit
def test_len_reflects_indexed_files(tmp_path: Path) -> None:
    _make_tree(tmp_path)
    idx = FileIndex(tmp_path)
    idx.build()
    assert len(idx) == len(idx.files)
    assert len(idx) > 0


# ---------------------------------------------------------------------------
# refresh
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_refresh_without_build_does_full_build(tmp_path: Path) -> None:
    _make_tree(tmp_path)
    idx = FileIndex(tmp_path)
    idx.refresh()  # 直接 refresh — 應退回 build
    assert len(idx) > 0


@pytest.mark.unit
def test_refresh_picks_up_new_file(tmp_path: Path) -> None:
    _make_tree(tmp_path)
    idx = FileIndex(tmp_path)
    idx.build()
    before = len(idx)

    # 加一個新檔。為了確保 mtime_ns 改變,顯式 bump 父 dir mtime。
    new_file = tmp_path / "src" / "added.py"
    new_file.write_text("added")
    _bump_mtime(tmp_path / "src")

    idx.refresh()
    after = len(idx)
    assert after == before + 1
    assert any(p.name == "added.py" for p in idx.files)


@pytest.mark.unit
def test_refresh_drops_removed_file(tmp_path: Path) -> None:
    _make_tree(tmp_path)
    idx = FileIndex(tmp_path)
    idx.build()
    target = tmp_path / "src" / "main.py"
    target.unlink()
    _bump_mtime(tmp_path / "src")

    idx.refresh()
    names = {p.name for p in idx.files}
    assert "main.py" not in names


@pytest.mark.unit
def test_refresh_handles_removed_directory(tmp_path: Path) -> None:
    _make_tree(tmp_path)
    idx = FileIndex(tmp_path)
    idx.build()
    # 刪整個 sub-tree
    import shutil

    shutil.rmtree(tmp_path / "src")
    _bump_mtime(tmp_path)

    idx.refresh()
    rels = {p.relative_to(tmp_path).as_posix() for p in idx.files}
    assert not any(r.startswith("src/") for r in rels)


@pytest.mark.unit
def test_refresh_unchanged_dir_reuses_entries(tmp_path: Path) -> None:
    """mtime 沒變的 dir,refresh 後檔案 list 不變。"""
    _make_tree(tmp_path)
    idx = FileIndex(tmp_path)
    idx.build()
    before_paths = sorted(p.as_posix() for p in idx.files)
    idx.refresh()
    after_paths = sorted(p.as_posix() for p in idx.files)
    assert after_paths == before_paths


# ---------------------------------------------------------------------------
# fuzzy_search
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_fuzzy_search_empty_query_returns_top_k(tmp_path: Path) -> None:
    _make_tree(tmp_path)
    idx = FileIndex(tmp_path)
    idx.build()
    matches = idx.fuzzy_search("", top_k=3)
    assert len(matches) == 3
    assert all(isinstance(m, FileMatch) for m in matches)
    assert all(m.score == 0.0 for m in matches)


@pytest.mark.unit
def test_fuzzy_search_without_build_returns_empty(tmp_path: Path) -> None:
    idx = FileIndex(tmp_path)
    assert idx.fuzzy_search("auth", top_k=5) == []


@pytest.mark.unit
def test_fuzzy_search_exact_filename_ranks_first(tmp_path: Path) -> None:
    _make_tree(tmp_path)
    idx = FileIndex(tmp_path)
    idx.build()
    matches = idx.fuzzy_search("auth", top_k=5)
    assert matches, "expected at least one match"
    top_names = [m.path.name for m in matches]
    # exact filename match `auth.py` 應排第一
    assert top_names[0] == "auth.py"
    # 同 query 也應命中 auth_handler.py 與 login.py 所在的 path
    matched_paths = {str(m.path) for m in matches}
    assert any("auth_handler" in p for p in matched_paths)


@pytest.mark.unit
def test_fuzzy_search_partial_query_matches_substring(tmp_path: Path) -> None:
    _make_tree(tmp_path)
    idx = FileIndex(tmp_path)
    idx.build()
    matches = idx.fuzzy_search("hndlr", top_k=5)  # 跨字元 fuzzy
    names = [m.path.name for m in matches]
    assert "auth_handler.py" in names


@pytest.mark.unit
def test_fuzzy_search_typo_no_match(tmp_path: Path) -> None:
    """完全沒任何 subsequence 命中的 query 應拿不到任何結果。"""
    _make_tree(tmp_path)
    idx = FileIndex(tmp_path)
    idx.build()
    matches = idx.fuzzy_search("zzzzz_no_such", top_k=5)
    assert matches == []


@pytest.mark.unit
def test_fuzzy_search_top_k_limit(tmp_path: Path) -> None:
    _make_tree(tmp_path)
    idx = FileIndex(tmp_path)
    idx.build()
    matches = idx.fuzzy_search("py", top_k=2)
    assert len(matches) <= 2


@pytest.mark.unit
def test_fuzzy_search_top_k_zero_returns_all_matched(tmp_path: Path) -> None:
    _make_tree(tmp_path)
    idx = FileIndex(tmp_path)
    idx.build()
    matches = idx.fuzzy_search("py", top_k=0)
    # 所有 .py 都該命中
    py_count = sum(1 for p in idx.files if p.suffix == ".py")
    assert len(matches) >= py_count


@pytest.mark.unit
def test_fuzzy_search_scores_descending(tmp_path: Path) -> None:
    _make_tree(tmp_path)
    idx = FileIndex(tmp_path)
    idx.build()
    matches = idx.fuzzy_search("auth", top_k=10)
    scores = [m.score for m in matches]
    assert scores == sorted(scores, reverse=True)


@pytest.mark.unit
def test_fuzzy_search_returns_match_segments(tmp_path: Path) -> None:
    _make_tree(tmp_path)
    idx = FileIndex(tmp_path)
    idx.build()
    matches = idx.fuzzy_search("auth", top_k=5)
    assert matches
    top = matches[0]
    assert top.match_segments, "expected non-empty match_segments"
    for start, end in top.match_segments:
        assert 0 <= start < end


@pytest.mark.unit
def test_module_level_fuzzy_search_helper(tmp_path: Path) -> None:
    """module-level :func:`fuzzy_search` 應等同 method 呼叫。"""
    _make_tree(tmp_path)
    idx = FileIndex(tmp_path)
    idx.build()
    a = idx.fuzzy_search("auth", top_k=5)
    b = fuzzy_search(idx, "auth", top_k=5)
    assert [m.path for m in a] == [m.path for m in b]


# ---------------------------------------------------------------------------
# Meta-tool: find_file
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_build_find_file_tool_metadata(tmp_path: Path) -> None:
    _make_tree(tmp_path)
    idx = FileIndex(tmp_path)
    tool = build_find_file_tool(idx)

    assert tool.name == FIND_FILE_TOOL_NAME
    meta = get_metadata(tool)
    assert meta.is_read_only is True
    assert meta.category == "filesystem"
    assert meta.concurrency_safe is True
    assert meta.is_deferred is False
    assert meta.cost_estimate == "low"


@pytest.mark.unit
def test_find_file_tool_returns_matches(tmp_path: Path) -> None:
    _make_tree(tmp_path)
    idx = FileIndex(tmp_path)
    idx.build()
    tool = build_find_file_tool(idx)

    parsed = _invoke_tool(tool, {"query": "auth", "top_k": 5})
    assert parsed["count"] >= 1
    assert parsed["root"] == str(idx.root)
    paths = [m["path"] for m in parsed["matches"]]
    # rel path 不含絕對 prefix
    assert all(not p.startswith("/") for p in paths)
    # absolute_path 仍是絕對
    abs_paths = [m["absolute_path"] for m in parsed["matches"]]
    assert all(p.startswith(str(tmp_path)) for p in abs_paths)


@pytest.mark.unit
def test_find_file_tool_auto_builds_on_first_call(tmp_path: Path) -> None:
    _make_tree(tmp_path)
    idx = FileIndex(tmp_path)
    assert len(idx) == 0
    tool = build_find_file_tool(idx)

    parsed = _invoke_tool(tool, {"query": "auth"})
    assert parsed["count"] >= 1
    assert len(idx) > 0


@pytest.mark.unit
def test_find_file_tool_no_auto_build(tmp_path: Path) -> None:
    _make_tree(tmp_path)
    idx = FileIndex(tmp_path)
    tool = build_find_file_tool(idx, auto_build=False)
    parsed = _invoke_tool(tool, {"query": "auth"})
    assert parsed["count"] == 0  # 沒 build,fuzzy_search 回空


@pytest.mark.unit
def test_find_file_tool_invalid_json_returns_error(tmp_path: Path) -> None:
    _make_tree(tmp_path)
    idx = FileIndex(tmp_path)
    idx.build()
    tool = build_find_file_tool(idx)

    raw = asyncio.run(tool.on_invoke_tool(None, "{not json"))
    parsed = json.loads(raw)
    assert parsed["count"] == 0
    assert parsed["error"] == "invalid_json"


@pytest.mark.unit
def test_find_file_tool_invalid_top_k_falls_back_to_default(tmp_path: Path) -> None:
    _make_tree(tmp_path)
    idx = FileIndex(tmp_path)
    idx.build()
    tool = build_find_file_tool(idx)
    parsed = _invoke_tool(tool, {"query": "py", "top_k": "abc"})
    # 預設 10 → 應拿到 <=10
    assert parsed["count"] <= 10


@pytest.mark.unit
def test_find_file_tool_empty_matches_hint(tmp_path: Path) -> None:
    _make_tree(tmp_path)
    idx = FileIndex(tmp_path)
    idx.build()
    tool = build_find_file_tool(idx)
    parsed = _invoke_tool(tool, {"query": "qqqq_no_such_xx"})
    assert parsed["count"] == 0
    assert "hint" in parsed


@pytest.mark.unit
def test_find_file_tool_schema_shape(tmp_path: Path) -> None:
    _make_tree(tmp_path)
    idx = FileIndex(tmp_path)
    tool = build_find_file_tool(idx)
    schema = tool.params_json_schema
    assert schema["type"] == "object"
    assert "query" in schema["properties"]
    assert "top_k" in schema["properties"]
    assert schema["required"] == ["query"]


# ---------------------------------------------------------------------------
# AnilaToolContext 整合
# ---------------------------------------------------------------------------


def _make_ctx(workspace: Path) -> AnilaToolContext:
    return AnilaToolContext(
        session_id="sess",
        turn_id=1,
        tool_call_id="call-1",
        agent_name="test-agent",
        workspace=workspace,
    )


@pytest.mark.unit
def test_find_file_tool_uses_context_workspace(tmp_path: Path) -> None:
    """context_resolver 提供 ctx 時,result 的 root 應取 ctx.workspace。"""
    _make_tree(tmp_path)
    idx = FileIndex(tmp_path)
    idx.build()
    ctx = _make_ctx(tmp_path)
    tool = build_find_file_tool(idx, context_resolver=lambda: ctx)

    parsed = _invoke_tool(tool, {"query": "auth"})
    # root 該是 ctx.workspace(已 resolve),不是裸的 idx.root
    assert parsed["root"] == str(ctx.workspace)
    assert parsed["count"] >= 1


@pytest.mark.unit
def test_find_file_tool_filters_paths_outside_workspace(tmp_path: Path) -> None:
    """若有 path 跳脫 workspace(極端 case),safe_path 應把它從結果剔除。"""
    _make_tree(tmp_path)
    # 索引整個 tmp_path,但 ctx workspace 只給 src/ — src/ 外的檔案應被剔除。
    idx = FileIndex(tmp_path)
    idx.build()
    ctx = _make_ctx(tmp_path / "src")
    tool = build_find_file_tool(idx, context_resolver=lambda: ctx)

    parsed = _invoke_tool(tool, {"query": "py", "top_k": 50})
    abs_paths = [m["absolute_path"] for m in parsed["matches"]]
    src_abs = str((tmp_path / "src").resolve())
    for p in abs_paths:
        assert p.startswith(src_abs), p


@pytest.mark.unit
def test_find_file_tool_context_resolver_can_return_none(tmp_path: Path) -> None:
    _make_tree(tmp_path)
    idx = FileIndex(tmp_path)
    idx.build()
    tool = build_find_file_tool(idx, context_resolver=lambda: None)
    parsed = _invoke_tool(tool, {"query": "auth"})
    assert parsed["count"] >= 1
    assert parsed["root"] == str(idx.root)


# ---------------------------------------------------------------------------
# register_file_index_tools 與 ToolRegistry
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_register_file_index_tools_adds_find_file(tmp_path: Path) -> None:
    _make_tree(tmp_path)
    idx = FileIndex(tmp_path)
    idx.build()
    reg = ToolRegistry()
    tools = register_file_index_tools(reg, idx)

    assert len(tools) == 1
    assert tools[0].name == FIND_FILE_TOOL_NAME
    assert FIND_FILE_TOOL_NAME in reg.tools
    # 預設 active (非 deferred)
    assert not reg.is_deferred(FIND_FILE_TOOL_NAME)
    active_names = {t.name for t in reg.find_active()}
    assert FIND_FILE_TOOL_NAME in active_names


@pytest.mark.unit
def test_register_file_index_tools_duplicate_raises(tmp_path: Path) -> None:
    _make_tree(tmp_path)
    idx = FileIndex(tmp_path)
    reg = ToolRegistry()
    register_file_index_tools(reg, idx)
    with pytest.raises(ValueError, match="Duplicate"):
        register_file_index_tools(reg, idx)


# ---------------------------------------------------------------------------
# Internal helper — 私有,但測試從外部用
# ---------------------------------------------------------------------------


def _bump_mtime(path: Path) -> None:
    """確保 path 的 mtime_ns 變動。

    部分檔案系統 mtime 解析度只到 ms / s,做完寫入 sleep 也可能拿不到不同的 ns。
    這裡直接 ``os.utime`` 把 mtime 設成「現在 + 1 秒」,保證測試穩定。
    """
    target_ns = (time.time_ns() // 1_000_000_000 + 1) * 1_000_000_000
    os.utime(path, ns=(target_ns, target_ns))
