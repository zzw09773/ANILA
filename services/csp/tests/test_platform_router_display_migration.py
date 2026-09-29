"""既有 anila-router 列的顯示名稱只在仍是出廠文案時改成 ANILA。"""
from __future__ import annotations

import importlib.util
from pathlib import Path

from sqlalchemy.orm import Session

from app.models.model_registry import ModelRegistry

_MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "migrations"
    / "versions"
    / "r1_0064_rename_platform_chat_entry_display.py"
)
_STOCK = "ANILA 自動選助手"
_RENAMED = "ANILA"


def _migration():
    spec = importlib.util.spec_from_file_location(_MIGRATION.stem, _MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _row(db: Session, name: str, display_name: str) -> ModelRegistry:
    row = ModelRegistry(
        name=name,
        display_name=display_name,
        model_type="llm",
        endpoint_url="http://models.example/v1",
        is_active=True,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def test_upgrade_renames_only_the_stock_platform_entry_label(db: Session):
    module = _migration()
    entry = _row(db, "anila-router", _STOCK)
    other = _row(db, "glm-kept", _STOCK)

    module.rename_platform_chat_entry_display(
        db, new_name=_RENAMED, old_name=_STOCK
    )
    db.commit()
    db.expire_all()

    assert db.get(ModelRegistry, entry.id).display_name == _RENAMED
    assert db.get(ModelRegistry, entry.id).name == "anila-router"
    assert db.get(ModelRegistry, other.id).display_name == _STOCK


def test_upgrade_leaves_an_admin_custom_entry_name(db: Session):
    module = _migration()
    entry = _row(db, "anila-router", "院內總入口")

    module.rename_platform_chat_entry_display(
        db, new_name=_RENAMED, old_name=_STOCK
    )
    db.commit()
    db.expire_all()

    assert db.get(ModelRegistry, entry.id).display_name == "院內總入口"
    assert db.get(ModelRegistry, entry.id).name == "anila-router"


def test_downgrade_leaves_display_names_alone():
    """降版分不出這一版改的與管理員自己取的「ANILA」，所以一律不動。"""
    import inspect

    module = _migration()
    source = inspect.getsource(module.downgrade)
    assert "rename_platform_chat_entry_display" not in source
    assert "execute" not in source
