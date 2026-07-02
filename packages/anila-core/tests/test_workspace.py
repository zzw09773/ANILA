"""Tests for the Workspace primitive (Sprint 12 PR 1)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from anila_core.workspace import (
    CapDeniedError,
    PathEscapeError,
    WorkspaceCaps,
    WorkspaceError,
    make_workspace,
)
from anila_core.workspace.caps import READ_ONLY_INSPECT_ALLOWLIST


# ---------------------------------------------------------------------------
# WorkspaceCaps
# ---------------------------------------------------------------------------


def test_default_caps_are_safe() -> None:
    caps = WorkspaceCaps()
    assert caps.fs_read is True
    assert caps.fs_write is True
    assert caps.network is False
    assert caps.exec_bash is False
    assert caps.exec_python is False
    assert caps.command_allowlist == ()
    assert caps.max_exec_seconds == 30
    assert caps.max_workspace_size_mb == 100


def test_caps_are_frozen() -> None:
    caps = WorkspaceCaps()
    with pytest.raises(Exception):  # dataclass FrozenInstanceError
        caps.network = True  # type: ignore[misc]


def test_with_overrides_returns_new_instance() -> None:
    caps = WorkspaceCaps()
    upgraded = caps.with_overrides(network=True, exec_bash=True)
    assert caps.network is False  # original untouched
    assert upgraded.network is True
    assert upgraded.exec_bash is True
    assert upgraded.fs_read is True  # other fields preserved


def test_inspect_allowlist_constants_exposed() -> None:
    assert "ls" in READ_ONLY_INSPECT_ALLOWLIST
    assert "rm" not in READ_ONLY_INSPECT_ALLOWLIST


# ---------------------------------------------------------------------------
# make_workspace + lifecycle
# ---------------------------------------------------------------------------


def test_make_workspace_creates_dir(tmp_path: Path) -> None:
    ws = make_workspace(root=str(tmp_path))
    try:
        assert ws.path.exists()
        assert ws.path.is_dir()
        assert ws.id.startswith("ws-")
        # The workspace dir is *under* the root we asked for.
        assert ws.path.parent == tmp_path.resolve()
    finally:
        ws.cleanup()


def test_workspace_is_closed_after_cleanup(tmp_path: Path) -> None:
    ws = make_workspace(root=str(tmp_path))
    assert ws.is_closed is False
    ws.cleanup()
    assert ws.is_closed is True
    assert not ws.path.exists()


def test_cleanup_is_idempotent(tmp_path: Path) -> None:
    ws = make_workspace(root=str(tmp_path))
    ws.cleanup()
    ws.cleanup()  # must not raise


def test_cleanup_after_false_keeps_dir(tmp_path: Path) -> None:
    ws = make_workspace(root=str(tmp_path), cleanup_after=False)
    target = ws.safe_path("artifact.txt")
    target.write_text("preserve me")
    ws.cleanup()
    assert ws.is_closed is True
    assert ws.path.exists()  # still there
    assert target.read_text() == "preserve me"


def test_context_manager_cleans_up(tmp_path: Path) -> None:
    with make_workspace(root=str(tmp_path)) as ws:
        ws_path = ws.path
        assert ws_path.exists()
    assert not ws_path.exists()


def test_context_manager_cleans_up_on_exception(tmp_path: Path) -> None:
    ws_holder: list[Path] = []
    with pytest.raises(RuntimeError):
        with make_workspace(root=str(tmp_path)) as ws:
            ws_holder.append(ws.path)
            raise RuntimeError("boom")
    assert ws_holder and not ws_holder[0].exists()


# ---------------------------------------------------------------------------
# safe_path
# ---------------------------------------------------------------------------


def test_safe_path_resolves_relative_inside_workspace(tmp_path: Path) -> None:
    with make_workspace(root=str(tmp_path)) as ws:
        resolved = ws.safe_path("subdir/file.txt")
        assert resolved.is_absolute()
        assert str(resolved).startswith(str(ws.path))


def test_safe_path_accepts_absolute_inside_workspace(tmp_path: Path) -> None:
    with make_workspace(root=str(tmp_path)) as ws:
        resolved = ws.safe_path(str(ws.path / "ok.txt"))
        assert resolved == (ws.path / "ok.txt")


def test_safe_path_rejects_dot_dot_escape(tmp_path: Path) -> None:
    with make_workspace(root=str(tmp_path)) as ws:
        with pytest.raises(PathEscapeError) as exc:
            ws.safe_path("../../etc/passwd")
        assert exc.value.workspace_root == str(ws.path)


def test_safe_path_rejects_absolute_outside_workspace(tmp_path: Path) -> None:
    with make_workspace(root=str(tmp_path)) as ws:
        with pytest.raises(PathEscapeError):
            # Pick a path guaranteed to be outside the workspace.
            ws.safe_path(str(tmp_path / "outside.txt"))


def test_safe_path_rejects_symlink_escape(tmp_path: Path) -> None:
    """Symlinks pointing outside the workspace should be caught."""
    if os.name == "nt":
        pytest.skip("Symlink creation needs admin on Windows")
    with make_workspace(root=str(tmp_path)) as ws:
        outside = tmp_path / "outside_target"
        outside.write_text("secret")
        link = ws.path / "shortcut"
        link.symlink_to(outside)
        with pytest.raises(PathEscapeError):
            ws.safe_path("shortcut")


def test_safe_path_after_close_raises(tmp_path: Path) -> None:
    ws = make_workspace(root=str(tmp_path))
    ws.cleanup()
    with pytest.raises(RuntimeError, match="closed"):
        ws.safe_path("x")


def test_safe_path_resolved_path_need_not_exist(tmp_path: Path) -> None:
    with make_workspace(root=str(tmp_path)) as ws:
        resolved = ws.safe_path("does_not_exist_yet/file.txt")
        assert resolved.parent.name == "does_not_exist_yet"


# ---------------------------------------------------------------------------
# relative()
# ---------------------------------------------------------------------------


def test_relative_renders_workspace_rooted_posix(tmp_path: Path) -> None:
    with make_workspace(root=str(tmp_path)) as ws:
        abs_path = ws.safe_path("nested/dir/file.txt")
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        abs_path.write_text("x")
        assert ws.relative(abs_path) == "nested/dir/file.txt"


def test_relative_rejects_outside_path(tmp_path: Path) -> None:
    with make_workspace(root=str(tmp_path)) as ws:
        with pytest.raises(PathEscapeError):
            ws.relative(tmp_path / "outside.txt")


# ---------------------------------------------------------------------------
# session_id propagation + repr
# ---------------------------------------------------------------------------


def test_session_id_passes_through(tmp_path: Path) -> None:
    with make_workspace(root=str(tmp_path), session_id="s-x") as ws:
        assert ws.session_id == "s-x"


def test_repr_includes_id(tmp_path: Path) -> None:
    with make_workspace(root=str(tmp_path)) as ws:
        r = repr(ws)
        assert ws.id in r
        assert "Workspace(" in r


# ---------------------------------------------------------------------------
# Multiple workspaces are isolated
# ---------------------------------------------------------------------------


def test_two_workspaces_have_distinct_paths(tmp_path: Path) -> None:
    a = make_workspace(root=str(tmp_path))
    b = make_workspace(root=str(tmp_path))
    try:
        assert a.id != b.id
        assert a.path != b.path
    finally:
        a.cleanup()
        b.cleanup()


def test_caps_default_safe_for_make_workspace(tmp_path: Path) -> None:
    with make_workspace(root=str(tmp_path)) as ws:
        assert ws.caps.network is False
        assert ws.caps.exec_bash is False


def test_caps_passed_through(tmp_path: Path) -> None:
    custom = WorkspaceCaps(network=True, exec_bash=True, max_exec_seconds=5)
    with make_workspace(root=str(tmp_path), caps=custom) as ws:
        assert ws.caps is custom
        assert ws.caps.network is True
        assert ws.caps.max_exec_seconds == 5


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


def test_path_escape_error_carries_context() -> None:
    err = PathEscapeError(
        requested="../etc/passwd",
        resolved="/etc/passwd",
        workspace_root="/tmp/anila-workspaces/ws-xxx",
    )
    assert err.requested == "../etc/passwd"
    assert err.resolved == "/etc/passwd"
    assert "outside" in str(err)


def test_cap_denied_error_shape() -> None:
    err = CapDeniedError(cap="exec_bash", detail="rm not in allowlist")
    assert err.cap == "exec_bash"
    assert "rm" in str(err)


def test_path_escape_is_workspace_error() -> None:
    assert issubclass(PathEscapeError, WorkspaceError)
    assert issubclass(CapDeniedError, WorkspaceError)
