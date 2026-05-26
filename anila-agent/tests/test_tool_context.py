"""AnilaToolContext + FileStateCache 單元測試。

對應 enhancement roadmap P0-3 任務的驗收 test。
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from anila_agent.core import (
    AnilaToolContext,
    FileStateCache,
    FileStateEntry,
    WorkspaceEscapeError,
)

# ---------------------------------------------------------------------------
# AnilaToolContext — dataclass 必欄位 / default 值
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_context_required_fields_and_defaults(tmp_path: Path) -> None:
    """建構時只給必欄位,其餘要拿到 sensible default。"""
    ctx = AnilaToolContext(
        session_id="sess-1",
        turn_id=0,
        tool_call_id="call-abc",
        agent_name="anila",
    )

    assert ctx.session_id == "sess-1"
    assert ctx.turn_id == 0
    assert ctx.tool_call_id == "call-abc"
    assert ctx.agent_name == "anila"
    # workspace 未指定 → None,代表不啟用邊界
    assert ctx.workspace is None
    # file_state_cache 每個 instance 一份(不要共用 class default)
    assert isinstance(ctx.file_state_cache, FileStateCache)
    assert ctx.user_id is None
    assert ctx.caller_id is None
    assert ctx.metadata == {}


@pytest.mark.unit
def test_each_context_has_independent_cache() -> None:
    """default_factory 必須讓每個 context 拿到自己的 cache,不會跨 instance 串味。"""
    a = AnilaToolContext(session_id="s", turn_id=0, tool_call_id="t1", agent_name="x")
    b = AnilaToolContext(session_id="s", turn_id=0, tool_call_id="t2", agent_name="x")
    assert a.file_state_cache is not b.file_state_cache
    assert a.metadata is not b.metadata


@pytest.mark.unit
def test_context_workspace_is_resolved(tmp_path: Path) -> None:
    """workspace 在 __post_init__ 內要被 resolve 成絕對路徑。"""
    sub = tmp_path / "ws"
    sub.mkdir()

    # 給相對路徑 + symlink 路徑,測 resolve 行為
    relative = Path(os.path.relpath(sub))
    ctx = AnilaToolContext(
        session_id="s",
        turn_id=0,
        tool_call_id="t",
        agent_name="x",
        workspace=relative,
    )
    assert ctx.workspace is not None
    assert ctx.workspace.is_absolute()
    assert ctx.workspace == sub.resolve()


# ---------------------------------------------------------------------------
# safe_path — 邊界保護
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_safe_path_allows_relative_inside_workspace(tmp_path: Path) -> None:
    """workspace 內的相對路徑應該被 anchor 並 resolve 回絕對路徑。"""
    ctx = AnilaToolContext(
        session_id="s",
        turn_id=0,
        tool_call_id="t",
        agent_name="x",
        workspace=tmp_path,
    )
    result = ctx.safe_path("data/foo.txt")
    assert result == (tmp_path / "data" / "foo.txt").resolve()


@pytest.mark.unit
def test_safe_path_allows_workspace_itself(tmp_path: Path) -> None:
    """workspace 本身也算合法(常見 list_dir 操作)。"""
    ctx = AnilaToolContext(
        session_id="s",
        turn_id=0,
        tool_call_id="t",
        agent_name="x",
        workspace=tmp_path,
    )
    assert ctx.safe_path(tmp_path) == tmp_path.resolve()
    assert ctx.safe_path(".") == tmp_path.resolve()


@pytest.mark.unit
def test_safe_path_rejects_dotdot_escape(tmp_path: Path) -> None:
    """`../../../etc/passwd` 之類的 escape 必須丟 WorkspaceEscapeError。"""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    ctx = AnilaToolContext(
        session_id="s",
        turn_id=0,
        tool_call_id="t",
        agent_name="x",
        workspace=workspace,
    )

    with pytest.raises(WorkspaceEscapeError):
        ctx.safe_path("../../../etc/passwd")


@pytest.mark.unit
def test_safe_path_rejects_absolute_outside_workspace(tmp_path: Path) -> None:
    """指向 workspace 外的絕對路徑也要擋。"""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    outsider = tmp_path / "elsewhere" / "secret.txt"

    ctx = AnilaToolContext(
        session_id="s",
        turn_id=0,
        tool_call_id="t",
        agent_name="x",
        workspace=workspace,
    )

    with pytest.raises(WorkspaceEscapeError):
        ctx.safe_path(outsider)


@pytest.mark.unit
def test_safe_path_no_workspace_means_no_boundary(tmp_path: Path) -> None:
    """workspace=None 時不檢查邊界,只 resolve。"""
    ctx = AnilaToolContext(
        session_id="s",
        turn_id=0,
        tool_call_id="t",
        agent_name="x",
    )
    target = tmp_path / "anywhere.txt"
    assert ctx.safe_path(target) == target.resolve()


@pytest.mark.unit
def test_is_inside_workspace(tmp_path: Path) -> None:
    """is_inside_workspace 是非 raising 版,給 policy / log 用。"""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    ctx = AnilaToolContext(
        session_id="s",
        turn_id=0,
        tool_call_id="t",
        agent_name="x",
        workspace=workspace,
    )

    assert ctx.is_inside_workspace(workspace / "foo.txt")
    assert not ctx.is_inside_workspace(tmp_path / "outside.txt")
    # workspace = None → 永遠合法
    ctx_no_ws = AnilaToolContext(
        session_id="s", turn_id=0, tool_call_id="t", agent_name="x"
    )
    assert ctx_no_ws.is_inside_workspace("/etc/passwd")


# ---------------------------------------------------------------------------
# FileStateCache — record_read / record_write / is_unchanged_since_read
# ---------------------------------------------------------------------------


def _write(path: Path, content: bytes) -> None:
    """test helper:寫檔。"""
    path.write_bytes(content)


@pytest.mark.unit
def test_cache_record_read_captures_state(tmp_path: Path) -> None:
    """record_read 要回傳 entry,且 has / get 都能拿到。"""
    target = tmp_path / "a.txt"
    _write(target, b"hello world")

    cache = FileStateCache()
    entry = cache.record_read(target)

    assert isinstance(entry, FileStateEntry)
    assert entry.action == "read"
    assert entry.size == len(b"hello world")
    assert entry.sha256  # 不空字串
    assert cache.has(target)
    assert cache.get(target) == entry


@pytest.mark.unit
def test_cache_record_write_overwrites_entry(tmp_path: Path) -> None:
    """同一個 path 先 read 再 write,entry 要更新為 write。"""
    target = tmp_path / "a.txt"
    _write(target, b"v1")

    cache = FileStateCache()
    read_entry = cache.record_read(target)
    assert read_entry.action == "read"

    # mtime 解析度有些 FS 是 1ms,改檔前 sleep 確保 mtime 變動可被偵測
    time.sleep(0.01)
    _write(target, b"v2-longer")
    write_entry = cache.record_write(target)

    assert write_entry.action == "write"
    assert write_entry.size == len(b"v2-longer")
    assert write_entry.sha256 != read_entry.sha256
    # cache 內只剩一個 entry(被覆蓋)
    assert len(cache.snapshot()) == 1


@pytest.mark.unit
def test_is_unchanged_since_read_true_when_no_third_party(tmp_path: Path) -> None:
    """讀完之後沒人動 → is_unchanged_since_read 為 True。"""
    target = tmp_path / "stable.txt"
    _write(target, b"data")

    cache = FileStateCache()
    cache.record_read(target)
    assert cache.is_unchanged_since_read(target) is True


@pytest.mark.unit
def test_is_unchanged_since_read_false_when_modified(tmp_path: Path) -> None:
    """第三方改檔之後 → 應該偵測得到。"""
    target = tmp_path / "vol.txt"
    _write(target, b"data")

    cache = FileStateCache()
    cache.record_read(target)

    # 模擬第三方寫入(改內容並保證 mtime / size 變)
    time.sleep(0.01)
    _write(target, b"data-modified")

    assert cache.is_unchanged_since_read(target) is False


@pytest.mark.unit
def test_is_unchanged_since_read_false_when_never_read(tmp_path: Path) -> None:
    """沒 record 過的檔案不能說 unchanged,要回 False。"""
    target = tmp_path / "nope.txt"
    _write(target, b"x")
    cache = FileStateCache()
    assert cache.is_unchanged_since_read(target) is False


@pytest.mark.unit
def test_is_unchanged_since_read_false_when_deleted(tmp_path: Path) -> None:
    """檔案被刪 → 也算不一致,回 False(避免 caller 拿著舊 hash 誤判)。"""
    target = tmp_path / "ghost.txt"
    _write(target, b"x")
    cache = FileStateCache()
    cache.record_read(target)
    target.unlink()
    assert cache.is_unchanged_since_read(target) is False


@pytest.mark.unit
def test_cache_clear(tmp_path: Path) -> None:
    """clear() 把所有 entry 移掉。"""
    a = tmp_path / "a.txt"
    b = tmp_path / "b.txt"
    _write(a, b"a")
    _write(b, b"b")

    cache = FileStateCache()
    cache.record_read(a)
    cache.record_read(b)
    assert len(cache.snapshot()) == 2

    cache.clear()
    assert len(cache.snapshot()) == 0
    assert not cache.has(a)


@pytest.mark.unit
def test_cache_keys_are_resolved(tmp_path: Path) -> None:
    """傳入的 path 不論相對 / 帶 `.` / 帶 `..` 都應該 normalize 為同一個 key。"""
    target = tmp_path / "data" / "file.txt"
    target.parent.mkdir()
    _write(target, b"content")

    cache = FileStateCache()
    cache.record_read(target)

    # 相對路徑 + 帶 ./ 也要能查到同一個 entry
    alt = tmp_path / "data" / "." / "file.txt"
    assert cache.has(alt)
    assert cache.get(alt) is not None
