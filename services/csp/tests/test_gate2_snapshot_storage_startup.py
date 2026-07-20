from __future__ import annotations

import os
from pathlib import Path
import stat
from types import SimpleNamespace

import pytest

from app.services import startup_security


def _formal_path(monkeypatch, tmp_path: Path) -> Path:
    monkeypatch.setattr(
        startup_security.settings,
        "ANILA_DEPLOYMENT_PROFILE",
        "prod-intranet-card",
    )
    monkeypatch.setattr(
        startup_security, "_FORMAL_SOURCE_SNAPSHOT_PATH", tmp_path
    )
    monkeypatch.setattr(
        startup_security.settings,
        "SOURCE_SNAPSHOT_STORAGE_PATH",
        str(tmp_path),
    )
    return tmp_path


def test_formal_snapshot_mount_requires_runtime_owner_and_0700(
    monkeypatch, tmp_path,
):
    path = _formal_path(monkeypatch, tmp_path)
    uid = os.geteuid() if hasattr(os, "geteuid") else 0
    monkeypatch.setattr(
        Path,
        "lstat",
        lambda self: SimpleNamespace(st_mode=stat.S_IFDIR | 0o700, st_uid=uid),
    )
    monkeypatch.setattr(os, "access", lambda target, mode: target == path)

    startup_security.assert_source_snapshot_storage_policy()

    monkeypatch.setattr(
        Path,
        "lstat",
        lambda self: SimpleNamespace(st_mode=stat.S_IFDIR | 0o750, st_uid=uid),
    )
    with pytest.raises(RuntimeError, match="mode 0700"):
        startup_security.assert_source_snapshot_storage_policy()


def test_formal_snapshot_mount_path_is_not_configurable(
    monkeypatch, tmp_path,
):
    _formal_path(monkeypatch, tmp_path)
    startup_security.settings.SOURCE_SNAPSHOT_STORAGE_PATH = str(tmp_path / "other")
    with pytest.raises(RuntimeError, match="must be"):
        startup_security.assert_source_snapshot_storage_policy()


def test_development_profile_does_not_require_formal_mount(monkeypatch):
    monkeypatch.setattr(
        startup_security.settings, "ANILA_DEPLOYMENT_PROFILE", "development"
    )
    monkeypatch.setattr(
        startup_security.settings,
        "SOURCE_SNAPSHOT_STORAGE_PATH",
        "data/source-snapshots",
    )
    startup_security.assert_source_snapshot_storage_policy()
